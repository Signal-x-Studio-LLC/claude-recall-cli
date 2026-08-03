#!/usr/bin/env python3
"""Recall CLI — save and search reusable session entries.

Storage: ~/.claude/recall.db (SQLite + FTS5)
Schema is QuantifAI-compatible via session_id foreign key.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "recall.db"

# Heuristic version — bump when scoring rules, patterns, or thresholds change.
# Stored alongside quality reports so scores are comparable only within
# the same version.
HEURISTIC_VERSION = 4

# Config file locations — co-located with this script
_SCRIPT_DIR = Path(__file__).resolve().parent
BASELINE_PATH = _SCRIPT_DIR / "baseline.json"
THRESHOLDS_PATH = _SCRIPT_DIR / "thresholds.json"

# Cached config (loaded once per invocation)
_baseline = None
_thresholds = None


def _load_baseline() -> dict:
    """Load compliance baseline derived from Claude Code system prompt."""
    global _baseline
    if _baseline is None:
        try:
            with open(BASELINE_PATH) as f:
                _baseline = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _baseline = {}
    return _baseline


def _load_thresholds() -> dict:
    """Load user-tunable efficiency thresholds."""
    global _thresholds
    if _thresholds is None:
        try:
            with open(THRESHOLDS_PATH) as f:
                _thresholds = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _thresholds = {}
    return _thresholds


def get_db() -> sqlite3.Connection:
    """Get or create the recall database."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection):
    """Apply schema migrations."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    row = db.execute("SELECT version FROM schema_version").fetchone()
    version = row["version"] if row else 0

    if version < 1:
        db.executescript("""
            CREATE TABLE recipes (
                id              TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                source_client   TEXT NOT NULL DEFAULT 'claude',
                project_path    TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                intent          TEXT NOT NULL,
                sources         TEXT,
                key_commands    TEXT,
                outcome         TEXT,
                prompt_template TEXT,
                tags            TEXT,
                quality_class   TEXT CHECK(quality_class IN
                                ('high_value', 'productive', 'neutral',
                                 'churn', 'dead_end')),
                quality_reason  TEXT,
                est_cost        REAL,
                token_count     INTEGER,
                outcome_ratio   REAL
            );

            CREATE VIRTUAL TABLE recipes_fts USING fts5(
                intent, outcome, prompt_template, tags,
                content=recipes, content_rowid=rowid
            );

            CREATE TRIGGER recipes_ai AFTER INSERT ON recipes BEGIN
                INSERT INTO recipes_fts(rowid, intent, outcome, prompt_template, tags)
                VALUES (new.rowid, new.intent, new.outcome, new.prompt_template, new.tags);
            END;

            CREATE TRIGGER recipes_au AFTER UPDATE ON recipes BEGIN
                INSERT INTO recipes_fts(recipes_fts, rowid, intent, outcome, prompt_template, tags)
                VALUES ('delete', old.rowid, old.intent, old.outcome, old.prompt_template, old.tags);
                INSERT INTO recipes_fts(rowid, intent, outcome, prompt_template, tags)
                VALUES (new.rowid, new.intent, new.outcome, new.prompt_template, new.tags);
            END;

            CREATE TRIGGER recipes_ad AFTER DELETE ON recipes BEGIN
                INSERT INTO recipes_fts(recipes_fts, rowid, intent, outcome, prompt_template, tags)
                VALUES ('delete', old.rowid, old.intent, old.outcome, old.prompt_template, old.tags);
            END;
        """)
        if version == 0:
            db.execute("INSERT INTO schema_version (version) VALUES (1)")
        else:
            db.execute("UPDATE schema_version SET version = 1")
        db.commit()
        version = 1

    if version < 2:
        # Add outcome tracking for quality correlation
        db.executescript("""
            ALTER TABLE recipes ADD COLUMN outcome_verified INTEGER DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN had_followup_fix INTEGER DEFAULT NULL;
        """)
        db.execute("UPDATE schema_version SET version = 2")
        db.commit()
        version = 2

    if version < 3:
        # Store analysis metrics alongside entries for future correlation
        db.executescript("""
            ALTER TABLE recipes ADD COLUMN commits_produced INTEGER DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN user_satisfaction INTEGER DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN compliance_grade TEXT DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN compliance_score REAL DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN process_score REAL DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN session_shape TEXT DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN thrash_ratio REAL DEFAULT NULL;
            ALTER TABLE recipes ADD COLUMN tokens_per_output INTEGER DEFAULT NULL;
        """)
        db.execute("UPDATE schema_version SET version = 3")
        db.commit()
        version = 3

    if version < 4:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS session_features (
                session_id          TEXT PRIMARY KEY,
                source_client       TEXT NOT NULL DEFAULT 'claude',
                project_path        TEXT,
                analyzed_at         TEXT DEFAULT (datetime('now')),
                compliance_score    REAL,
                compliance_grade    TEXT,
                process_score       REAL,
                session_shape       TEXT,
                thrash_ratio        REAL,
                tokens_per_output   INTEGER,
                total_tokens        INTEGER,
                total_cost          REAL,
                tool_misuses        INTEGER,
                anti_pattern_count  INTEGER,
                edit_count          INTEGER,
                unique_files        INTEGER,
                prompt_count        INTEGER,
                model_primary       TEXT,
                commits_produced    INTEGER,
                had_error_exit      INTEGER,
                outcome_pass        INTEGER DEFAULT NULL,
                had_followup_fix    INTEGER DEFAULT NULL,
                file_size_kb        INTEGER,
                -- v4 features
                late_error_rate     REAL,
                duration_min        REAL,
                model_switches      INTEGER,
                focused_thrash      INTEGER
            );
        """)
        db.execute("UPDATE schema_version SET version = 4")
        db.commit()
        version = 4

    if version < 5:
        # Add new feature columns (safe if they already exist from v4 CREATE)
        for col, typ in [
            ("late_error_rate", "REAL"),
            ("duration_min", "REAL"),
            ("model_switches", "INTEGER"),
            ("focused_thrash", "INTEGER"),
        ]:
            try:
                db.execute(f"ALTER TABLE session_features ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # Column already exists from v4 CREATE
        db.execute("UPDATE schema_version SET version = 5")
        db.commit()
        version = 5

    if version < 6:
        for table in ("recipes", "session_features"):
            columns = {
                row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "source_client" not in columns:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN "
                    "source_client TEXT NOT NULL DEFAULT 'claude'"
                )
        db.execute("UPDATE schema_version SET version = 6")
        db.commit()


def cmd_save(args):
    """Save an entry to the database with auto-populated analysis metrics."""
    db = get_db()
    recipe_id = str(uuid.uuid4())[:8]

    # Calculate token count and cost from session if available
    token_count = None
    est_cost = None
    compliance_grade = None
    compliance_score_val = None
    process_score_val = None
    session_shape_val = None
    thrash_ratio_val = None
    tokens_per_output_val = None
    commits = None

    session_file = _find_session_file(
        args.session_id, args.project, source_client=args.source_client
    )
    if session_file and args.source_client == "claude":
        token_count, est_cost = _extract_session_cost(session_file)

        # Auto-populate analysis metrics for future correlation
        analysis = _run_analysis(session_file)
        if "error" not in analysis:
            compliance_grade = analysis["compliance"]["grade"]
            compliance_score_val = analysis["compliance"]["score"]
            process_score_val = analysis["process"]["score"]
            session_shape_val = analysis["session_shape"]["session_shape"]
            thrash_ratio_val = analysis["thrash_analysis"]["thrash_ratio"]
            tokens_per_output_val = analysis["cost_efficiency"]["tokens_per_output"]

        # Count git commits in session
        commits = _count_commits(session_file)

    db.execute(
        """INSERT INTO recipes
           (id, session_id, source_client, project_path, intent, sources, key_commands,
            outcome, prompt_template, tags, quality_class, quality_reason,
            est_cost, token_count, compliance_grade, compliance_score,
            process_score, session_shape, thrash_ratio, tokens_per_output,
            commits_produced)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            recipe_id,
            args.session_id,
            args.source_client,
            args.project,
            args.intent,
            args.sources,
            args.key_commands,
            args.outcome,
            args.prompt_template,
            args.tags,
            args.quality_class,
            args.quality_reason,
            est_cost,
            token_count,
            compliance_grade,
            compliance_score_val,
            process_score_val,
            session_shape_val,
            thrash_ratio_val,
            tokens_per_output_val,
            commits,
        ),
    )
    db.commit()
    db.close()

    print(json.dumps({
        "status": "saved",
        "id": recipe_id,
        "source_client": args.source_client,
        "intent": args.intent,
        "quality_class": args.quality_class,
        "compliance_grade": compliance_grade,
        "process_score": process_score_val,
        "session_shape": session_shape_val,
        "est_cost": est_cost,
        "token_count": token_count,
        "commits_produced": commits,
        "tip": f"Rate this session: /recall verify {recipe_id} --outcome pass|fail",
    }, indent=2))


def cmd_find(args):
    """Search entries by keyword using FTS5."""
    db = get_db()
    # Quote each term for FTS5 safety (hyphens, special chars)
    terms = args.query.split()
    # Use OR between terms for broader recall, AND is too strict
    query = " OR ".join(f'"{t}"' for t in terms)

    rows = db.execute(
        """SELECT r.id, r.intent, r.quality_class, r.tags, r.created_at,
                  r.source_client,
                  r.project_path, r.est_cost,
                  snippet(recipes_fts, 0, '>>>', '<<<', '...', 30) as match_snippet
           FROM recipes_fts
           JOIN recipes r ON r.rowid = recipes_fts.rowid
           WHERE recipes_fts MATCH ?
           ORDER BY rank
           LIMIT 10""",
        (query,),
    ).fetchall()

    if not rows:
        print(json.dumps({"results": [], "query": query, "message": "No recipes found"}))
    else:
        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "source_client": r["source_client"],
                "intent": r["intent"],
                "quality": r["quality_class"],
                "tags": r["tags"],
                "project": r["project_path"],
                "cost": r["est_cost"],
                "created": r["created_at"],
                "match": r["match_snippet"],
            })
        print(json.dumps({"results": results, "query": query}, indent=2))

    db.close()


def cmd_list(args):
    """List recent entries."""
    db = get_db()
    limit = getattr(args, "limit", 20)

    rows = db.execute(
        """SELECT id, intent, quality_class, tags, created_at, source_client,
                  project_path, est_cost
           FROM recipes
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "source_client": r["source_client"],
            "intent": r["intent"],
            "quality": r["quality_class"],
            "tags": r["tags"],
            "project": r["project_path"],
            "cost": r["est_cost"],
            "created": r["created_at"],
        })

    print(json.dumps({"recipes": results, "count": len(results)}, indent=2))
    db.close()


