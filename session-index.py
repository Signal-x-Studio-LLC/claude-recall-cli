#!/usr/bin/env python3
"""Permanent full-text index over agent conversation history.

The premise, measured: conversation text -- what you asked and what the agent
answered in prose -- is 3.8% of a session corpus. Tool results, tool-call
inputs and metadata are the other 96%. So the thing you actually interrogate
("what did I decide about X in May") fits in a few hundred MB and can be kept
forever, while the bulk stays expendable.

This index is deliberately NOT a backup. It answers "what happened, when, in
which session" and then hands you a session id. Full fidelity -- tool calls,
file contents, diffs -- lives in the cold tarballs, which you restore only once
the index has told you which one you want.

It reads live transcripts AND .tar.zst archives, so a month whose bytes have
rotated out of R2 still answers queries; rows carry `archived` so a hit can say
"this session's full transcript is gone, here is what was said."

    session-index.py build              # incremental, from live trees
    session-index.py build --from-archives
    session-index.py query "worktree guard"  [--project X] [--since 2026-05] [-n 20]
    session-index.py stats
"""
from __future__ import annotations
import argparse, glob, json, os, sqlite3, sys, subprocess, tarfile, tempfile

DB    = os.path.expanduser("~/.claude/session-index.db")
TREES = {"claude": "~/.claude/projects",
         "codex-archived": "~/.codex/archived_sessions",
         "codex-sessions": "~/.codex/sessions"}
