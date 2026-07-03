#!/usr/bin/env python3
"""Mine Claude Code session transcripts for creation events.

Answers: what has Claude created across all sessions, and which of it is
durable (still on disk / merged to a repo) vs ephemeral (tmp, scratchpads,
removed worktrees)? Also tallies repeated Bash procedures worth codifying
into standalone scripts.

This is the artifact-side complement to poe-extract.py (which mines *user*
turns for voice signals). Before this script existed, one-off variants of it
were rebuilt in throwaway sessions at least 12 times (mine_sessions.py,
mine_failures_v3.py, friction_mining_v2.py, ...) — hence the durable home.

Usage:
    python3 artifact-miner.py                       # writes artifact-mine.json to cwd
    python3 artifact-miner.py --out results.json
    python3 artifact-miner.py --projects-dir ~/.claude/projects
"""
import argparse
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

EPHEMERAL_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
HOME = str(Path.home())

HEREDOC_RE = re.compile(r"cat\s*>\s*([^\s<]+)\s*<<")
TEE_RE = re.compile(r"\|\s*tee\s+(-a\s+)?([/~][^\s;&|]+)")
# both linked-worktree conventions seen in the corpus: .claude/worktrees/ and .worktrees/
WORKTREE_RE = re.compile(r"(.*?)/\.?(?:claude/)?worktrees?/[^/]+/(.*)")

SCRIPT_EXTS = (".py", ".sh", ".js", ".mjs", ".ts", ".sql", ".rb")


def classify(path: str) -> str:
    if path.startswith(EPHEMERAL_PREFIXES) or "/scratchpad" in path:
        return "ephemeral"
    if path.startswith(HOME + "/.claude/"):
        if "/projects/" in path:
            return "claude-internal"
        for kind in ("skills", "hooks", "commands", "agents"):
            if f"/.claude/{kind}/" in path:
                return f"claude-{kind}"
        return "claude-config"
    if "/.claude/worktrees/" in path or "/.worktrees/" in path or "/worktrees/" in path:
        return "worktree"
    if path.startswith(HOME + "/Workspace/"):
        return "workspace"
    if path.startswith(HOME):
        return "home-other"
    return "other"


def normalize_bash(cmd: str) -> str:
    """Reduce a bash command to a coarse procedure signature."""
    first = re.sub(r"\s+", " ", cmd.strip().split("\n")[0])[:120]
    tokens = first.split(" ")
    sig = []
    for t in tokens[:4]:
        if t.startswith(("-", "/", "~", '"', "'", "$", "http")) or "=" in t:
            break
        sig.append(t)
    return " ".join(sig) if sig else tokens[0][:40]