def cmd_show(args):
    """Show full entry details."""
    db = get_db()

    row = db.execute(
        "SELECT * FROM recipes WHERE id = ? OR id LIKE ?",
        (args.id, f"{args.id}%"),
    ).fetchone()

    if not row:
        print(json.dumps({"error": f"Entry '{args.id}' not found"}))
        sys.exit(1)

    recipe = dict(row)
    # Parse JSON fields for display
    for field in ("sources", "key_commands", "tags"):
        if recipe.get(field):
            try:
                recipe[field] = json.loads(recipe[field])
            except (json.JSONDecodeError, TypeError):
                pass

    print(json.dumps(recipe, indent=2))
    db.close()


def cmd_use(args):
    """Output an entry's prompt template ready to paste, with variable hints."""
    db = get_db()

    row = db.execute(
        "SELECT id, intent, prompt_template, sources, key_commands, outcome, tags "
        "FROM recipes WHERE id = ? OR id LIKE ?",
        (args.id, f"{args.id}%"),
    ).fetchone()

    if not row:
        print(json.dumps({"error": f"Entry '{args.id}' not found"}))
        sys.exit(1)

    recipe = dict(row)

    # Parse template for variables, unescape literal \n
    template = (recipe.get("prompt_template") or "").replace("\\n", "\n")
    variables = re.findall(r"\{\{(\w+)\}\}", template)

    output = {
        "id": recipe["id"],
        "intent": recipe["intent"],
        "prompt_template": template,
        "variables": variables,
        "sources": recipe.get("sources"),
        "key_steps": recipe.get("key_commands"),
    }

    # If user provided variable values, substitute them
    if hasattr(args, "vars") and args.vars:
        filled = template
        for var_assignment in args.vars:
            if "=" in var_assignment:
                key, val = var_assignment.split("=", 1)
                filled = filled.replace("{{" + key + "}}", val)
        output["filled_prompt"] = filled

    print(json.dumps(output, indent=2))
    db.close()


def cmd_stats(args):
    """Show recall library statistics."""
    db = get_db()

    total = db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    by_quality = db.execute(
        "SELECT quality_class, COUNT(*) as cnt FROM recipes GROUP BY quality_class ORDER BY cnt DESC"
    ).fetchall()
    by_project = db.execute(
        "SELECT project_path, COUNT(*) as cnt FROM recipes GROUP BY project_path ORDER BY cnt DESC"
    ).fetchall()
    total_cost = db.execute(
        "SELECT SUM(est_cost) FROM recipes WHERE est_cost IS NOT NULL"
    ).fetchone()[0]
    total_tokens = db.execute(
        "SELECT SUM(token_count) FROM recipes WHERE token_count IS NOT NULL"
    ).fetchone()[0]

    # Tag frequency
    all_tags = []
    rows = db.execute("SELECT tags FROM recipes WHERE tags IS NOT NULL").fetchall()
    for r in rows:
        try:
            all_tags.extend(json.loads(r[0]))
        except (json.JSONDecodeError, TypeError):
            pass

    tag_freq = Counter(all_tags).most_common(10)

    output = {
        "total_recipes": total,
        "by_quality": {r[0]: r[1] for r in by_quality},
        "by_project": {r[0].split("/")[-1] if r[0] else "unknown": r[1] for r in by_project[:5]},
        "total_tracked_cost": round(total_cost, 4) if total_cost else 0,
        "total_tracked_tokens": total_tokens or 0,
        "top_tags": tag_freq,
        "db_path": str(DB_PATH),
    }

    print(json.dumps(output, indent=2))
    db.close()