ARCHIVE_DIR = os.path.expanduser("~/.claude/archived_sessions")
HISTORY = os.path.expanduser("~/.claude/history.jsonl")

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  session_id TEXT, project TEXT, tree TEXT,
  ts TEXT, role TEXT, text TEXT,
  source_file TEXT, archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_ts      ON turns(ts);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, mtime_ns INTEGER, turns INTEGER, indexed_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
  text, content='turns', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
  INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
  INSERT INTO turns_fts(turns_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
"""

MIN_CHARS = 12   # drop "ok", "go", "yes" -- they carry no retrieval signal

# Injected context, not conversation. Codex re-sends AGENTS.md on every session
# and Claude injects environment blocks; measured at ~100 MB of the 359 MB of
# raw text, repeated verbatim hundreds of times. Indexing it inflates the
# permanent tier and, worse, floods every query with boilerplate hits.
BOILERPLATE_PREFIXES = (
    "# AGENTS.md instructions",
    "<user_instructions>",
    "<environment_context>",
    "<INSTRUCTIONS>",
    "<system-reminder>",
    "# Repository Guidelines",
    "Caveat: The messages below were generated",
)

def is_boilerplate(t: str) -> bool:
    h = t.lstrip()[:120]
    return any(h.startswith(p) for p in BOILERPLATE_PREFIXES)

def connect():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    return c

def extract(path, fh=None):
    """Yield (session_id, project, ts, role, text) for conversation turns only.

    Codex rollouts carry identity ONCE, in a leading `session_meta` record, and
    never repeat it on individual messages. An earlier version used the message
    payload's `id` as the session id, which produced 380k distinct "sessions"
    that mapped to nothing and left `project` empty for the whole Codex corpus --
    defeating the point of this index, which is to hand you a session you can
    restore. Track session_meta as we stream and carry it forward.
    """
    opener = fh if fh is not None else open(path, errors="ignore")
    cx_sid = cx_cwd = ""
    with opener as f:
        for line in f:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "ignore")
            if '"content"' not in line and '"payload"' not in line \
               and '"session_meta"' not in line:
                continue
            try: o = json.loads(line)
            except Exception: continue

            if o.get("type") == "session_meta":
                mp = o.get("payload") or {}
                cx_sid = mp.get("id") or cx_sid
                cx_cwd = mp.get("cwd") or cx_cwd
                continue

            # Claude Code shape
            t = o.get("type")
            if t in ("user", "assistant"):
                m = o.get("message") or {}
                c = m.get("content")
                sid = o.get("sessionId") or ""
                proj = o.get("cwd") or ""
                ts = o.get("timestamp") or ""
                if isinstance(c, str):
                    if len(c) >= MIN_CHARS and not is_boilerplate(c):
                        yield sid, proj, ts, t, c
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = b.get("text") or ""
                            if len(txt) >= MIN_CHARS and not is_boilerplate(txt):
                                yield sid, proj, ts, t, txt
                continue

            # Codex rollout shape
            p = o.get("payload") or {}
            if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                ts = o.get("timestamp") or ""
                for b in (p.get("content") or []):
                    if not isinstance(b, dict): continue
                    if b.get("type") in ("input_text", "output_text", "text"):
                        txt = b.get("text") or ""
                        if len(txt) >= MIN_CHARS and not is_boilerplate(txt):
                            yield cx_sid, cx_cwd, ts, p["role"], txt

def index_file(conn, path, tree, archived=0, fh=None, label=None):
    key = label or path
    try: mt = os.stat(path).st_mtime_ns if fh is None else 0
    except OSError: return 0
    row = conn.execute("SELECT mtime_ns FROM files WHERE path=?", (key,)).fetchone()
    if row and row[0] == mt and mt != 0: return 0
    conn.execute("DELETE FROM turns WHERE source_file=?", (key,))
    n = 0
    for sid, proj, ts, role, text in extract(path, fh):
        conn.execute("INSERT INTO turns(session_id,project,tree,ts,role,text,source_file,archived)"
                     " VALUES(?,?,?,?,?,?,?,?)", (sid, proj, tree, ts, role, text, key, archived))
        n += 1
    conn.execute("INSERT OR REPLACE INTO files(path,mtime_ns,turns,indexed_at)"
                 " VALUES(?,?,?,datetime('now'))", (key, mt, n))
    return n

def index_history(conn):
    """~/.claude/history.jsonl is the only record that predates surviving
    transcripts -- 22,431 prompts from 2025-09 to 2026-03 whose sessions were
    deleted by cleanupPeriodDays while it was still at the 30-day default. It is
    8.7 MB and it is the deepest archaeology on the machine, so it belongs in the
    index even though it holds prompts only, with no assistant side."""
    import datetime
    if not os.path.exists(HISTORY): return 0
    mt = os.stat(HISTORY).st_mtime_ns
    row = conn.execute("SELECT mtime_ns FROM files WHERE path=?", (HISTORY,)).fetchone()
    if row and row[0] == mt: return 0
    conn.execute("DELETE FROM turns WHERE source_file=?", (HISTORY,))
    n = 0
    for line in open(HISTORY, errors="ignore"):
        try: o = json.loads(line)
        except Exception: continue
        txt = o.get("display") or o.get("prompt") or o.get("text") or ""
        if not isinstance(txt, str) or len(txt) < MIN_CHARS or is_boilerplate(txt):
            continue
        ts = o.get("timestamp")
        if isinstance(ts, (int, float)):
            v = ts / 1000 if ts > 1e11 else ts
            ts = datetime.datetime.fromtimestamp(v).isoformat(timespec="seconds")
        elif not isinstance(ts, str):
            ts = ""
        conn.execute("INSERT INTO turns(session_id,project,tree,ts,role,text,source_file,archived)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     ("", o.get("project") or o.get("cwd") or "", "history",
                      ts, "user", txt, HISTORY, 2))
        n += 1
    conn.execute("INSERT OR REPLACE INTO files(path,mtime_ns,turns,indexed_at)"
                 " VALUES(?,?,?,datetime('now'))", (HISTORY, mt, n))
    conn.commit()
    return n


def cmd_build(a):
    conn = connect(); total = files = 0
    if a.from_archives:
        for tar in sorted(glob.glob(ARCHIVE_DIR + "/**/*.tar.zst", recursive=True)):
            with tempfile.TemporaryDirectory() as td:
                tp = os.path.join(td, "a.tar")
                with open(tp, "wb") as w:
                    subprocess.run(["zstd", "-d", "-c", tar], stdout=w, check=True)
                with tarfile.open(tp) as tf:
                    for m in tf.getmembers():
                        if not m.name.endswith(".jsonl"): continue
                        ex = tf.extractfile(m)
                        if ex is None: continue
                        tree = "claude" if "/.claude/" in m.name else "codex"
                        n = index_file(conn, m.name, tree, archived=1, fh=ex,
                                       label=f"archive:{os.path.basename(tar)}:{m.name}")
                        total += n; files += 1 if n else 0
            conn.commit()
            print(f"  {os.path.basename(tar)}: indexed")
    else:
        for tree, root in TREES.items():
            for p in glob.glob(os.path.expanduser(root) + "/**/*.jsonl", recursive=True):
                n = index_file(conn, p, tree)
                if n: files += 1; total += n
            conn.commit()
    h = index_history(conn)
    if h: print(f"  indexed {h:,} prompts from history.jsonl (pre-transcript era)")
    total += h
    conn.execute("INSERT INTO turns_fts(turns_fts) VALUES('optimize')")
    conn.commit()
    print(f"  indexed {total:,} turns from {files:,} changed file(s)")
    print(f"  db: {DB} ({os.path.getsize(DB)/1e6:.1f} MB)")
    return 0

def cmd_query(a):
    conn = connect()
    sql = ("SELECT t.ts, t.role, t.session_id, t.project, t.archived,"
           " snippet(turns_fts, 0, '>>>', '<<<', ' … ', 18) "
           "FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid "
           "WHERE turns_fts MATCH ?")
    args = [a.q]
    if a.project: sql += " AND t.project LIKE ?"; args.append(f"%{a.project}%")
    if a.since:   sql += " AND t.ts >= ?";        args.append(a.since)
    sql += " ORDER BY bm25(turns_fts) LIMIT ?"; args.append(a.n)
    rows = conn.execute(sql, args).fetchall()
    if not rows: print("  no matches"); return 0
    for ts, role, sid, proj, arch, snip in rows:
        tag = {0: "", 1: " [ARCHIVED — restore the tarball for full detail]",
               2: " [PROMPT-ONLY — transcript no longer exists]"}[arch]
        print(f"\n  {ts[:19]}  {role:9} {os.path.basename(proj or '?')}  session {sid[:8]}{tag}")
        print(f"    {' '.join(snip.split())}")
    print(f"\n  {len(rows)} hit(s)")
    return 0

def cmd_stats(a):
    conn = connect()
    q = lambda s: conn.execute(s).fetchone()
    print(f"  db size        {os.path.getsize(DB)/1e6:8.1f} MB")
    print(f"  turns          {q('SELECT count(*) FROM turns')[0]:>8,}")
    print(f"  sessions       {q('SELECT count(DISTINCT session_id) FROM turns')[0]:>8,}")
    print(f"  files indexed  {q('SELECT count(*) FROM files')[0]:>8,}")
    r = q("SELECT min(ts), max(ts) FROM turns WHERE ts != ''")
    print(f"  range          {(r[0] or '?')[:10]} .. {(r[1] or '?')[:10]}")
    for tree, n in conn.execute("SELECT tree, count(*) FROM turns GROUP BY tree"):
        print(f"    {tree:16} {n:>8,}")
    return 0

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build"); b.add_argument("--from-archives", action="store_true")
    qp = sub.add_parser("query"); qp.add_argument("q"); qp.add_argument("-n", type=int, default=10)
    qp.add_argument("--project"); qp.add_argument("--since")
    sub.add_parser("stats")
    a = ap.parse_args()
    return {"build": cmd_build, "query": cmd_query, "stats": cmd_stats}.get(a.cmd or "stats",
            cmd_stats)(a)

if __name__ == "__main__":
    sys.exit(main())