def worktree_to_repo(path: str) -> str | None:
    m = WORKTREE_RE.match(path)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def mine(projects_dir: Path) -> dict:
    writes = []
    edits_count = Counter()
    bash_sigs = Counter()
    bash_examples = {}
    bash_sig_projects = defaultdict(set)
    bash_file_creates = []
    per_project_writes = Counter()
    write_basename_projects = defaultdict(set)
    write_basename_counts = Counter()
    tool_counts = Counter()
    n_lines = 0
    n_files = 0

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        pname = proj_dir.name
        for jf in proj_dir.rglob("*.jsonl"):
            n_files += 1
            try:
                fh = open(jf, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    n_lines += 1
                    if '"tool_use"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    ts = (rec.get("timestamp") or "")[:10]
                    side = bool(rec.get("isSidechain"))
                    for block in (msg.get("content") or []):
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = block.get("name") or "?"
                        tool_counts[name] += 1
                        inp = block.get("input") or {}
                        if name in ("Write", "NotebookEdit"):
                            p = inp.get("file_path") or inp.get("notebook_path") or ""
                            if p:
                                writes.append((p, classify(p), pname, ts, side))
                                per_project_writes[pname] += 1
                                base = os.path.basename(p)
                                if base.endswith(SCRIPT_EXTS):
                                    write_basename_projects[base].add(pname)
                                    write_basename_counts[base] += 1
                        elif name == "Edit":
                            p = inp.get("file_path") or ""
                            if p:
                                edits_count[classify(p)] += 1
                        elif name == "Bash":
                            cmd = inp.get("command") or ""
                            if not cmd:
                                continue
                            sig = normalize_bash(cmd)
                            bash_sigs[sig] += 1
                            bash_examples.setdefault(sig, cmd[:200])
                            bash_sig_projects[sig].add(pname)
                            for m in HEREDOC_RE.finditer(cmd):
                                bash_file_creates.append((m.group(1), pname, ts))
                            for m in TEE_RE.finditer(cmd):
                                bash_file_creates.append((m.group(2), pname, ts))

    survival = defaultdict(Counter)
    unique_paths = {}
    for p, cat, pname, ts, side in writes:
        key = (p, cat)
        if key in unique_paths:
            continue
        unique_paths[key] = (pname, ts)
        if os.path.exists(p):
            survival[cat]["exists"] += 1
        else:
            alt = worktree_to_repo(p)
            if alt and os.path.exists(alt):
                survival[cat]["merged-to-repo"] += 1
            else:
                survival[cat]["gone"] += 1

    eph_scripts = Counter()
    eph_script_meta = defaultdict(set)
    for p, cat, pname, ts, side in writes:
        if cat == "ephemeral" and p.endswith(SCRIPT_EXTS):
            base = os.path.basename(p)
            eph_scripts[base] += 1
            eph_script_meta[base].add(pname.replace("-Users-nino-Workspace-dev-", ""))

    gone_workspace = [
        {"path": p, "project": pname, "date": ts}
        for (p, cat), (pname, ts) in unique_paths.items()
        if cat == "workspace" and not os.path.exists(p)
    ]

    claude_assets = sorted(
        {p for (p, cat) in unique_paths if cat.startswith("claude-") and cat != "claude-internal"}
    )

    return {
        "corpus": {"files": n_files, "lines": n_lines},
        "tool_counts": dict(tool_counts.most_common(30)),
        "write_categories": dict(Counter(cat for (_, cat) in unique_paths).most_common()),
        "edit_categories": dict(edits_count.most_common()),
        "survival": {k: dict(v) for k, v in survival.items()},
        "top_projects_by_writes": dict(per_project_writes.most_common(25)),
        "ephemeral_scripts_top": [
            {"name": k, "count": v, "projects": sorted(eph_script_meta[k])[:6]}
            for k, v in eph_scripts.most_common(80)
        ],
        "bash_top_signatures": [
            {"sig": s, "count": c, "projects": len(bash_sig_projects[s]),
             "project_names": sorted(x.replace("-Users-nino-Workspace-dev-", "") for x in bash_sig_projects[s])[:8],
             "example": bash_examples[s]}
            for s, c in bash_sigs.most_common(120)
        ],
        "cross_project_script_writes": [
            {"name": k, "count": write_basename_counts[k], "projects": sorted(x.replace("-Users-nino-Workspace-dev-", "") for x in v)[:10]}
            for k, v in sorted(write_basename_projects.items(), key=lambda kv: -len(kv[1]))
            if len(v) >= 2
        ][:60],
        "bash_file_creates_sample": bash_file_creates[:100],
        "gone_workspace_sample": gone_workspace[:200],
        "gone_workspace_total": len(gone_workspace),
        "claude_assets_written": claude_assets,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--projects-dir", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--out", default="artifact-mine.json")
    args = ap.parse_args()

    result = mine(Path(args.projects_dir).expanduser())
    out = Path(args.out)
    out.write_text(json.dumps(result, indent=1))
    total = sum(result["write_categories"].values())
    print(f"done: {result['corpus']['files']} transcripts, {result['corpus']['lines']} lines, {total} unique written paths")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