# ---------------------------------------------------------------------------
# Session quality analysis
#
# Two layers:
#   1. COMPLIANCE — did Claude follow its own system prompt rules?
#      Sourced from baseline.json (derived from Anthropic's published prompt).
#   2. EFFICIENCY — did it do so within acceptable cost/effort bounds?
#      Sourced from thresholds.json (user-tunable, subjective).
# ---------------------------------------------------------------------------


def _parse_session_events(session_file: Path) -> list[dict]:
    """Parse a session JSONL into a list of typed events."""
    events = []
    try:
        with open(session_file) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return events


def _analyze_tool_selection(events: list[dict]) -> dict:
    """COMPLIANCE: Check for Bash commands that should have used dedicated tools.
    Rules sourced from baseline.json → tool_selection."""
    baseline = _load_baseline()
    thresholds = _load_thresholds()

    ts = baseline.get("tool_selection", {})
    misuse_patterns = [
        (p["bash_pattern"], p["expected_tool"], p.get("prompt_rule", ""))
        for p in ts.get("misuse_patterns", [])
    ]
    bash_ok = tuple(ts.get("bash_ok_prefixes", []))
    penalty = thresholds.get("compliance_penalty_per_misuse", 15)

    misuses = []
    bash_count = 0

    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            bash_count += 1
            cmd = block.get("input", {}).get("command", "").strip()

            if any(cmd.startswith(p) for p in bash_ok):
                continue

            for pattern, better_tool, rule in misuse_patterns:
                if re.search(pattern, cmd):
                    misuses.append({
                        "command": cmd[:120],
                        "should_use": better_tool,
                        "rule": rule,
                    })
                    break

    # Normalize: misuse RATE, not raw count
    # A 50-bash session with 3 misuses (6%) is better than
    # a 5-bash session with 3 misuses (60%)
    eligible_bash = bash_count  # total Bash calls checked
    misuse_rate = (len(misuses) / eligible_bash * 100) if eligible_bash > 0 else 0.0

    # Score based on rate: 0% = 100, scales down
    # penalty_per_misuse is repurposed as "penalty per 10% misuse rate"
    rate_penalty = (misuse_rate / 10) * penalty
    score = max(0, round(100 - rate_penalty))

    return {
        "layer": "compliance",
        "bash_calls": bash_count,
        "misuses": misuses,
        "misuse_count": len(misuses),
        "misuse_rate_pct": round(misuse_rate, 1),
        "score": score,
    }


def _analyze_thrash(events: list[dict]) -> dict:
    """EFFICIENCY: Detect re-edits of the same file (planning vs thrashing).
    Thresholds sourced from thresholds.json → thrash."""
    t = _load_thresholds().get("thrash", {})
    penalties = t.get("penalty_per_tier", {})

    edit_targets = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            if block.get("name") in ("Write", "Edit"):
                fp = block.get("input", {}).get("file_path", "")
                if fp:
                    edit_targets.append(fp)

    if not edit_targets:
        return {"layer": "process", "edits": 0, "unique_files": 0,
                "re_edits": {}, "thrash_ratio": 0.0, "score": 100}

    counts = Counter(edit_targets)
    re_edits = {f: c for f, c in counts.items() if c > 2}
    unique = len(counts)
    total = len(edit_targets)
    thrash_ratio = round(total / unique, 2) if unique > 0 else 0.0

    score = 100
    if thrash_ratio > t.get("ratio_critical", 3.0):
        score -= penalties.get("critical", 40)
    elif thrash_ratio > t.get("ratio_high", 2.0):
        score -= penalties.get("high", 20)
    elif thrash_ratio > t.get("ratio_warning", 1.5):
        score -= penalties.get("warning", 10)

    for f, c in re_edits.items():
        if c >= t.get("single_file_max", 5):
            score -= penalties.get("single_file_over_max", 10)

    return {
        "layer": "process",
        "edits": total,
        "unique_files": unique,
        "re_edits": {Path(f).name: c for f, c in re_edits.items()},
        "thrash_ratio": thrash_ratio,
        "score": max(0, score),
    }


def _analyze_prompt_clarity(events: list[dict]) -> dict:
    """PROCESS METRIC: Measure session structure — exploration vs output.

    NOT a quality score. Discussion-heavy sessions (architecture, planning,
    debugging) are legitimate and should not be penalized. This metric
    describes the session shape, not its quality.

    Thresholds sourced from thresholds.json → prompt_clarity."""
    t = _load_thresholds().get("prompt_clarity", {})

    user_msgs_before_output = 0
    total_user_msgs = 0
    found_output = False
    # Detect if pre-output phase included research tools (Read/Grep/Glob)
    # which signals deliberate exploration, not unclear prompting
    research_before_output = 0

    for ev in events:
        if ev.get("type") == "user":
            total_user_msgs += 1
            if not found_output:
                user_msgs_before_output += 1

        if ev.get("type") == "assistant":
            content = ev.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                if not found_output and block.get("name") in ("Read", "Grep", "Glob", "Agent"):
                    research_before_output += 1
                if block.get("name") in ("Write", "Edit"):
                    found_output = True

    # Classify session shape rather than scoring quality
    if not found_output:
        shape = "exploration_only"
    elif user_msgs_before_output <= t.get("perfect_prompts", 1):
        shape = "direct_execution"
    elif user_msgs_before_output <= t.get("ok_prompts", 4):
        shape = "brief_alignment"
    elif research_before_output >= 5:
        shape = "research_then_build"  # deliberate — not a clarity problem
    else:
        warmup_ratio = (user_msgs_before_output / total_user_msgs
                        if total_user_msgs > 0 else 1.0)
        if warmup_ratio < t.get("warmup_ratio_ok", 0.30):
            shape = "extended_discussion"
        else:
            shape = "late_start"

    # Process score — descriptive, not judgmental
    # research_then_build and direct_execution both score well
    shape_scores = {
        "direct_execution": 100,
        "brief_alignment": 85,
        "research_then_build": 80,  # deliberate exploration is fine
        "extended_discussion": 60,
        "late_start": 40,
        "exploration_only": t.get("no_output_score", 30),
    }
    score = shape_scores.get(shape, 50)

    return {
        "layer": "process",
        "session_shape": shape,
        "prompts_before_output": user_msgs_before_output,
        "research_calls_before_output": research_before_output,
        "total_prompts": total_user_msgs,
        "had_output": found_output,
        "score": score,
    }


def _analyze_cost_efficiency(events: list[dict]) -> dict:
    """EFFICIENCY: Tokens per productive tool call (Write/Edit/Bash).
    Thresholds sourced from thresholds.json → cost_efficiency."""
    t = _load_thresholds().get("cost_efficiency", {})
    model_costs = t.get("model_costs_per_million", {
        "claude-opus-4-6": 0.015,
        "claude-sonnet-4-6": 0.003,
        "claude-haiku-4-5": 0.0008,
    })

    total_tokens = 0
    productive_calls = 0
    total_cost = 0.0

    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message", {})
        if not isinstance(msg, dict):
            continue

        usage = msg.get("usage", {})
        model = msg.get("model", "")
        tokens = (usage.get("input_tokens", 0)
                  + usage.get("output_tokens", 0)
                  + usage.get("cache_read_input_tokens", 0))
        total_tokens += tokens

        rate = 0.003
        for model_key, model_rate in model_costs.items():
            if model_key in model:
                rate = model_rate
                break
        total_cost += tokens * rate / 1_000_000

        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("name") in ("Write", "Edit", "Bash"):
                    productive_calls += 1

    tokens_per_output = (
        round(total_tokens / productive_calls)
        if productive_calls > 0 else None
    )

    if tokens_per_output is None:
        score = t.get("no_output_score", 30)
    elif tokens_per_output < t.get("excellent", 3000):
        score = 100
    elif tokens_per_output < t.get("good", 8000):
        score = 80
    elif tokens_per_output < t.get("moderate", 20000):
        score = 60
    elif tokens_per_output < t.get("poor", 50000):
        score = 40
    else:
        score = 20

    return {
        "layer": "process",
        "total_tokens": total_tokens,
        "est_cost": round(total_cost, 4),
        "productive_calls": productive_calls,
        "tokens_per_output": tokens_per_output,
        "score": score,
    }


def _analyze_antipatterns(events: list[dict]) -> dict:
    """COMPLIANCE: Detect behavioral anti-patterns defined in system prompt.
    Rules sourced from baseline.json → anti_patterns."""
    baseline = _load_baseline()
    thresholds = _load_thresholds()
    severity_weights = thresholds.get("anti_pattern_severity_weights",
                                      {"high": 25, "medium": 15, "low": 5})

    rules = {r["id"]: r for r in baseline.get("anti_patterns", {}).get("rules", [])}
    issues = []

    # Extract all tool calls in order
    tool_calls = []
    bash_cmds = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            inp = block.get("input", {})
            tool_calls.append({"name": name, "input": inp})
            if name == "Bash":
                bash_cmds.append(inp.get("command", ""))

    # 1. Repeated identical Bash commands (no_retry_loops)
    rule = rules.get("no_retry_loops", {})
    threshold = rule.get("threshold", 3)
    bash_counts = Counter(bash_cmds)
    repeated = {c[:80]: n for c, n in bash_counts.items() if n >= threshold and c}
    if repeated:
        issues.append({
            "type": "repeated_commands",
            "rule": rule.get("prompt_rule", ""),
            "detail": f"{len(repeated)} commands run {threshold}+ times",
            "examples": list(repeated.keys())[:3],
            "severity": rule.get("severity", "high"),
        })

    # 2. Long read sequences with no output (no_brute_force)
    rule = rules.get("no_brute_force", {})
    threshold = rule.get("threshold", 15)
    consecutive_reads = 0
    max_reads_without_output = 0
    for tc in tool_calls:
        if tc["name"] in ("Read", "Grep", "Glob"):
            consecutive_reads += 1
        elif tc["name"] in ("Write", "Edit", "Bash"):
            max_reads_without_output = max(max_reads_without_output, consecutive_reads)
            consecutive_reads = 0
    max_reads_without_output = max(max_reads_without_output, consecutive_reads)

    if max_reads_without_output >= threshold:
        issues.append({
            "type": "exploration_dead_end",
            "rule": rule.get("prompt_rule", ""),
            "detail": f"{max_reads_without_output} consecutive reads without output",
            "severity": rule.get("severity", "medium"),
        })

    # 3. Excessive agent spawns (no_excessive_agents)
    rule = rules.get("no_excessive_agents", {})
    threshold = rule.get("threshold", 8)
    agent_count = sum(1 for tc in tool_calls if tc["name"] == "Agent")
    if agent_count >= threshold:
        issues.append({
            "type": "excessive_agents",
            "rule": rule.get("prompt_rule", ""),
            "detail": f"{agent_count} sub-agents spawned",
            "severity": rule.get("severity", "low"),
        })

    # 4. Edit without prior Read (read_before_edit)
    rule = rules.get("read_before_edit", {})
    if rule:
        files_read = set()
        edits_without_read = []
        for tc in tool_calls:
            if tc["name"] == "Read":
                fp = tc["input"].get("file_path", "")
                if fp:
                    files_read.add(fp)
            elif tc["name"] == "Edit":
                fp = tc["input"].get("file_path", "")
                if fp and fp not in files_read:
                    edits_without_read.append(Path(fp).name)
        if edits_without_read:
            issues.append({
                "type": "edit_without_read",
                "rule": rule.get("prompt_rule", ""),
                "detail": f"{len(edits_without_read)} files edited without reading first",
                "examples": edits_without_read[:5],
                "severity": rule.get("severity", "medium"),
            })

    score = 100 - sum(
        severity_weights.get(i["severity"], 0) for i in issues
    )

    return {
        "layer": "compliance",
        "issues": issues,
        "issue_count": len(issues),
        "score": max(0, score),
    }


def _run_analysis(session_file: Path) -> dict:
    """Run all analysis passes on a session file.

    Returns two distinct layers:
      - compliance: graded (A-F). Did Claude follow its documented rules?
        Sourced from baseline.json. Objective, binary checks.
      - process: ungraded descriptive metrics. How did the session behave?
        Sourced from thresholds.json. Useful for spotting outliers,
        NOT for judging session quality.

    Only compliance gets a letter grade because it has ground truth
    (documented rules with right/wrong answers). Process metrics describe
    behavior without claiming good or bad — task complexity, model choice,
    and session intent all affect these numbers legitimately.
    """
    events = _parse_session_events(session_file)
    if not events:
        return {"error": "No events found in session file"}

    tool_sel = _analyze_tool_selection(events)
    thrash = _analyze_thrash(events)
    clarity = _analyze_prompt_clarity(events)
    cost = _analyze_cost_efficiency(events)
    anti = _analyze_antipatterns(events)

    compliance_scores = {
        "tool_selection": tool_sel["score"],
        "anti_patterns": anti["score"],
    }
    process_scores = {
        "planning": thrash["score"],
        "session_shape": clarity["score"],
        "cost_efficiency": cost["score"],
    }
    compliance_avg = round(sum(compliance_scores.values()) / len(compliance_scores), 1)
    process_avg = round(sum(process_scores.values()) / len(process_scores), 1)

    # Only compliance gets a letter grade — it has ground truth
    grades = _load_thresholds().get("grades", {"A": 85, "B": 70, "C": 55, "D": 40})
    if compliance_avg >= grades.get("A", 85):
        compliance_grade = "A"
    elif compliance_avg >= grades.get("B", 70):
        compliance_grade = "B"
    elif compliance_avg >= grades.get("C", 55):
        compliance_grade = "C"
    elif compliance_avg >= grades.get("D", 40):
        compliance_grade = "D"
    else:
        compliance_grade = "F"

    return {
        "heuristic_version": HEURISTIC_VERSION,
        "baseline_source": "Claude Code system prompt (via baseline.json)",
        "compliance": {
            "grade": compliance_grade,
            "score": compliance_avg,
            "scores": compliance_scores,
        },
        "process": {
            "score": process_avg,
            "scores": process_scores,
            "note": "Descriptive metrics, not quality judgment. Affected by task complexity, model choice, and session intent.",
        },
        "tool_selection": tool_sel,
        "thrash_analysis": thrash,
        "session_shape": clarity,
        "cost_efficiency": cost,
        "anti_patterns": anti,
    }


def cmd_analyze(args):
    """Analyze a session for quality patterns."""
    session_file = None

    if args.session_id:
        session_file = _find_session_file(args.session_id)
    elif args.file:
        session_file = Path(args.file)

    if not session_file or not session_file.exists():
        print(json.dumps({"error": "Session file not found"}))
        sys.exit(1)

    result = _run_analysis(session_file)
    result["session_id"] = session_file.stem
    print(json.dumps(result, indent=2))


def cmd_quality(args):
    """Generate quality trends across recent sessions."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        print(json.dumps({"error": "No projects directory found"}))
        sys.exit(1)

    days = int(args.days) if args.days != "all" else None
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=days)
        if days else None
    )

    sessions = []
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        project_name = str(proj_dir.name).replace("-Users-nino-", "").replace("-", "/")

        for jf in proj_dir.glob("*.jsonl"):
            if cutoff:
                mtime = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue

            # Skip tiny sessions
            if jf.stat().st_size < 5000:
                continue

            result = _run_analysis(jf)
            if "error" in result:
                continue

            sessions.append({
                "session_id": jf.stem,
                "project": project_name,
                "date": datetime.fromtimestamp(
                    jf.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "compliance_grade": result["compliance"]["grade"],
                "compliance_score": result["compliance"]["score"],
                "process_score": result["process"]["score"],
                "tokens": result["cost_efficiency"]["total_tokens"],
                "cost": result["cost_efficiency"]["est_cost"],
                "misuses": result["tool_selection"]["misuse_count"],
                "thrash_ratio": result["thrash_analysis"]["thrash_ratio"],
                "session_shape": result["session_shape"]["session_shape"],
                "issues": result["anti_patterns"]["issue_count"],
            })

    if not sessions:
        print(json.dumps({"error": "No sessions found in timeframe"}))
        sys.exit(1)

    sessions.sort(key=lambda s: s["date"], reverse=True)
    sessions = sessions[:args.limit]

    n = len(sessions)
    total_cost = sum(s["cost"] for s in sessions)
    total_tokens = sum(s["tokens"] for s in sessions)

    # Compliance aggregates (graded)
    avg_compliance = round(sum(s["compliance_score"] for s in sessions) / n, 1)
    compliance_grades = Counter(s["compliance_grade"] for s in sessions)

    # Process aggregates (descriptive, not graded)
    avg_process = round(sum(s["process_score"] for s in sessions) / n, 1)
    shape_dist = Counter(s["session_shape"] for s in sessions)

    # Lowest compliance sessions (these are actionable)
    worst_compliance = sorted(sessions, key=lambda s: s["compliance_score"])[:5]

    output = {
        "summary": {
            "heuristic_version": HEURISTIC_VERSION,
            "baseline_source": "Claude Code system prompt (via baseline.json)",
            "sessions_analyzed": n,
            "days": args.days,
            "total_cost": round(total_cost, 4),
            "total_tokens": total_tokens,
        },
        "compliance": {
            "note": "Graded. Did Claude follow its documented system prompt rules?",
            "avg_score": avg_compliance,
            "grade_distribution": dict(compliance_grades.most_common()),
        },
        "process": {
            "note": "Descriptive only. Affected by task complexity, model choice, and session intent. Not a quality judgment.",
            "avg_score": avg_process,
            "session_shapes": dict(shape_dist.most_common()),
        },
        "worst_compliance": [
            {
                "session_id": s["session_id"][:12],
                "project": s["project"].split("/")[-1],
                "date": s["date"],
                "compliance": s["compliance_grade"],
                "score": s["compliance_score"],
                "misuses": s["misuses"],
                "issues": s["issues"],
            }
            for s in worst_compliance
        ],
        "recent": [
            {
                "session_id": s["session_id"][:12],
                "project": s["project"].split("/")[-1],
                "date": s["date"],
                "compliance": s["compliance_grade"],
                "process": s["process_score"],
                "shape": s["session_shape"],
            }
            for s in sessions[:10]
        ],
    }

    print(json.dumps(output, indent=2))


def _count_commits(session_file: Path) -> int:
    """Count git commit commands in a session."""
    count = 0
    try:
        with open(session_file) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                content = obj.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (block.get("type") == "tool_use"
                            and block.get("name") == "Bash"
                            and "git commit" in block.get("input", {}).get("command", "")):
                        count += 1
    except Exception:
        pass
    return count


def cmd_verify(args):
    """Mark a saved entry with outcome data for quality correlation."""
    db = get_db()

    row = db.execute(
        "SELECT id, intent, outcome_verified, user_satisfaction FROM recipes "
        "WHERE id = ? OR id LIKE ?",
        (args.id, f"{args.id}%"),
    ).fetchone()

    if not row:
        print(json.dumps({"error": f"Entry '{args.id}' not found"}))
        sys.exit(1)

    entry = dict(row)
    updates = {}

    if args.outcome is not None:
        updates["outcome_verified"] = 1 if args.outcome == "pass" else 0
    if args.satisfaction is not None:
        updates["user_satisfaction"] = args.satisfaction
    if args.followup is not None:
        updates["had_followup_fix"] = 1 if args.followup == "yes" else 0

    if not updates:
        print(json.dumps({"error": "No outcome flags provided. Use --outcome, --satisfaction, or --followup"}))
        sys.exit(1)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [entry["id"]]
    db.execute(f"UPDATE recipes SET {set_clause} WHERE id = ?", values)
    db.commit()

    # Read back
    row = db.execute("SELECT * FROM recipes WHERE id = ?", (entry["id"],)).fetchone()
    result = {
        "status": "verified",
        "id": entry["id"],
        "intent": entry["intent"],
        "outcome_verified": row["outcome_verified"],
        "user_satisfaction": row["user_satisfaction"],
        "had_followup_fix": row["had_followup_fix"],
    }
    print(json.dumps(result, indent=2))
    db.close()


def cmd_backfill(args):
    """Backfill analysis metrics on existing entries missing them."""
    db = get_db()

    rows = db.execute(
        "SELECT id, session_id, project_path FROM recipes WHERE compliance_grade IS NULL"
    ).fetchall()

    if not rows:
        print(json.dumps({"status": "nothing_to_backfill", "message": "All entries already have analysis metrics"}))
        return

    filled = 0
    skipped = 0

    for row in rows:
        session_file = _find_session_file(row["session_id"])
        if not session_file:
            skipped += 1
            continue

        analysis = _run_analysis(session_file)
        if "error" in analysis:
            skipped += 1
            continue

        commits = _count_commits(session_file)

        db.execute(
            """UPDATE recipes SET
                compliance_grade = ?, compliance_score = ?, process_score = ?,
                session_shape = ?, thrash_ratio = ?, tokens_per_output = ?,
                commits_produced = ?
               WHERE id = ?""",
            (
                analysis["compliance"]["grade"],
                analysis["compliance"]["score"],
                analysis["process"]["score"],
                analysis["session_shape"]["session_shape"],
                analysis["thrash_analysis"]["thrash_ratio"],
                analysis["cost_efficiency"]["tokens_per_output"],
                commits,
                row["id"],
            ),
        )
        filled += 1

    db.commit()
    db.close()

    print(json.dumps({
        "status": "backfilled",
        "filled": filled,
        "skipped": skipped,
        "total": len(rows),
        "message": f"Backfilled {filled}/{len(rows)} entries ({skipped} session files not found)",
    }, indent=2))


def _extract_session_features(session_file: Path) -> dict | None:
    """Extract all features from a session for the features table."""
    events = _parse_session_events(session_file)
    if not events or len(events) < 5:
        return None

    analysis = _run_analysis(session_file)
    if "error" in analysis:
        return None

    commits = _count_commits(session_file)

    # Detect primary model
    models = Counter()
    for ev in events:
        if ev.get("type") == "assistant":
            model = ev.get("message", {}).get("model", "")
            if model:
                models[model] += 1
    primary_model = models.most_common(1)[0][0] if models else ""
    # Simplify model name
    for short in ("opus", "sonnet", "haiku"):
        if short in primary_model:
            primary_model = short
            break

    had_error = 1 if analysis["anti_patterns"]["issue_count"] > 0 else 0

    # --- NEW FEATURES ---

    # 1. Late-session error rate: errors in the last 20% of tool calls
    all_tool_calls = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                all_tool_calls.append(block)
            elif block.get("type") == "tool_result":
                # Check for error in result
                is_err = block.get("is_error", False)
                if all_tool_calls:
                    all_tool_calls[-1]["_had_error"] = is_err

    # Also detect errors from tool_result events at top level
    tool_results = []
    for ev in events:
        if ev.get("type") == "result":
            tool_results.append(ev)

    # Approximate: count Bash calls where the command likely failed
    # (stderr patterns, non-zero exit, error keywords in tool_result)
    bash_calls_with_index = []
    idx = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                if block.get("name") == "Bash":
                    bash_calls_with_index.append(idx)
                idx += 1

    # Check tool results for errors
    error_indices = set()
    result_idx = 0
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        content = ev.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result":
                if block.get("is_error", False):
                    error_indices.add(result_idx)
                result_idx += 1

    total_tc = idx if idx > 0 else 1
    late_cutoff = int(total_tc * 0.8)
    late_errors = 0
    late_total = 0
    for bi in bash_calls_with_index:
        if bi >= late_cutoff:
            late_total += 1
            if bi in error_indices:
                late_errors += 1
    late_error_rate = round(late_errors / late_total, 3) if late_total > 0 else 0.0

    # 2. Session duration from timestamps
    first_ts = None
    last_ts = None
    for ev in events:
        ts = ev.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
    duration_min = None
    if first_ts and last_ts:
        try:
            t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except (ValueError, TypeError):
            pass

    # 3. Model switches: how many times did the model change between turns?
    model_sequence = []
    for ev in events:
        if ev.get("type") == "assistant":
            m = ev.get("message", {}).get("model", "")
            if m:
                model_sequence.append(m)
    model_switches = 0
    for i in range(1, len(model_sequence)):
        if model_sequence[i] != model_sequence[i - 1]:
            model_switches += 1

    # 4. Focused thrash: max edits to a single file
    # (high focused_thrash = hammering one file; different signal from thrash_ratio)
    edit_counts = analysis["thrash_analysis"]["re_edits"]  # {filename: count}
    focused_thrash = max(edit_counts.values()) if edit_counts else 0

    return {
        "session_id": session_file.stem,
        "compliance_score": analysis["compliance"]["score"],
        "compliance_grade": analysis["compliance"]["grade"],
        "process_score": analysis["process"]["score"],
        "session_shape": analysis["session_shape"]["session_shape"],
        "thrash_ratio": analysis["thrash_analysis"]["thrash_ratio"],
        "tokens_per_output": analysis["cost_efficiency"]["tokens_per_output"],
        "total_tokens": analysis["cost_efficiency"]["total_tokens"],
        "total_cost": analysis["cost_efficiency"]["est_cost"],
        "tool_misuses": analysis["tool_selection"]["misuse_count"],
        "anti_pattern_count": analysis["anti_patterns"]["issue_count"],
        "edit_count": analysis["thrash_analysis"]["edits"],
        "unique_files": analysis["thrash_analysis"]["unique_files"],
        "prompt_count": analysis["session_shape"]["total_prompts"],
        "model_primary": primary_model,
        "commits_produced": commits,
        "had_error_exit": had_error,
        "outcome_pass": 1 if commits > 0 else 0,
        "file_size_kb": session_file.stat().st_size // 1024,
        "late_error_rate": late_error_rate,
        "duration_min": duration_min,
        "model_switches": model_switches,
        "focused_thrash": focused_thrash,
    }


def _detect_followup_fixes(db: sqlite3.Connection):
    """Cross-reference sessions to auto-detect follow-up fix patterns.

    A session has a followup fix if a later session in the same project:
    - Starts within 4 hours
    - Edits at least one of the same files
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return 0

    updated = 0

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue

        # Get all sessions for this project, sorted by mtime
        sessions = []
        for jf in proj_dir.glob("*.jsonl"):
            if jf.stat().st_size < 5000:
                continue
            sessions.append({
                "file": jf,
                "id": jf.stem,
                "mtime": jf.stat().st_mtime,
            })

        sessions.sort(key=lambda s: s["mtime"])
        if len(sessions) < 2:
            continue

        # Extract files written per session (cached)
        files_cache = {}
        def get_files_written(sf: Path) -> set:
            key = str(sf)
            if key not in files_cache:
                files = set()
                try:
                    with open(sf) as f:
                        for line in f:
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if obj.get("type") != "assistant":
                                continue
                            content = obj.get("message", {}).get("content", [])
                            if not isinstance(content, list):
                                continue
                            for block in content:
                                if (block.get("type") == "tool_use"
                                        and block.get("name") in ("Write", "Edit")):
                                    fp = block.get("input", {}).get("file_path", "")
                                    if fp:
                                        files.add(fp)
                except Exception:
                    pass
                files_cache[key] = files
            return files_cache[key]

        # Compare consecutive sessions
        for i in range(len(sessions) - 1):
            curr = sessions[i]
            nxt = sessions[i + 1]

            # Within 4 hours?
            time_gap_hours = (nxt["mtime"] - curr["mtime"]) / 3600
            if time_gap_hours > 4:
                continue

            # File overlap?
            curr_files = get_files_written(curr["file"])
            nxt_files = get_files_written(nxt["file"])
            if not curr_files or not nxt_files:
                continue

            overlap = curr_files & nxt_files
            if overlap:
                # Mark the earlier session as having a followup fix
                db.execute(
                    "UPDATE session_features SET had_followup_fix = 1 WHERE session_id = ?",
                    (curr["id"],),
                )
                # Mark the later session as NOT a followup target
                # (it's the fix, not the thing being fixed)
                db.execute(
                    "UPDATE session_features SET had_followup_fix = 0 "
                    "WHERE session_id = ? AND had_followup_fix IS NULL",
                    (nxt["id"],),
                )
                updated += 1

    # Mark remaining NULL as 0 (no followup detected)
    db.execute(
        "UPDATE session_features SET had_followup_fix = 0 WHERE had_followup_fix IS NULL"
    )
    db.commit()
    return updated


def cmd_extract(args):
    """Batch extract features from all sessions for correlation analysis."""
    db = get_db()
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        print(json.dumps({"error": "No projects directory found"}))
        sys.exit(1)

    days = int(args.days) if args.days != "all" else None
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=days)
        if days else None
    )

    # Get already-extracted session IDs
    existing = set()
    try:
        rows = db.execute("SELECT session_id FROM session_features").fetchall()
        existing = {r[0] for r in rows}
    except Exception:
        pass

    extracted = 0
    skipped = 0
    total = 0

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        project_name = str(proj_dir.name).replace("-Users-nino-", "").replace("-", "/")

        for jf in proj_dir.glob("*.jsonl"):
            if cutoff:
                mtime = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue

            if jf.stat().st_size < 5000:
                continue

            total += 1

            if jf.stem in existing:
                skipped += 1
                continue

            features = _extract_session_features(jf)
            if not features:
                skipped += 1
                continue

            db.execute(
                """INSERT OR REPLACE INTO session_features
                   (session_id, project_path, compliance_score, compliance_grade,
                    process_score, session_shape, thrash_ratio, tokens_per_output,
                    total_tokens, total_cost, tool_misuses, anti_pattern_count,
                    edit_count, unique_files, prompt_count, model_primary,
                    commits_produced, had_error_exit, outcome_pass, file_size_kb,
                    late_error_rate, duration_min, model_switches, focused_thrash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    features["session_id"], project_name,
                    features["compliance_score"], features["compliance_grade"],
                    features["process_score"], features["session_shape"],
                    features["thrash_ratio"], features["tokens_per_output"],
                    features["total_tokens"], features["total_cost"],
                    features["tool_misuses"], features["anti_pattern_count"],
                    features["edit_count"], features["unique_files"],
                    features["prompt_count"], features["model_primary"],
                    features["commits_produced"], features["had_error_exit"],
                    features["outcome_pass"], features["file_size_kb"],
                    features["late_error_rate"], features["duration_min"],
                    features["model_switches"], features["focused_thrash"],
                ),
            )
            extracted += 1

    db.commit()

    # Auto-detect followup fixes
    followups = _detect_followup_fixes(db)

    # Stats
    total_features = db.execute("SELECT COUNT(*) FROM session_features").fetchone()[0]
    with_commits = db.execute(
        "SELECT COUNT(*) FROM session_features WHERE commits_produced > 0"
    ).fetchone()[0]
    with_followups = db.execute(
        "SELECT COUNT(*) FROM session_features WHERE had_followup_fix = 1"
    ).fetchone()[0]

    db.close()

    print(json.dumps({
        "status": "extracted",
        "new_sessions": extracted,
        "skipped": skipped,
        "total_scanned": total,
        "total_in_table": total_features,
        "followup_fixes_detected": followups,
        "sessions_with_commits": with_commits,
        "sessions_with_followup_fix": with_followups,
        "ready_for_correlate": total_features >= 50,
    }, indent=2))


def _pearson_r(x: list[float], y: list[float]) -> tuple[float, float]:
    """Pearson correlation coefficient and approximate two-tailed p-value.
    No external dependencies — uses Abramowitz & Stegun t-CDF approximation."""
    import math
    n = len(x)
    if n < 10:
        return 0.0, 1.0

    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
    den_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5
    if den_x == 0 or den_y == 0:
        return 0.0, 1.0
    r = num / (den_x * den_y)

    if abs(r) >= 0.9999:
        return round(r, 4), 0.0001

    # t-statistic
    t_stat = r * math.sqrt((n - 2) / (1 - r * r))
    df = n - 2

    # Approximate two-tailed p-value using normal approximation for large df
    # For df > 30 this is very accurate
    if df > 30:
        # Use normal approximation
        z = abs(t_stat)
        # Abramowitz & Stegun 26.2.17
        p_one_tail = 0.5 * math.erfc(z / math.sqrt(2))
        p = 2 * p_one_tail
    else:
        # Rough approximation for small df using regularized incomplete beta
        x_val = df / (df + t_stat * t_stat)
        # Simple approximation: for small samples, report p > 0.05 as not significant
        # This is a known limitation — with < 30 samples, use exact tables
        p = 0.1 if abs(r) < 0.35 else 0.05 if abs(r) < 0.5 else 0.01

    return round(r, 4), round(max(p, 0.0001), 4)


def cmd_correlate(args):
    """Compute correlations between process metrics and outcomes."""
    db = get_db()

    rows = db.execute(
        """SELECT compliance_score, process_score, thrash_ratio,
                  tokens_per_output, tool_misuses, anti_pattern_count,
                  edit_count, prompt_count, commits_produced,
                  outcome_pass, had_followup_fix,
                  late_error_rate, duration_min, model_switches, focused_thrash
           FROM session_features
           WHERE outcome_pass IS NOT NULL"""
    ).fetchall()

    if len(rows) < 30:
        print(json.dumps({
            "error": f"Need at least 30 sessions with outcomes, have {len(rows)}. Run /recall extract first.",
            "current_count": len(rows),
        }))
        sys.exit(1)

    # Build feature vectors
    features = {
        "compliance_score": [],
        "process_score": [],
        "thrash_ratio": [],
        "tokens_per_output": [],
        "tool_misuses": [],
        "anti_pattern_count": [],
        "edit_count": [],
        "prompt_count": [],
        "late_error_rate": [],
        "duration_min": [],
        "model_switches": [],
        "focused_thrash": [],
    }
    outcome_pass = []
    followup_fix = []

    for row in rows:
        for key in features:
            val = row[key]
            features[key].append(float(val) if val is not None else 0.0)
        outcome_pass.append(float(row["outcome_pass"] or 0))
        followup_fix.append(float(row["had_followup_fix"] or 0))

    n = len(outcome_pass)
    pass_rate = sum(outcome_pass) / n
    followup_rate = sum(followup_fix) / n

    # Correlate each feature with outcome_pass
    pass_correlations = {}
    for name, values in features.items():
        r, p = _pearson_r(values, outcome_pass)
        pass_correlations[name] = {
            "r": r, "p": p,
            "significant": p < 0.05,
            "direction": "positive" if r > 0 else "negative",
        }

    # Correlate each feature with had_followup_fix
    fix_correlations = {}
    for name, values in features.items():
        r, p = _pearson_r(values, followup_fix)
        fix_correlations[name] = {
            "r": r, "p": p,
            "significant": p < 0.05,
            "direction": "positive" if r > 0 else "negative",
        }

    # Mean comparison: pass vs fail
    mean_comp = {}
    for name, values in features.items():
        pass_vals = [v for v, o in zip(values, outcome_pass) if o == 1]
        fail_vals = [v for v, o in zip(values, outcome_pass) if o == 0]
        if pass_vals and fail_vals:
            mean_comp[name] = {
                "pass_mean": round(sum(pass_vals) / len(pass_vals), 2),
                "fail_mean": round(sum(fail_vals) / len(fail_vals), 2),
                "pass_n": len(pass_vals),
                "fail_n": len(fail_vals),
            }

    # Find significant predictors
    sig_pass = [k for k, v in pass_correlations.items() if v["significant"]]
    sig_fix = [k for k, v in fix_correlations.items() if v["significant"]]

    # Build conclusion
    if not sig_pass and not sig_fix:
        conclusion = (
            "No metrics show statistically significant correlation with outcomes. "
            "Process metrics are descriptive noise at this sample size. "
            "Consider: more data, or accept that compliance is the only actionable signal."
        )
    else:
        parts = []
        if sig_pass:
            parts.append(f"Metrics predicting commit production: {', '.join(sig_pass)}")
        if sig_fix:
            parts.append(f"Metrics predicting followup fixes: {', '.join(sig_fix)}")
        conclusion = ". ".join(parts) + "."

    output = {
        "sample_size": n,
        "pass_rate": round(pass_rate, 3),
        "followup_fix_rate": round(followup_rate, 3),
        "vs_outcome_pass": pass_correlations,
        "vs_followup_fix": fix_correlations,
        "mean_comparison": mean_comp,
        "significant_predictors": {
            "outcome_pass": sig_pass,
            "followup_fix": sig_fix,
        },
        "conclusion": conclusion,
    }

    print(json.dumps(output, indent=2))
    db.close()


def _find_session_file(
    session_id: str,
    project_path: str = None,
    source_client: str = "claude",
) -> Path | None:
    """Locate a session JSONL file."""
    if source_client == "codex":
        roots = (
            Path.home() / ".codex" / "sessions",
            Path.home() / ".codex" / "archived_sessions",
        )
        candidates: list[Path] = []
        for root in roots:
            if root.exists():
                candidates.extend(root.rglob(f"*{session_id}*.jsonl"))
        return max(candidates, key=lambda item: item.stat().st_mtime_ns) if candidates else None

    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def _extract_session_cost(session_file: Path) -> tuple:
    """Extract total tokens and estimated cost from a session JSONL."""
    total_tokens = 0
    total_cost = 0.0

    # Approximate pricing per 1M tokens (input/output blended)
    MODEL_COSTS = {
        "claude-opus-4-6": 0.015,       # ~$15/M input, $75/M output blended
        "claude-sonnet-4-6": 0.003,      # ~$3/M input, $15/M output blended
        "claude-haiku-4-5": 0.0008,      # ~$0.8/M input, $4/M output blended
    }

    try:
        with open(session_file) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "assistant":
                    continue

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue

                usage = msg.get("usage", {})
                model = msg.get("model", "")

                input_t = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                tokens = input_t + output_t
                total_tokens += tokens

                # Find matching cost rate
                rate = 0.003  # default to sonnet-class
                for model_key, model_rate in MODEL_COSTS.items():
                    if model_key in model:
                        rate = model_rate
                        break

                total_cost += tokens * rate / 1_000_000

    except Exception:
        pass

    return (total_tokens if total_tokens > 0 else None,
            round(total_cost, 4) if total_cost > 0 else None)


def main():
    parser = argparse.ArgumentParser(description="Recall CLI")
    sub = parser.add_subparsers(dest="command")

    # save
    save_p = sub.add_parser("save")
    save_p.add_argument("--session-id", required=True)
    save_p.add_argument(
        "--source-client", choices=["claude", "codex", "gemini"], default="claude"
    )
    save_p.add_argument("--project", default=None)
    save_p.add_argument("--intent", required=True)
    save_p.add_argument("--sources", default="[]")
    save_p.add_argument("--key-commands", default="[]")
    save_p.add_argument("--outcome", default=None)
    save_p.add_argument("--prompt-template", default=None)
    save_p.add_argument("--quality-class", default="productive",
                        choices=["high_value", "productive", "neutral", "churn", "dead_end"])
    save_p.add_argument("--quality-reason", default=None)
    save_p.add_argument("--tags", default="[]")

    # find
    find_p = sub.add_parser("find")
    find_p.add_argument("query")

    # list
    list_p = sub.add_parser("list")
    list_p.add_argument("--limit", type=int, default=20)

    # show
    show_p = sub.add_parser("show")
    show_p.add_argument("id")

    # use
    use_p = sub.add_parser("use")
    use_p.add_argument("id")
    use_p.add_argument("--var", dest="vars", action="append",
                       help="Variable substitution: --var key=value")

    # stats
    sub.add_parser("stats")

    # analyze
    analyze_p = sub.add_parser("analyze")
    analyze_g = analyze_p.add_mutually_exclusive_group(required=True)
    analyze_g.add_argument("--session-id", default=None,
                           help="Session ID to analyze")
    analyze_g.add_argument("--file", default=None,
                           help="Path to session JSONL file")

    # quality
    quality_p = sub.add_parser("quality")
    quality_p.add_argument("--days", default="30",
                           help="Days to look back, or 'all'")
    quality_p.add_argument("--limit", type=int, default=50,
                           help="Max sessions to analyze")

    # verify
    verify_p = sub.add_parser("verify")
    verify_p.add_argument("id")
    verify_p.add_argument("--outcome", choices=["pass", "fail"], default=None,
                          help="Did the session produce working code?")
    verify_p.add_argument("--satisfaction", type=int, choices=range(1, 6),
                          default=None, metavar="1-5",
                          help="User satisfaction rating (1=terrible, 5=excellent)")
    verify_p.add_argument("--followup", choices=["yes", "no"], default=None,
                          help="Was there a follow-up fix session?")

    # backfill
    sub.add_parser("backfill")

    # extract
    extract_p = sub.add_parser("extract")
    extract_p.add_argument("--days", default="90",
                           help="Days to look back, or 'all'")

    # correlate
    sub.add_parser("correlate")

    args = parser.parse_args()

    if args.command == "save":
        cmd_save(args)
    elif args.command == "find":
        cmd_find(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "use":
        cmd_use(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "quality":
        cmd_quality(args)
    elif args.command == "verify":
        cmd_verify(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "correlate":
        cmd_correlate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
