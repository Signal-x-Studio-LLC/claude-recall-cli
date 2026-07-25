#!/usr/bin/env python3
"""Extract user-voice signals from Claude Code sessions for building a Poe character stack.


Unlike recall-scan (which scores sessions for reusable prompt recipes), this tool mines
the USER turns across all sessions to codify how Nino thinks, decides, corrects, and
pushes back. The output is a queryable voice corpus — a "Poe" in the Altered Carbon sense.

Storage lives in the same SQLite file as recall-cli (~/.claude/recall.db) under the
voice_signals table + FTS5 index, so Poe and recipes share one corpus.

Usage:
    poe-extract.py extract [--limit N] [--since DAYS]   scan all JSONL -> corpus.jsonl
    poe-extract.py extract --session PATH                scan one JSONL -> DB (hook)
    poe-extract.py publish                               corpus.jsonl -> DB
    poe-extract.py assemble                              DB -> stack.md
    poe-extract.py query TERMS [--limit N]               FTS5 search -> markdown
    poe-extract.py run                                   extract + publish + assemble
    poe-extract.py init                                  ensure DB schema exists
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
POE_DIR = Path.home() / ".claude" / "poe"
CORPUS_PATH = POE_DIR / "corpus.jsonl"
STACK_PATH = POE_DIR / "stack.md"
QUEUE_PATH = POE_DIR / "queue"
HOOK_LOG_PATH = POE_DIR / "hook.log"
RECALL_DB = Path.home() / ".claude" / "recall.db"

# Opportunistic-catchup staleness threshold for read paths (query/assemble).
READ_PATH_STALE_SECONDS = 900  # 15 min

# A surfaced "prior" must be SETTLED history, not a fresh message from an
# in-flight concurrent session. The recall DB is machine-global and ingests
# every session continuously, so without a floor a directive typed into one
# live session leaks into another's prompt-hook as a "prior" within minutes.
# Signals newer than this are excluded from the per-prompt surface.
# (2026-05-29 cross-session-leak postmortem.)
PRIOR_RECENCY_FLOOR_HOURS = 24

NOISE_PREFIXES = (
    "[Request interrupted",
    "This session is being continued",
    "Caveat: The messages below",
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    # Agent-generated prompts (multi-agent sessions). These arrive through the
    # UserPromptSubmit path but are not Nino's voice — ingesting them drifts
    # the tone fingerprint toward machine-written text.
    "<task-notification>",
    "<teammate-message",
    "<agent-message",
    "Another Claude session sent a message",
)

# Messages dominated by pasted logs/code/tool output are not user voice.
# Heuristic: high ratio of non-prose characters or very long.
MAX_USER_MSG_LEN = 4000
MIN_USER_MSG_LEN = 2  # lowered from 4 to capture bare "go" / "ok" redirects

# Signal patterns. Each tuple: (signal_type, compiled regex, short label)
def _c(p): return re.compile(p, re.IGNORECASE)

SIGNAL_PATTERNS = [
    # === CORRECTIONS — negative feedback on prior assistant action ===
    ("correction", _c(r"^\s*(no|nope|stop|wait|hold on|don't|do not)\b"), "opening-negative"),
    ("correction", _c(r"\bthat(?:'s| is)\s+(not|wrong|incorrect|bad)\b"), "that-is-wrong"),
    ("correction", _c(r"\byou(?:'re| are)\s+(wrong|incorrect|missing|off)\b"), "you-are-wrong"),
    ("correction", _c(r"\bwhy (did|would) you\b"), "why-did-you"),
    ("correction", _c(r"\b(undo|revert|roll ?back|back out|unwind)\b"), "undo"),
    ("correction", _c(r"\bnot what I (asked|wanted|meant)\b"), "not-what-i-asked"),
    ("correction", _c(r"\b(over[- ]?engineer|over[- ]?complicated|too much|scope creep)\b"), "over-engineered"),

    # === PREFERENCES — explicit rules about how things should be ===
    ("preference", _c(r"\bI (prefer|like|want|hate|dislike|don't (want|like))\b"), "i-prefer"),
    ("preference", _c(r"\bwe (prefer|always|never) (use|write|do|have|go|commit|push|call)\b"), "we-convention"),
    ("preference", _c(r"\bwe (don't|do not) (use|write|do|want|need to|commit|push|call|allow)\b"), "we-dont"),
    ("preference", _c(r"\b(always|never) (use|write|call|do|add|create|commit|push)\b"), "always-never"),
    ("preference", _c(r"\b(make sure|ensure) (you|to|that)\b"), "make-sure"),
    ("preference", _c(r"\bfrom now on\b"), "from-now-on"),
    ("preference", _c(r"\bgoing forward\b"), "going-forward"),

    # === RATIONALE — reasons behind decisions ===
    ("rationale", _c(r"\bbecause\b"), "because"),
    ("rationale", _c(r"\bthe reason (is|we|I)\b"), "the-reason"),
    ("rationale", _c(r"\b(we|I) got burned\b"), "got-burned"),
    ("rationale", _c(r"\blast time\b"), "last-time"),
    ("rationale", _c(r"\botherwise\b"), "otherwise"),
    ("rationale", _c(r"\bthat way\b"), "that-way"),

    # === DECLARATIONS — imperative rules, often first messages ===
    ("declaration", _c(r"^\s*(use|don't|do not|keep|avoid|skip|drop|remove|add)\s+\w"), "imperative-rule"),

    # === APPROVALS — validated choices (short msgs are stronger signal) ===
    ("approval", _c(r"^\s*(perfect|exactly|yes exactly|good call|nice|that's (it|right)|correct)\b"), "short-approval"),
    ("approval", _c(r"\bship it\b"), "ship-it"),
    # redirect-go: short messages that mean "you already have authorization, keep moving".
    # The prior_assistant column captures what Claude was asking — useful for
    # learning which question shapes Nino routinely overrides with "go".
    ("approval", _c(r"^\s*(go|proceed|continue|keep going|do all|all of it|do it|do the rest|move on|next|push|run it|execute)[.!]?\s*$"), "redirect-go"),

    # === REJECTIONS with alternative ===
    ("rejection", _c(r"\binstead\b"), "instead"),
    ("rejection", _c(r"\brather than\b"), "rather-than"),
]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS voice_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    project         TEXT,
    timestamp       TEXT,
    signal_type     TEXT NOT NULL,
    label           TEXT NOT NULL,
    phrase          TEXT NOT NULL,
    message         TEXT,
    prior_assistant TEXT,
    phrase_hash     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, signal_type, phrase_hash)
);

CREATE INDEX IF NOT EXISTS voice_signals_signal_idx
    ON voice_signals(signal_type, label);
CREATE INDEX IF NOT EXISTS voice_signals_project_idx
    ON voice_signals(project);
CREATE INDEX IF NOT EXISTS voice_signals_session_idx
    ON voice_signals(session_id);

-- Porter stemmer collapses engineer/engineered/engineering to a common
-- stem so synonym-shaped queries hit. Big recall lift vs default unicode61.
CREATE VIRTUAL TABLE IF NOT EXISTS voice_signals_fts USING fts5(
    phrase, message, signal_type, label, project,
    content=voice_signals, content_rowid=id,
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS voice_signals_ai AFTER INSERT ON voice_signals BEGIN
    INSERT INTO voice_signals_fts(rowid, phrase, message, signal_type, label, project)
    VALUES (new.id, new.phrase, new.message, new.signal_type, new.label, new.project);
END;

CREATE TRIGGER IF NOT EXISTS voice_signals_ad AFTER DELETE ON voice_signals BEGIN
    INSERT INTO voice_signals_fts(voice_signals_fts, rowid, phrase, message, signal_type, label, project)
    VALUES ('delete', old.id, old.phrase, old.message, old.signal_type, old.label, old.project);
END;

-- Watermark per session file. Idempotent ingest is driven by comparing
-- on-disk mtime to last_mtime_ns; advancing the watermark is the commit.
CREATE TABLE IF NOT EXISTS ingest_watermark (
    session_path    TEXT PRIMARY KEY,
    last_mtime_ns   INTEGER NOT NULL,
    last_ingested   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Single-row table tracking the last successful full-sweep timestamp,
-- consulted by read paths (query/assemble) for opportunistic catchup.
CREATE TABLE IF NOT EXISTS ingest_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Reverse Poe: assistant turns followed by a NON-corrective user reply.
-- These are response shapes Nino tolerated — validated cadence/structure
-- exemplars. The shape_signature is a normalized digest of the response
-- structure (length bucket, has_question, has_list, has_code_block) used
-- for dedup. Storing the assistant text lets us mine cadence later.
CREATE TABLE IF NOT EXISTS validated_responses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    project         TEXT,
    timestamp       TEXT,
    response_text   TEXT NOT NULL,
    response_chars  INTEGER NOT NULL,
    shape_signature TEXT NOT NULL,
    follow_label    TEXT,           -- e.g. 'redirect-go', 'approval', 'silent'
    response_hash   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, response_hash)
);

CREATE INDEX IF NOT EXISTS validated_responses_shape_idx
    ON validated_responses(shape_signature);
CREATE INDEX IF NOT EXISTS validated_responses_project_idx
    ON validated_responses(project);
"""


def _migrate_fts_tokenizer(conn: sqlite3.Connection) -> bool:
    """Ensure the FTS5 index uses the porter tokenizer. Returns True if a
    rebuild is needed (caller invokes after schema recreate)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='voice_signals_fts'"
    ).fetchone()
    if not row:
        return False  # fresh DB — schema will create with porter, empty so no rebuild
    if row[0] and "porter" in row[0].lower():
        # Already correct, but check the internal storage is populated.
        # voice_signals_fts_data is FTS5's internal index table.
        try:
            data_rows = conn.execute("SELECT COUNT(*) FROM voice_signals_fts_data").fetchone()[0]
            base_rows = conn.execute("SELECT COUNT(*) FROM voice_signals").fetchone()[0]
            # FTS5 always has at least 1 row in _data for metadata; empty index has 1.
            return base_rows > 0 and data_rows <= 1
        except sqlite3.OperationalError:
            return False
    # Drop the wrong-tokenizer index; schema script recreates with porter.
    conn.execute("DROP TRIGGER IF EXISTS voice_signals_ai")
    conn.execute("DROP TRIGGER IF EXISTS voice_signals_ad")
    conn.execute("DROP TABLE voice_signals_fts")
    conn.commit()
    return True


def db_connect() -> sqlite3.Connection:
    RECALL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(RECALL_DB))
    needs_rebuild = _migrate_fts_tokenizer(conn)
    conn.executescript(SCHEMA_SQL)
    if needs_rebuild:
        conn.execute("INSERT INTO voice_signals_fts(voice_signals_fts) VALUES('rebuild')")
        conn.commit()
    return conn


def phrase_hash(phrase: str) -> str:
    """Stable hash for dedup — normalize whitespace and case."""
    norm = re.sub(r"\s+", " ", phrase.lower()).strip()[:200]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


_HOME_PREFIX = str(Path.home()) + "/"
_WORKSPACE_PREFIX = str(Path.home() / "Workspace") + "/"


def _cwd_to_label(cwd: str) -> str:
    """Render a cwd as a short, readable project label.
    /Users/nino/Workspace/dev/wip/aisles-storefront -> wip/aisles-storefront
    /Users/nino/foo -> foo"""
    if cwd.startswith(_WORKSPACE_PREFIX):
        rel = cwd[len(_WORKSPACE_PREFIX):]
        # Drop the 'dev/' segment — it's nearly all of them, adds no information.
        if rel.startswith("dev/"):
            rel = rel[4:]
        return rel
    if cwd.startswith(_HOME_PREFIX):
        return cwd[len(_HOME_PREFIX):]
    return cwd


def _read_cwd(session_file: Path) -> str | None:
    """Peek the JSONL for the first entry that has a cwd field. Cheap — usually
    in the first or second line. Returns None if not found."""
    try:
        with open(session_file, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 20:  # bail if no cwd in early lines
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        return None
    return None


def project_label_for(session_file: Path) -> str:
    """Resolve a session file to its human-readable project label via cwd.
    Falls back to a dirname-derived label if cwd is unavailable."""
    cwd = _read_cwd(session_file)
    if cwd:
        return _cwd_to_label(cwd)
    # Fallback: best-effort decode of Claude Code's dirname encoding.
    # Leading '-' marker, then path segments joined by '-'. Strip the
    # /Users/nino/ home prefix if present and leave the rest as-is (no
    # lossy dash-to-slash substitution).
    name = session_file.parent.name
    if name.startswith("-Users-nino-"):
        return name[len("-Users-nino-"):]
    return name


def iter_user_messages(session_file: Path):
    """Yield (timestamp, text, prior_assistant_text) for each real user turn.
    Deduplicates messages within a session (sidechain entries duplicate main chain)."""
    prior_assistant = ""
    seen_msgs: set[str] = set()
    try:
        with open(session_file, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                t = obj.get("type")
                ts = obj.get("timestamp", "")

                if t == "assistant":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        texts = [
                            c.get("text", "")
                            for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        if texts:
                            prior_assistant = " ".join(texts)[:600]
                    continue

                if t != "user":
                    continue

                msg = obj.get("message", {})
                content = msg.get("content", "")

                # Extract only real text user messages, skip tool_result
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "tool_result":
                            text = None
                            break
                        if c.get("type") == "text":
                            text = c.get("text", "")
                            break

                if not text:
                    continue

                text = text.strip()
                if len(text) < MIN_USER_MSG_LEN or len(text) > MAX_USER_MSG_LEN * 4:
                    continue
                if text.startswith(NOISE_PREFIXES):
                    continue
                # Skip messages that are mostly tags/code dumps
                if text.count("<") > 20 or text.count("```") > 6:
                    continue

                # Dedupe within session (sidechain duplicates)
                msg_key = text[:200]
                if msg_key in seen_msgs:
                    continue
                seen_msgs.add(msg_key)

                yield (
                    ts,
                    redact_secrets(text[:MAX_USER_MSG_LEN]),
                    redact_secrets(prior_assistant),
                )
    except Exception:
        return


# Credential-shaped strings, redacted at ingest. Nino pastes real keys into
# prompts ("use this api key: ..."), and those sentences match signal patterns
# like [rejection/instead] — so they land in voice_signals, get mirrored into
# the FTS index, and the prompt-hook re-injects them into model context on any
# keyword match. That is silent egress from a file nobody thinks of as a secret
# store. Redact at the two ingest yields (iter_user_messages, iter_pairs) so
# every downstream path — signals, validated responses, phrase hashes, FTS —
# only ever sees the placeholder. Provider prefixes, not entropy heuristics:
# false positives here cost a mangled phrase, false negatives leak a live key.
SECRET_PATTERNS = re.compile(
    r"""(
        sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}     # Anthropic
      | sk-proj-[A-Za-z0-9_\-]{20,}             # OpenAI project
      | sk-or-v1-[A-Za-z0-9_\-]{20,}            # OpenRouter
      | sk-[A-Za-z0-9]{32,}                     # generic OpenAI-style
      | cfk_[A-Za-z0-9_\-]{24,}                 # Cloudflare
      | sbp_[A-Za-z0-9]{20,}                    # Supabase personal token
      | sb(?:p|s)_[A-Za-z0-9_\-]{20,}           # Supabase service keys
      | gh[pousr]_[A-Za-z0-9]{20,}              # GitHub tokens
      | AKIA[0-9A-Z]{16}                        # AWS access key id
      | xox[baprs]-[A-Za-z0-9\-]{10,}           # Slack
      | ey[A-Za-z0-9_\-]{10,}\.ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}  # JWT
    )""",
    re.VERBOSE,
)

REDACTION = "[redacted-secret]"


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with a fixed placeholder."""
    if not text:
        return text
    return SECRET_PATTERNS.sub(REDACTION, text)


# Markers that suggest a regex hit landed inside pasted content, not Nino's voice
PASTE_MARKERS = re.compile(
    r'(\*\*[^*]+\*\*|`[^`]+`|\{"|"\}|://|\\n|\\\"|"detail":|^\s*[-*]\s|^\s*\d+\.\s)',
    re.MULTILINE,
)

# If these markers appear in the raw message, restrict scanning to the intro only
HEAVY_PASTE_MARKERS = re.compile(r'(```|^##+ |\n- \*\*|\n\d+\. \*\*)', re.MULTILINE)

# Sentence boundaries: terminator (. ! ?) followed by whitespace+capital/newline,
# or a newline run. Keeping it conservative — over-splitting truncates context.
SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'(\[])|\n{2,}')


def _bound_to_sentence(scan: str, match_start: int, match_end: int) -> str:
    """Extract the sentence containing the match, plus one preceding sentence
    if the match lands near the start of its own sentence (gives the regex hit
    its run-up — important for matches like 'because' where the rationale is in
    the prior clause). Avoids mid-word truncation."""
    # Walk back to nearest sentence start at or before match_start.
    left = 0
    for m in SENTENCE_SPLIT.finditer(scan, 0, match_start):
        left = m.end()
    # Walk forward to nearest sentence terminator at or after match_end.
    right = len(scan)
    fwd = SENTENCE_SPLIT.search(scan, match_end)
    if fwd:
        right = fwd.start()
    phrase = scan[left:right].strip()
    # If the match is in the first ~40 chars of the bounded sentence and a
    # prior sentence exists, prepend it — gives `because`/`instead` etc. the
    # antecedent they need to make sense.
    if (match_start - left) < 40 and left > 0:
        # Walk back one more sentence.
        prior_left = 0
        for m in SENTENCE_SPLIT.finditer(scan, 0, left - 1):
            prior_left = m.end()
        prior = scan[prior_left:left].strip()
        if prior:
            phrase = (prior + " " + phrase).strip()
    return phrase


def extract_signals(text: str):
    """Return list of (signal_type, label, matched_phrase) for a user message."""
    hits = []
    seen = set()

    # If the message looks like it contains pasted blocks, only scan the intro
    if HEAVY_PASTE_MARKERS.search(text):
        scan = text[:300]
    else:
        scan = text[:800]

    for stype, pat, label in SIGNAL_PATTERNS:
        m = pat.search(scan)
        if not m:
            continue
        key = (stype, label)
        if key in seen:
            continue

        phrase = _bound_to_sentence(scan, m.start(), m.end())
        # Cap pathological-length sentences (legal/run-on prose).
        if len(phrase) > 400:
            phrase = phrase[:400].rsplit(" ", 1)[0] + "…"

        # Skip if the phrase itself looks like pasted content
        if PASTE_MARKERS.search(phrase):
            continue
        # Skip short or mostly-URL phrases — but redirect-go is intentionally short.
        # The signal IS the brevity ("go" alone after a hesitation question);
        # the context lives in prior_assistant.
        if label != "redirect-go" and len(phrase) < 20:
            continue

        seen.add(key)
        hits.append((stype, label, phrase))
    return hits


CORRECTIVE_LABELS = {
    "opening-negative", "that-is-wrong", "you-are-wrong", "why-did-you",
    "undo", "not-what-i-asked", "over-engineered",
}


def _shape_signature(text: str) -> str:
    """Compact signature of an assistant response shape — used for dedup.
    Captures: length bucket, ends-with-question, has bullet list, has code block,
    sentence count bucket. Two responses with the same signature are roughly
    structurally equivalent."""
    n = len(text)
    if n < 200:
        bucket = "xs"
    elif n < 600:
        bucket = "s"
    elif n < 1500:
        bucket = "m"
    elif n < 4000:
        bucket = "l"
    else:
        bucket = "xl"
    sent = len(re.findall(r"[.!?](?:\s|$)", text))
    sb = min(sent // 5, 6)  # bucketed sentence count
    q = "Q" if text.rstrip().endswith("?") else "."
    lst = "L" if re.search(r"(?m)^\s*[-*]\s|\n\d+\.\s", text) else "-"
    code = "C" if "```" in text else "-"
    return f"{bucket}/{sb}/{q}{lst}{code}"


def _classify_follow(user_text: str) -> str | None:
    """Given the user reply that followed an assistant turn, classify it.
    Returns one of: 'corrective', 'redirect-go', 'approval', 'neutral',
    or None for empty/skip."""
    text = user_text.strip()
    if not text:
        return None
    # Run the signal patterns; if any corrective label fires, this is corrective.
    hits = extract_signals(text)
    labels = {label for _, label, _ in hits}
    if labels & CORRECTIVE_LABELS:
        return "corrective"
    if "redirect-go" in labels:
        return "redirect-go"
    if "short-approval" in labels or "ship-it" in labels:
        return "approval"
    return "neutral"


def iter_pairs(session_file: Path):
    """Yield (assistant_text, follow_user_text, ts) for each adjacent
    assistant→user pair in the JSONL."""
    pending_assistant: str | None = None
    pending_ts = ""
    try:
        with open(session_file, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                ts = obj.get("timestamp", "")
                if t == "assistant":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        texts = [
                            c.get("text", "") for c in content
                            if isinstance(c, dict) and c.get("type") == "text"
                        ]
                        if texts:
                            pending_assistant = " ".join(texts).strip()
                            pending_ts = ts
                elif t == "user" and pending_assistant:
                    msg = obj.get("message", {})
                    content = msg.get("content", "")
                    text = None
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "tool_result":
                                text = None
                                break
                            if c.get("type") == "text":
                                text = c.get("text", "")
                                break
                    if text:
                        yield (
                            redact_secrets(pending_assistant),
                            redact_secrets(text.strip()),
                            pending_ts,
                        )
                    pending_assistant = None
    except OSError:
        return


def _extract_validated_from_file(jf: Path) -> list[dict]:
    """Pair-walk pass: find assistant turns followed by non-corrective replies.
    Excludes turns where the assistant text is dominated by tool-use markers
    or is trivially short (no behavioral signal)."""
    project = project_label_for(jf)
    session_id = jf.stem
    out: list[dict] = []
    for assistant_text, follow_text, ts in iter_pairs(jf):
        # Filter: skip very short or near-empty assistant turns.
        if len(assistant_text) < 80:
            continue
        # Skip turns dominated by tool-call narration (lots of backticks/paths).
        if assistant_text.count("`") > 30:
            continue
        follow_class = _classify_follow(follow_text)
        if follow_class is None or follow_class == "corrective":
            continue
        signature = _shape_signature(assistant_text)
        digest = hashlib.sha1(assistant_text[:600].encode("utf-8")).hexdigest()[:16]
        out.append({
            "project": project,
            "session_id": session_id,
            "timestamp": ts,
            "response_text": assistant_text[:2000],
            "response_chars": len(assistant_text),
            "shape_signature": signature,
            "follow_label": follow_class,
            "response_hash": digest,
        })
    return out


def _upsert_validated(conn: sqlite3.Connection, records: list[dict]) -> int:
    inserted = 0
    for r in records:
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO validated_responses
                    (session_id, project, timestamp, response_text, response_chars,
                     shape_signature, follow_label, response_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["session_id"], r.get("project"), r.get("timestamp") or "",
                    r["response_text"], r["response_chars"],
                    r["shape_signature"], r.get("follow_label"), r["response_hash"],
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
        except sqlite3.Error as e:
            print(f"  validated insert error: {e}", file=sys.stderr)
    conn.commit()
    return inserted


def _extract_from_file(jf: Path) -> list[dict]:
    """Extract all signal records from a single JSONL file."""
    project = project_label_for(jf)
    session_id = jf.stem
    records: list[dict] = []
    for ts, text, prior in iter_user_messages(jf):
        hits = extract_signals(text)
        for stype, label, phrase in hits:
            records.append({
                "project": project,
                "session_id": session_id,
                "timestamp": ts,
                "signal": stype,
                "label": label,
                "phrase": phrase,
                "message": text[:1200],
                "prior_assistant": prior[:400],
            })
    return records


def _upsert_signals(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Insert records into voice_signals, skipping duplicates. Returns inserted count."""
    inserted = 0
    for r in records:
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO voice_signals
                    (session_id, project, timestamp, signal_type, label,
                     phrase, message, prior_assistant, phrase_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["session_id"],
                    r.get("project"),
                    r.get("timestamp") or "",
                    r["signal"],
                    r["label"],
                    r["phrase"],
                    r.get("message", ""),
                    r.get("prior_assistant", ""),
                    phrase_hash(r["phrase"]),
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
        except sqlite3.Error as e:
            print(f"  db error: {e}", file=sys.stderr)
    conn.commit()
    return inserted


def _watermark_get(conn: sqlite3.Connection, path: str) -> int:
    row = conn.execute(
        "SELECT last_mtime_ns FROM ingest_watermark WHERE session_path = ?",
        (path,),
    ).fetchone()
    return row[0] if row else 0


def _watermark_set(conn: sqlite3.Connection, path: str, mtime_ns: int) -> None:
    conn.execute(
        """
        INSERT INTO ingest_watermark (session_path, last_mtime_ns)
        VALUES (?, ?)
        ON CONFLICT(session_path) DO UPDATE SET
            last_mtime_ns = excluded.last_mtime_ns,
            last_ingested = datetime('now')
        """,
        (path, mtime_ns),
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM ingest_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO ingest_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _ingest_file(conn: sqlite3.Connection, jf: Path) -> tuple[int, int]:
    """Ingest one session file if its mtime exceeds the watermark.
    Returns (signals_seen, signals_or_validated_inserted). No-op if current.
    Runs both the user-signal extractor AND the validated-response pair walker
    so both corpora stay in sync under one watermark."""
    try:
        mtime_ns = jf.stat().st_mtime_ns
    except FileNotFoundError:
        return (0, 0)
    path_str = str(jf)
    if _watermark_get(conn, path_str) >= mtime_ns:
        return (0, 0)
    records = _extract_from_file(jf)
    inserted = _upsert_signals(conn, records)
    validated = _extract_validated_from_file(jf)
    v_inserted = _upsert_validated(conn, validated)
    _watermark_set(conn, path_str, mtime_ns)
    conn.commit()
    # Roll validated inserts into the "anything changed?" count so catchup
    # rebuilds stack.md when only reverse-Poe data is new.
    return (len(records) + len(validated), inserted + v_inserted)


def _iter_session_files() -> list[Path]:
    files: list[Path] = []
    if not PROJECTS_DIR.exists():
        return files
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        files.extend(proj_dir.glob("*.jsonl"))
    return files


def cmd_catchup(verbose: bool = False) -> None:
    """Ingest every session file with mtime newer than its watermark.

    Idempotent: re-running with no new data is near-zero cost (one stat
    per file, no extraction, no DB writes beyond the meta timestamp).
    Safe to invoke from any trigger — hook drain, schedule, read path."""
    conn = db_connect()
    files = _iter_session_files()
    scanned = 0
    ingested_files = 0
    total_signals = 0
    total_inserted = 0
    for jf in files:
        scanned += 1
        signals, inserted = _ingest_file(conn, jf)
        if signals:
            ingested_files += 1
            total_signals += signals
            total_inserted += inserted
    _meta_set(conn, "last_catchup", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.commit()
    conn.close()
    if verbose or ingested_files:
        print(
            f"catchup: {scanned} scanned, {ingested_files} ingested, "
            f"{total_inserted}/{total_signals} new signals",
            file=sys.stderr,
        )
    # Stack.md is the human/LLM-facing artifact. Rebuild it when the corpus
    # changed so the consumer never reads stale signals.
    if total_inserted > 0:
        try:
            cmd_assemble(_skip_catchup=True)
        except Exception as e:
            print(f"stack rebuild failed: {e}", file=sys.stderr)


def cmd_drain_queue(verbose: bool = False) -> None:
    """Drain ~/.claude/poe/queue, ingesting each listed transcript path.

    The queue is a newline-delimited file of transcript paths written by
    the SessionEnd hook. Drains atomically: rename the queue aside, then
    ingest entries. Files not in the queue are still picked up by the
    next catchup sweep — the queue is an optimization, not the source
    of truth."""
    conn = db_connect()
    if not QUEUE_PATH.exists():
        # Nothing queued, but still run a watermark sweep so the worker
        # is self-healing if WatchPaths missed an event.
        conn.close()
        cmd_catchup(verbose=verbose)
        return
    tmp = QUEUE_PATH.with_suffix(".draining")
    try:
        QUEUE_PATH.rename(tmp)
    except FileNotFoundError:
        conn.close()
        cmd_catchup(verbose=verbose)
        return

    paths: list[Path] = []
    seen: set[str] = set()
    with open(tmp, "r", errors="replace") as f:
        for line in f:
            p = line.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            paths.append(Path(p))
    tmp.unlink(missing_ok=True)

    ingested = 0
    inserted_total = 0
    for jf in paths:
        if not jf.exists():
            continue
        signals, inserted = _ingest_file(conn, jf)
        if signals:
            ingested += 1
            inserted_total += inserted
    conn.close()
    if verbose or ingested:
        print(
            f"drain-queue: {len(paths)} queued, {ingested} ingested, "
            f"{inserted_total} new signals",
            file=sys.stderr,
        )
    # Belt-and-suspenders: catch any sessions the queue missed.
    cmd_catchup(verbose=False)


HEDGE_WORDS = {
    "maybe", "perhaps", "possibly", "might", "could", "seems", "somewhat",
    "kind", "sort", "fairly", "rather", "pretty", "i think", "i guess",
    "i suppose", "probably", "arguably", "potentially", "presumably",
}
CHEERLEAD_WORDS = {
    "great", "awesome", "fantastic", "amazing", "perfect", "excellent",
    "love", "absolutely", "definitely", "totally", "wonderful",
}
PROFANITY = {"fuck", "shit", "damn", "hell", "crap", "wtf", "bs"}


def _tone_stats(messages: list[str]) -> dict:
    """Compute tone fingerprint from a list of raw user-turn texts."""
    if not messages:
        return {}
    sentence_lens: list[int] = []
    word_count = 0
    hedge_hits = 0
    cheerlead_hits = 0
    profanity_hits = 0
    question_count = 0
    imperative_starts = 0
    lowercase_starts = 0
    opening_verbs: Counter = Counter()
    msg_count = 0

    for text in messages:
        text = text.strip()
        if not text:
            continue
        msg_count += 1
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        for s in sentences:
            words = re.findall(r"\b\w+\b", s)
            if not words:
                continue
            sentence_lens.append(len(words))
            word_count += len(words)
            if s.rstrip().endswith("?"):
                question_count += 1
            first = words[0].lower()
            if first[:1].isalpha() and not s[0].isupper():
                lowercase_starts += 1
            # Imperative heuristic: starts with a bare verb (no subject)
            if first in {
                "do", "don't", "use", "stop", "drop", "remove", "add",
                "make", "keep", "let", "go", "skip", "fix", "check",
                "build", "ship", "push", "merge", "delete", "rename",
                "move", "rewrite", "rework",
            }:
                imperative_starts += 1
                opening_verbs[first] += 1
        low = text.lower()
        for h in HEDGE_WORDS:
            if h in low:
                hedge_hits += 1
                break  # one hit per message
        for c in CHEERLEAD_WORDS:
            if re.search(r"\b" + re.escape(c) + r"\b", low):
                cheerlead_hits += 1
                break
        for p in PROFANITY:
            if re.search(r"\b" + re.escape(p) + r"\b", low):
                profanity_hits += 1
                break

    if not sentence_lens:
        return {}
    sentence_lens.sort()
    median_len = sentence_lens[len(sentence_lens) // 2]
    p90_len = sentence_lens[int(len(sentence_lens) * 0.9)]

    return {
        "messages": msg_count,
        "sentences": len(sentence_lens),
        "median_sentence_words": median_len,
        "p90_sentence_words": p90_len,
        "hedge_rate_pct": round(100 * hedge_hits / msg_count, 1),
        "cheerlead_rate_pct": round(100 * cheerlead_hits / msg_count, 1),
        "profanity_rate_pct": round(100 * profanity_hits / msg_count, 1),
        "question_rate_pct": round(100 * question_count / len(sentence_lens), 1),
        "imperative_start_rate_pct": round(100 * imperative_starts / len(sentence_lens), 1),
        "lowercase_start_rate_pct": round(100 * lowercase_starts / len(sentence_lens), 1),
        "top_imperative_openers": opening_verbs.most_common(10),
    }


def _signal_age_days(ts: str) -> float | None:
    """Days between a signal's recorded timestamp and now. Returns None for
    unparseable timestamps (legacy rows)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _decay_weight(age_days: float | None) -> float:
    """Recency weight in [0.1, 1.0]. Decays linearly from 1.0 at 0d to
    0.1 at 365d, floor at 0.1 thereafter. Unknown-age rows get 0.5 — they
    exist but should not dominate fresh signals."""
    if age_days is None:
        return 0.5
    if age_days <= 0:
        return 1.0
    if age_days >= 365:
        return 0.1
    return 1.0 - (age_days / 365.0) * 0.9


def classify_prompt_intent(prompt: str) -> list[tuple[str, str]]:
    """Predict failure modes a user PROMPT is likely to elicit from Claude.
    Different from classify_situation: that operates on drafts (does this
    response contain a failure?), this operates on prompts (does this prompt
    invite a failure?). Designed for the UserPromptSubmit hook.

    Pattern catalog calibrated against the 10-prompt adversarial set —
    elicitation shapes Nino's corpus shows Claude routinely falls into."""
    hits: list[tuple[str, str]] = []
    t = prompt.strip()
    if not t:
        return hits
    low = t.lower()

    # User asks Claude to break work into steps with check-ins.
    # Strong predictor of mid-task hesitation questions.
    if re.search(r"\b(walk me through|lay out (each|the) step|step by step|"
                 r"let me know (when|if) (you|to|i)|check in (with me|along)|"
                 r"each step|ready to (continue|proceed|move))\b", low):
        hits.append(("invites-hesitation", "step-by-step + check-in language"))

    # User uses hedge words. Claude tends to mirror — hedge in = hedge out.
    user_hedge_count = sum(
        1 for h in HEDGE_WORDS if re.search(r"\b" + re.escape(h) + r"\b", low)
    )
    if user_hedge_count >= 2:
        hits.append(("invites-hedging", f"{user_hedge_count} hedge words in prompt"))

    # User invites evaluation / asks for praise-shaped response.
    if re.search(r"\b(honest thoughts|honest opinion|honest review|what do you think|"
                 r"how (does it|do you) look|review (my|the|this)|give me your|"
                 r"thoughts\?)\b", low):
        hits.append(("invites-cheerleading", "evaluation request shape"))

    # User asks open-ended "design / architect / build" with no constraints.
    # Predicts over-engineering — Claude enumerates every edge case.
    # Matches "design an auth system", "build a payment service", etc. —
    # an optional qualifier word between determiner and the structural noun
    # widens coverage beyond bare "design an app".
    if re.search(
        r"\b(design|architect|build|create) (an?|the) (\w+ )?"
        r"(system|app|service|api|platform|tool|architecture|framework|"
        r"flow|pipeline|workflow|module|component|library|scheme)\b",
        low,
    ):
        if not re.search(r"\b(only|just|simple|minimal|small|tiny|quick|stub|sketch)\b", low):
            hits.append(("invites-over-engineering", "open-ended design ask"))

    # User asks for an "explain" with no audience or depth constraint.
    # Predicts vague rationale / generic textbook prose.
    if re.search(r"^\s*(explain|describe|walk me through)\b", low):
        if not re.search(r"\b(why|because|so that|for the purpose|in (one|two) "
                         r"(sentence|paragraph|line)|short(ly)?|brief)\b", low):
            hits.append(("invites-vague-rationale", "explain without depth/audience constraint"))

    # User explicitly mentions destructive ops.
    if re.search(r"\b(delete|remove|drop|reset|force[ -]push|truncate|"
                 r"clean ?up (my|the) (git|repo|branches|database))\b", low):
        # Strong destructive language — pause regardless of prior context.
        hits.append(("invites-destructive-action", "user requested destructive op"))

    # User asks "what should I do?" / "what's the right approach?" — open-ended
    # vague rationale invitation, and combined with hedge often produces churn.
    if re.search(r"\b(what (should|do) (i|we)|what'?s the right (approach|way)|"
                 r"how should (we|i)|where (to|should i) start)\b", low):
        hits.append(("invites-vague-rationale", "open-ended 'what should we' framing"))

    return hits


PROMPT_GUIDANCE = {
    "invites-hesitation": (
        "Prompt invites step-by-step check-ins. Nino's corpus has 293 cases "
        "of overriding hesitation questions with a single 'go'. Lay out the "
        "full plan in one pass; do not ask for permission between steps."
    ),
    "invites-hedging": (
        "Prompt contains hedge words. Do NOT mirror — Nino's tone is "
        "concrete, not hedged. Replace 'maybe'/'might' with 'X if Y, "
        "otherwise Z'."
    ),
    "invites-cheerleading": (
        "Prompt invites evaluation. Skip praise words. Lead with the "
        "load-bearing critique or the load-bearing affirmation, in that "
        "order. No 'great', 'amazing', 'absolutely'."
    ),
    "invites-over-engineering": (
        "Open-ended design ask. Nino has 14 documented pushbacks on "
        "over-engineering. Start with the minimum viable shape for the "
        "stated case. Do NOT pre-emptively enumerate edge cases."
    ),
    "invites-vague-rationale": (
        "Open 'explain' / 'what should we do'. Anchor the response in the "
        "specific situation rather than generic theory. If you cannot tell "
        "the situation, ask ONE clarifying question — not three."
    ),
    "invites-destructive-action": (
        "User mentioned a destructive op. Confirm scope before executing "
        "even if authorization seems implicit. The 'still ask when' clause "
        "in the decision-bias rule applies."
    ),
}


def classify_situation(text: str) -> list[tuple[str, str]]:
    """Classify a draft response or prompt into Poe-relevant situations.
    Returns list of (situation_id, evidence) tuples — one text can match
    multiple situations. Designed to be cheap (regex-only) so it can run
    in hooks and tight loops."""
    hits: list[tuple[str, str]] = []
    t = text.strip()
    if not t:
        return hits
    low = t.lower()
    tail = t[-400:]

    # 1) Hesitation — the highest-volume failure mode. Two shapes:
    #    a) Explicit question form ("want me to...?", "should I...?")
    #    b) Statement-form soft closer ("ready when you are", "say the word")
    #    Both functionally request permission; the second is harder to spot
    #    because it isn't grammatically a question. Both live here so the
    #    post-response Stop hook (anti-hesitation.py) can share this catalog.
    hesitation_patterns = [
        r"\b(want me to|should I|do you want|shall I|would you like|let me know)\b[^.]*\?",
        r"\b(proceed|continue|keep going|go ahead|move on)\b[^.]*\?",
        r"\b(stop here|pause|hold|wait)\b[^.]*\?",
        r"\bor (should|do|stop)\b[^.]*\?",
        # Statement-form soft closers — anchored to end-of-text/sentence so
        # mid-prose mentions don't false-positive. \s+ (not \s) tolerates
        # blank lines between the prior sentence and the closer.
        r"(?:^|[.!]\s+)(ready when you are|say the word|let me know when|"
        r"when you'?re ready|on your signal|whenever you'?re ready|"
        r"flag me when|ping me when)\b[^.!?]{0,80}[.!?]?\s*$",
    ]
    for p in hesitation_patterns:
        m = re.search(p, tail, re.IGNORECASE)
        if m:
            hits.append(("hesitation", m.group(0).strip()))
            break

    # 2) Over-explanation / hedge density.
    hedge_count = sum(1 for h in HEDGE_WORDS if re.search(r"\b" + re.escape(h) + r"\b", low))
    if hedge_count >= 3:
        hits.append(("hedge-dense", f"{hedge_count} hedge words"))

    # 3) Cheerleading.
    cheer = [c for c in CHEERLEAD_WORDS if re.search(r"\b" + re.escape(c) + r"\b", low)]
    if cheer:
        hits.append(("cheerleading", ", ".join(cheer[:3])))

    # 4) Destructive-action signals (the rule's "still ask when").
    # Note: patterns starting with `--` can't use \b before the dashes since
    # `--` is two non-word chars — \b requires a word/non-word boundary.
    destruct = re.search(
        r"(?:\b(?:force[ -]push|rm -rf|drop (?:table|database)|delete branch|"
        r"reset --hard|amend (?:the )?published|truncate (?:table|database))\b"
        r"|(?:^|\s)--(?:no-verify|force|hard))",
        low,
    )
    if destruct:
        hits.append(("destructive-action", destruct.group(0).strip()))

    # 5) Vague rationale — assertions with no "because" or concrete grounding.
    # Threshold of 200 chars: shorter than that, terseness is fine; longer,
    # absence of any causal connective is suspicious. Calibrated against the
    # golden negative cases — technical explanations naturally use "because"
    # / "since" / etc., so the false-positive risk is low.
    if len(t) > 200 and not re.search(r"\b(because|since|due to|the reason|otherwise|so that)\b", low):
        hits.append(("vague-rationale", f"no causal connective in {len(t)}-char draft"))

    # 6) Over-engineering tell: enumerating many "what if" branches or future
    #    needs in a single response.
    whatif = len(re.findall(r"\bwhat if\b", low))
    if whatif >= 2:
        hits.append(("over-engineered", f"{whatif} 'what if' branches"))

    # 7) Trailing summary: closing with "Summary:" or "To summarize" or "In short" —
    #    Nino explicitly said don't do this.
    if re.search(r"\b(in summary|to summarize|in short|tldr|tl;dr|to recap)\b", tail, re.IGNORECASE):
        hits.append(("trailing-summary", "closing recap pattern"))

    return hits


SITUATION_GUIDANCE = {
    "hesitation": (
        "**Hesitation question detected.** The CLAUDE.md decision-bias rule "
        "prohibits this. The corpus has 293 cases of Claude asking exactly this "
        "shape of question and Nino answering with a single 'go'/'proceed'. "
        "Restate as a status sentence: 'Doing X next — flag if you want to stop.'"
    ),
    "hedge-dense": (
        "**Hedge density above tolerance.** Nino's corpus shows ~11% hedge "
        "rate; this draft is higher. Replace 'maybe', 'might', 'I think' with "
        "concrete assertions or explicit qualifiers ('with caveat X')."
    ),
    "cheerleading": (
        "**Cheerleading word detected.** Nino's corpus has <2% praise-word "
        "rate. Replace with neutral evaluation: 'great' → 'works', "
        "'perfect' → 'matches', 'absolutely' → drop."
    ),
    "destructive-action": (
        "**Destructive action mentioned.** The decision-bias rule's "
        "'still ask when' clause applies — confirm before executing even if "
        "the broader thread was authorized."
    ),
    "vague-rationale": (
        "**Long draft with no causal connective.** Nino's rationale signals "
        "use 'because', 'otherwise', 'the reason is' liberally. Add the "
        "load-bearing 'why' or trim the assertion."
    ),
    "over-engineered": (
        "**Multiple 'what if' branches.** Nino has 14 documented pushbacks on "
        "over-engineering. State the case the user actually has; defer "
        "hypothetical branches until they arise."
    ),
    "trailing-summary": (
        "**Trailing summary closing.** Nino has a saved memory: don't "
        "summarize what you just did — the diff is visible. Cut the recap."
    ),
}


def cmd_poe_check(text: str | None, limit: int) -> None:
    """Classify a draft against Poe situations and surface relevant signals.
    Reads draft from stdin if --text not provided. Output is markdown."""
    if text is None:
        text = sys.stdin.read()
    if not text.strip():
        print("poe-check: empty input. Pass text via stdin or --text.", file=sys.stderr)
        sys.exit(2)

    situations = classify_situation(text)
    print("# Poe check")
    print()
    if not situations:
        print("_No Poe red flags detected in this draft._")
        return

    print(f"_{len(situations)} situation(s) detected._")
    print()
    for sit_id, evidence in situations:
        guidance = SITUATION_GUIDANCE.get(sit_id, "")
        print(f"## `{sit_id}`")
        print()
        print(f"**Evidence:** `{evidence}`")
        print()
        if guidance:
            print(guidance)
            print()

    # Pull representative corpus signals to ground the warnings.
    if not RECALL_DB.exists():
        return
    conn = db_connect()
    sit_to_labels = {
        "hesitation": ["redirect-go"],
        "hedge-dense": ["i-prefer"],
        "cheerleading": [],
        "destructive-action": ["undo"],
        "vague-rationale": ["because"],
        "over-engineered": ["over-engineered"],
        "trailing-summary": [],
    }
    relevant_labels: set[str] = set()
    for sit_id, _ in situations:
        relevant_labels.update(sit_to_labels.get(sit_id, []))
    if relevant_labels:
        print("## Grounding signals from corpus")
        print()
        placeholders = ",".join("?" for _ in relevant_labels)
        rows = conn.execute(
            f"""
            SELECT signal_type, label, phrase, project, prior_assistant
            FROM voice_signals
            WHERE label IN ({placeholders})
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (*relevant_labels, limit),
        ).fetchall()
        for stype, label, phrase, project, prior in rows:
            phrase_clean = re.sub(r"\s+", " ", phrase).strip()[:200]
            proj_short = (project or "?").split("/")[-1]
            if label == "redirect-go" and prior:
                tail = re.sub(r"\s+", " ", prior).strip()[-180:]
                print(f"- Claude: \"…{tail}\" → Nino: **\"{phrase_clean}\"** _({proj_short})_")
            else:
                print(f"- \"{phrase_clean}\" _({proj_short}, `{label}`)_")
        print()
    conn.close()


def _collect_memory_indexes() -> list[tuple[Path, str]]:
    """Find all MEMORY.md indexes Claude Code uses for auto-memory.
    Returns list of (path, content) tuples."""
    out = []
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return out
    for memdir in base.glob("*/memory/MEMORY.md"):
        try:
            content = memdir.read_text(errors="replace")
            out.append((memdir, content))
        except OSError:
            continue
    return out


def _memory_topics(indexes: list[tuple[Path, str]]) -> set[str]:
    """Extract memory topics (the file slugs and titles from index lines)."""
    topics: set[str] = set()
    for path, content in indexes:
        for line in content.splitlines():
            # Index lines look like: - [Title](file.md) — one-line hook
            m = re.match(r"-\s*\[([^\]]+)\]\(([^)]+)\)", line)
            if not m:
                continue
            title, slug = m.group(1), m.group(2)
            topics.add(title.lower())
            topics.add(slug.lower().replace(".md", "").replace("_", " ").replace("-", " "))
    return topics


def cmd_memory_link(promote_threshold: int = 5, verbose: bool = False) -> None:
    """Cross-reference voice corpus with auto-memory:
      - Promotion candidates: signal labels that fire ≥N times across multiple
        projects but don't appear in any MEMORY.md index — implicit rules
        worth codifying explicitly.
      - Stale memory candidates: memory entries whose topic terms don't
        match any recent voice signal — possibly outdated.
    """
    if not RECALL_DB.exists():
        print("No DB.", file=sys.stderr)
        return
    indexes = _collect_memory_indexes()
    if not indexes:
        print("No MEMORY.md indexes found.", file=sys.stderr)
    memory_topics = _memory_topics(indexes)

    conn = db_connect()
    label_rows = conn.execute(
        """
        SELECT signal_type, label,
               COUNT(*) AS total,
               COUNT(DISTINCT project) AS proj_count,
               MAX(timestamp) AS last_seen
        FROM voice_signals
        WHERE timestamp != ''
        GROUP BY signal_type, label
        HAVING total >= ?
        ORDER BY total DESC
        """,
        (promote_threshold,),
    ).fetchall()

    print("# Poe ↔ MEMORY cross-link report")
    print()
    print(f"_Memory indexes scanned: {len(indexes)}_")
    print(f"_Memory topic terms: {len(memory_topics)}_")
    print()

    print(f"## Promotion candidates (labels firing ≥{promote_threshold}× without explicit memory)")
    print()
    print("_These are implicit rules. If you find one valuable, codify it as an explicit memory._")
    print()
    promo = []
    for stype, label, total, proj_count, last_seen in label_rows:
        # Generate fuzzy keys to test against memory topics.
        keys = {label.replace("-", " "), label.replace("_", " "), label}
        if any(k.lower() in memory_topics or
               any(k.lower() in t for t in memory_topics) for k in keys):
            continue
        # Require cross-project recurrence for promotion-worthiness.
        if proj_count < 2:
            continue
        promo.append((stype, label, total, proj_count, last_seen))
    for stype, label, total, proj_count, last_seen in promo[:20]:
        age = _signal_age_days(last_seen) or 0
        print(f"- `{stype}/{label}` — {total} signals across {proj_count} projects, last {int(age)}d ago")
    if not promo:
        print("- _(none — all recurring labels have memory coverage)_")
    print()

    # Stale memory: memories whose slug terms don't appear in any *recent* signal.
    print("## Memory entries with no recent voice reinforcement")
    print()
    print("_Memory topics that don't appear in voice signals from the last 90 days. May be stale; verify before relying on them._")
    print()
    recent_phrases = conn.execute(
        "SELECT phrase, message FROM voice_signals "
        "WHERE timestamp >= ? ",
        ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),),
    ).fetchall()
    haystack = " ".join(
        (p or "").lower() + " " + (m or "").lower() for p, m in recent_phrases
    )
    stale = []
    for path, content in indexes:
        for line in content.splitlines():
            m = re.match(r"-\s*\[([^\]]+)\]\(([^)]+)\)\s*[—-]+\s*(.*)", line)
            if not m:
                continue
            title, slug, hook = m.group(1), m.group(2), m.group(3)
            slug_terms = [
                t for t in re.split(r"[_\-\s]+", slug.replace(".md", "").lower())
                if len(t) >= 4
            ]
            if not slug_terms:
                continue
            if not any(term in haystack for term in slug_terms):
                stale.append((title, slug, str(path.parent.parent.name)))
    for title, slug, proj in stale[:20]:
        print(f"- **{title}** (`{slug}`) — _{proj}_")
    if not stale:
        print("- _(none — every memory has recent voice reinforcement)_")
    print()

    conn.close()


def cmd_drift(verbose: bool = False) -> None:
    """Report preference labels that haven't been reinforced lately.
    A 'drift candidate' is a label that:
      - has historical signals (≥3 total occurrences)
      - has not seen a new signal in 90+ days
    These may be stale rules to review or remove."""
    if not RECALL_DB.exists():
        print("No DB.", file=sys.stderr)
        return
    conn = db_connect()
    rows = conn.execute(
        """
        SELECT signal_type, label,
               COUNT(*) AS total,
               MAX(timestamp) AS last_seen
        FROM voice_signals
        WHERE timestamp != ''
        GROUP BY signal_type, label
        HAVING total >= 3
        ORDER BY last_seen ASC
        """
    ).fetchall()
    conn.close()
    if not rows:
        print("No drift data — corpus is empty or missing timestamps.", file=sys.stderr)
        return

    drift: list[tuple[str, str, int, float]] = []
    fresh: list[tuple[str, str, int, float]] = []
    for stype, label, total, last_seen in rows:
        age = _signal_age_days(last_seen)
        if age is None:
            continue
        if age >= 90:
            drift.append((stype, label, total, age))
        else:
            fresh.append((stype, label, total, age))

    print("# Poe drift report")
    print()
    print(f"_Labels with ≥3 historical signals, sorted by staleness._")
    print()
    print(f"## Drift candidates ({len(drift)})")
    print()
    print("_No reinforcement in 90+ days — review whether the rule still applies._")
    print()
    for stype, label, total, age in drift[:25]:
        print(f"- `{stype}/{label}` — {total} signals, last seen {int(age)}d ago")
    if not drift:
        print("- _(none — all active labels reinforced within 90 days)_")
    print()
    if verbose:
        print(f"## Active labels ({len(fresh)})")
        print()
        for stype, label, total, age in fresh[:20]:
            print(f"- `{stype}/{label}` — {total} signals, last seen {int(age)}d ago")
        print()


def _render_validated_section() -> list[str]:
    """Render a 'response shapes Nino tolerated' section using the
    validated_responses corpus. Highlights:
      - distribution of response sizes that didn't draw correction
      - shape signatures with the highest pass rate
    Read directly from DB to keep the assemble path linear."""
    if not RECALL_DB.exists():
        return []
    conn = sqlite3.connect(str(RECALL_DB))
    try:
        total = conn.execute("SELECT COUNT(*) FROM validated_responses").fetchone()[0]
    except sqlite3.OperationalError:
        conn.close()
        return []
    if total == 0:
        conn.close()
        return []

    size_rows = conn.execute(
        """
        SELECT substr(shape_signature, 1, instr(shape_signature, '/') - 1) AS bucket,
               COUNT(*) c
        FROM validated_responses
        GROUP BY bucket
        ORDER BY c DESC
        """
    ).fetchall()
    follow_rows = conn.execute(
        "SELECT follow_label, COUNT(*) FROM validated_responses GROUP BY follow_label ORDER BY 2 DESC"
    ).fetchall()
    top_shapes = conn.execute(
        """
        SELECT shape_signature, COUNT(*) c
        FROM validated_responses
        GROUP BY shape_signature
        ORDER BY c DESC
        LIMIT 4
        """
    ).fetchall()
    # Pull one short exemplar per top shape for human readability.
    exemplars: list[tuple[str, str, int]] = []
    for sig, _ in top_shapes:
        row = conn.execute(
            "SELECT response_text, response_chars FROM validated_responses "
            "WHERE shape_signature = ? ORDER BY response_chars ASC LIMIT 1",
            (sig,),
        ).fetchone()
        if row:
            exemplars.append((sig, row[0], row[1]))
    conn.close()

    lines = []
    lines.append("## Tolerated response shapes (reverse Poe)")
    lines.append("")
    lines.append(
        "_Mined from assistant turns Nino did NOT correct. These are response "
        "structures that survived contact — Claude can mirror them without "
        "triggering a pushback. Inverse of the Red Lines section: those say "
        "what fails, these say what works._"
    )
    lines.append("")
    lines.append(f"- **Validated turns**: {total}")
    if size_rows:
        size_summary = ", ".join(f"{bucket}={c}" for bucket, c in size_rows)
        lines.append(f"- **Size distribution**: {size_summary} _(xs<200ch, s<600, m<1500, l<4000, xl≥4000)_")
    if follow_rows:
        follow_summary = ", ".join(f"{(l or 'neutral')}={c}" for l, c in follow_rows)
        lines.append(f"- **Follow-up classification**: {follow_summary}")
    lines.append("")
    if exemplars:
        lines.append("### Most common shape signatures")
        lines.append("")
        lines.append("_Signature legend: `size/sentence-bucket/ends-with-Q-or-period + L=list + C=code`_")
        lines.append("")
        for sig, text, chars in exemplars:
            preview = re.sub(r"\s+", " ", text[:160]).strip()
            if len(text) > 160:
                preview += "…"
            lines.append(f"- **`{sig}`** ({chars} chars): \"{preview}\"")
        lines.append("")
    return lines


def _render_tone_section(stats: dict) -> list[str]:
    """Return markdown lines for the tone card section of stack.md."""
    if not stats:
        return []
    lines = []
    lines.append("## Tone fingerprint")
    lines.append("")
    lines.append(
        "_Quantitative voice card. Match this cadence when speaking as or for Nino: "
        "short sentences, low hedge rate, imperative openings, no cheerleading._"
    )
    lines.append("")
    lines.append(f"- **Corpus**: {stats['messages']} user messages, {stats['sentences']} sentences")
    lines.append(f"- **Median sentence length**: {stats['median_sentence_words']} words (p90: {stats['p90_sentence_words']})")
    lines.append(f"- **Hedge rate**: {stats['hedge_rate_pct']}% of messages contain hedge words (\"maybe\", \"might\", \"I think\")")
    lines.append(f"- **Cheerleading rate**: {stats['cheerlead_rate_pct']}% contain praise words (\"great\", \"awesome\", \"perfect\")")
    lines.append(f"- **Profanity rate**: {stats['profanity_rate_pct']}%")
    lines.append(f"- **Question rate**: {stats['question_rate_pct']}% of sentences end with '?'")
    lines.append(f"- **Imperative-opener rate**: {stats['imperative_start_rate_pct']}% of sentences start with a bare verb")
    lines.append(f"- **Lowercase-first-letter rate**: {stats['lowercase_start_rate_pct']}% (informal/typing-style)")
    if stats["top_imperative_openers"]:
        top = ", ".join(f"`{v}` ({c})" for v, c in stats["top_imperative_openers"][:8])
        lines.append(f"- **Top imperative verbs**: {top}")
    lines.append("")
    lines.append("**Style implications when responding as Poe:**")
    lines.append("- Sentences average ~10–15 words; cap at 25 unless quoting.")
    lines.append("- No \"I think\" / \"perhaps\" / \"maybe\" — assert or qualify with a concrete reason.")
    lines.append("- No \"great\" / \"perfect\" / \"absolutely\" — replace with neutral evaluation.")
    lines.append("- Start instructions with the verb, not the subject.")
    lines.append("")
    return lines


def cmd_hook_stats(days: int = 7) -> None:
    """Summarize hook.log over the last N days. Reports fire rate, situation
    distribution, top surfaced signals, and silent-rate."""
    if not HOOK_LOG_PATH.exists():
        print(f"No hook log at {HOOK_LOG_PATH} yet.", file=sys.stderr)
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    emitted = 0
    intent_counts: Counter = Counter()
    situation_counts: Counter = Counter()
    signal_counts: Counter = Counter()
    with open(HOOK_LOG_PATH, "r", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(entry["ts"])
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            total += 1
            if entry.get("emitted"):
                emitted += 1
            for i in entry.get("intents", []):
                intent_counts[i] += 1
            for s in entry.get("situations", []):
                situation_counts[s] += 1
            for sig in entry.get("signals", []):
                signal_counts[sig] += 1
    if total == 0:
        print(f"No hook activity in the last {days} days.", file=sys.stderr)
        return
    print(f"# Poe hook stats — last {days} days")
    print()
    print(f"- **Total invocations**: {total}")
    print(f"- **Emitted context**: {emitted} ({round(100*emitted/total,1)}%)")
    print(f"- **Silent (no signal)**: {total - emitted} ({round(100*(total-emitted)/total,1)}%)")
    print()
    if intent_counts:
        print("## Prompt intents detected")
        print()
        for intent, count in intent_counts.most_common():
            print(f"- `{intent}` — {count}")
        print()
    if situation_counts:
        print("## Situations detected (in-draft patterns)")
        print()
        for sit, count in situation_counts.most_common():
            print(f"- `{sit}` — {count}")
        print()
    if signal_counts:
        print("## Top surfaced signals")
        print()
        for sig, count in signal_counts.most_common(15):
            print(f"- `{sig}` — {count}")
        print()


# Retrieval bar. The hook's contract is "silence is the default — noise on every
# prompt is worse than no hook at all", but measured emission was 99.2% over
# 1244 invocations (30d to 2026-07-24). Two causes, both fixed below:
#   1. The FTS index covers `message` — the whole user turn, up to 4000 chars —
#      so one token landing anywhere inside a long paste surfaced the signal.
#      Matching is now scoped to `phrase`, the extracted sentence itself.
#   2. The query was OR across tokens, so a single common word was enough.
#      A candidate must now share MIN_TOKEN_OVERLAP distinct prompt tokens with
#      the phrase (or one long, specific token), and clear a bm25 floor.
# Overlap is checked on word-prefixes because the FTS tokenizer is porter-stemmed
# ("retrieval" must still count against "retrieve").
# Threshold picked by replaying 3639 real prompts from hook.log, not guessed.
# Emission: overlap>=3 -> 34.4%, >=4 -> 6.7%, >=5 -> 1.3%. Four is the knee.
# At 3 the survivors are word coincidence ("migrating our backend to TypeScript"
# pulled up "bc migraiton is out"); at 4 they're on topic ("urvil wants me to add
# stripe" pulled up "defer stripe config for now").
#
# Deliberately NOT thresholding on bm25. bm25 scores depend on corpus-wide IDF,
# so any absolute floor tuned against this 650-signal corpus would mute a fresh
# or small one entirely — it broke the in-memory test DB immediately. Token
# overlap is corpus-size independent and does the semantic work anyway.
#
# Consequence worth knowing: a prompt with fewer than MIN_TOKEN_OVERLAP content
# words can never surface a prior. That is intended — short prompts carry too
# little topic to match on.
MIN_TOKEN_OVERLAP = 4
OVERLAP_PREFIX = 6          # word-prefix length; FTS is porter-stemmed
NEAR_DUP_RATIO = 0.75       # candidate is ~the prompt restated: no information


def _phrase_overlap(phrase: str, tokens: list[str]) -> int:
    """Count distinct prompt tokens appearing as word-prefixes in the phrase."""
    words = [w for w in re.split(r"\W+", phrase.lower()) if w]
    return sum(
        1 for t in tokens
        if any(w.startswith(t[:OVERLAP_PREFIX]) for w in words)
    )


def _is_near_duplicate(phrase: str, prompt: str) -> bool:
    """True when the candidate is essentially the prompt said again.

    A prior that restates what Nino just typed carries no information — it
    reads as insight but is an echo. Word-set containment rather than string
    similarity, so reordering and light edits still count as the same thing.
    """
    a = {w for w in re.split(r"\W+", phrase.lower()) if len(w) >= 4}
    b = {w for w in re.split(r"\W+", prompt.lower()) if len(w) >= 4}
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= NEAR_DUP_RATIO


def _relevant_signals(
    conn: sqlite3.Connection,
    tokens: list[str],
    floor_iso: str,
    current_session: str,
    limit: int = 4,
    prompt: str = "",
) -> list[tuple]:
    """FTS-match prompt tokens against SETTLED voice signals, deduped by label.

    Two guards keep one session's live context from leaking into another's as a
    "prior" (the 2026-05-29 cross-session-leak class):
      - recency floor: skip signals whose timestamp is newer than floor_iso, so
        a fresh message from a concurrent in-flight session is never surfaced;
      - self-echo: skip signals from current_session.
    Timestamps are compared on their first 19 chars (YYYY-MM-DDTHH:MM:SS) so the
    UTC 'Z' suffix doesn't break lexical ordering. Dedupe by label so a
    high-volume label (e.g. 'instead') can't dominate the top-K.

    Relevance bar: see MIN_TOKEN_OVERLAP above. A candidate that merely matched
    the FTS query is not enough — it has to actually be about the same thing.
    """
    if not tokens:
        return []
    # Scope the match to `phrase`; `message` carries pasted content and is noise.
    fts_query = "phrase : (" + " OR ".join(tokens[:5]) + ")"
    try:
        rows = conn.execute(
            """
            SELECT v.signal_type, v.label, v.phrase, v.project,
                   bm25(voice_signals_fts) AS score
            FROM voice_signals_fts f
            JOIN voice_signals v ON v.id = f.rowid
            WHERE voice_signals_fts MATCH ?
              AND v.signal_type IN ('correction', 'preference', 'rejection')
              AND (v.timestamp = '' OR substr(v.timestamp, 1, 19) < ?)
              AND v.session_id != ?
            ORDER BY score
            LIMIT 50
            """,
            (fts_query, floor_iso, current_session),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    scored = []
    for stype, label, phrase, project, _score in rows:
        if "[Image #" in phrase:
            continue
        if _phrase_overlap(phrase, tokens) < MIN_TOKEN_OVERLAP:
            continue
        if prompt and _is_near_duplicate(phrase, prompt):
            continue
        scored.append((stype, label, phrase, project))
    rows = scored
    out: list[tuple] = []
    seen_labels: set[str] = set()
    for row in rows:
        label = row[1]
        if label in seen_labels:
            continue
        seen_labels.add(label)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def cmd_prompt_hook() -> None:
    """UserPromptSubmit hook entry point. Reads Claude Code hook JSON from
    stdin, surfaces a compact Poe context block on stdout that gets injected
    into the assistant's view of the prompt. Must be fast (<200ms typical)
    since it runs on every user prompt.

    Design: only emit output when there's a load-bearing signal. Silence is
    the default — noise on every prompt is worse than no hook at all."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    prompt = payload.get("prompt") or payload.get("user_message") or ""
    if not prompt or len(prompt) < 20:
        return
    # Agent-generated prompts (task notifications, teammate messages) are not
    # Nino's voice — don't classify, retrieve, or log them. Same catalog the
    # bulk extractor uses, so both ingestion paths stay in sync.
    if prompt.lstrip().startswith(NOISE_PREFIXES):
        return

    # Prompt-intent classification predicts elicitation shapes; situation
    # classification catches drafts containing failure patterns directly
    # (rare in user prompts, but cheap to also check).
    prompt_intents = classify_prompt_intent(prompt)
    situations = classify_situation(prompt)

    # Topic-driven retrieval: extract content words from the prompt, query Poe.
    tokens = [
        t for t in re.split(r"\W+", prompt.lower())
        if len(t) >= 4 and t not in {
            "this", "that", "with", "from", "have", "will", "would", "should",
            "could", "what", "when", "where", "which", "their", "these", "those",
            "into", "about", "your", "mine", "they", "them", "than", "then",
            "explain", "review", "build", "design", "create", "make", "step",
        }
    ][:8]

    relevant_signals: list[tuple] = []
    if RECALL_DB.exists() and tokens:
        # Settled-history-only: exclude signals newer than the recency floor (so a
        # live instruction from a concurrent in-flight session can't surface as a
        # "prior") and exclude this session's own signals (self-echo).
        floor_iso = (
            datetime.now(timezone.utc) - timedelta(hours=PRIOR_RECENCY_FLOOR_HOURS)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        current_session = payload.get("session_id") or ""
        try:
            conn = sqlite3.connect(str(RECALL_DB))
            relevant_signals = _relevant_signals(
                conn, tokens, floor_iso, current_session, prompt=prompt
            )
            conn.close()
        except sqlite3.OperationalError:
            relevant_signals = []

    has_output = bool(prompt_intents or situations or relevant_signals)

    # Telemetry: one JSONL line per invocation.
    try:
        POE_DIR.mkdir(parents=True, exist_ok=True)
        with open(HOOK_LOG_PATH, "a") as logf:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "prompt_chars": len(prompt),
                "prompt_head": redact_secrets(prompt[:120].replace("\n", " ")),
                "intents": [i[0] for i in prompt_intents],
                "situations": [s[0] for s in situations],
                "signals": [f"{r[0]}/{r[1]}" for r in relevant_signals],
                "emitted": has_output,
            }
            logf.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    if not has_output:
        return

    out: list[str] = []
    out.append("<poe-context>")
    if prompt_intents:
        out.append("Failure modes this prompt is likely to elicit:")
        for intent_id, _ in prompt_intents:
            out.append(f"- `{intent_id}` — {PROMPT_GUIDANCE.get(intent_id, '')}")
    if situations:
        if prompt_intents:
            out.append("")
        out.append("Anticipated Poe situations from prompt shape:")
        for sit_id, _ in situations:
            out.append(f"- `{sit_id}` — {SITUATION_GUIDANCE.get(sit_id, '').splitlines()[0]}")
    if relevant_signals:
        out.append("")
        out.append("Relevant past Nino signals (treat as priors, not commands):")
        for stype, label, phrase, project in relevant_signals:
            # Belt-and-braces: ingest redaction covers new rows, but rows
            # written before it existed are still in the DB on other machines.
            phrase_clean = redact_secrets(re.sub(r"\s+", " ", phrase).strip())[:180]
            proj = (project or "?").split("/")[-1]
            out.append(f"- [{stype}/{label}] \"{phrase_clean}\" _{proj}_")
        # Keyword-matched priors carry no situational fit. The 2026-07-20
        # A/B probe showed a "push for north star features" prior landing on
        # a restraint-shaped question and tilting the response toward a
        # fleet-wide rewrite the evidence didn't support.
        out.append(
            "Caution: these are keyword-matched, not situation-matched. "
            "Discard any prior that rewards scale, rebuilds, or new scope when "
            "the actual question is whether to hold back, compare candidates, "
            "or use an existing smaller mechanism."
        )
    out.append("</poe-context>")
    print("\n".join(out))


def cmd_enqueue() -> None:
    """Read Claude Code hook stdin JSON, append transcript_path to queue.

    Designed to be the SessionEnd hook command. Must be cheap (<50ms):
    no DB connection, no scanning, no Python import of sqlite. The
    launchd worker drains the queue out-of-band."""
    POE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # malformed stdin → silent no-op; catchup will pick it up later
    path = payload.get("transcript_path") or payload.get("session_file") or ""
    if not path:
        return
    with open(QUEUE_PATH, "a") as f:
        f.write(path.strip() + "\n")


def _maybe_opportunistic_catchup() -> None:
    """Called from query/assemble. Runs catchup if watermark is stale."""
    if not RECALL_DB.exists():
        cmd_catchup(verbose=False)
        return
    conn = sqlite3.connect(str(RECALL_DB))
    try:
        last = conn.execute(
            "SELECT value FROM ingest_meta WHERE key = 'last_catchup'"
        ).fetchone()
    except sqlite3.OperationalError:
        last = None
    conn.close()
    if not last:
        cmd_catchup(verbose=False)
        return
    try:
        last_dt = datetime.fromisoformat(last[0])
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    except ValueError:
        age = float("inf")
    if age > READ_PATH_STALE_SECONDS:
        cmd_catchup(verbose=False)


def cmd_extract(limit: int | None, since_days: int | None, session: str | None) -> None:
    POE_DIR.mkdir(parents=True, exist_ok=True)

    # Single-session mode: parse one JSONL, upsert to DB, advance watermark.
    # `session is not None` is the gate — empty string is an explicit error,
    # not a fall-through to expensive full-rebuild.
    if session is not None:
        if not session.strip():
            print(
                "extract --session given empty path. Use 'catchup' for a full sweep "
                "or pass a real transcript path.",
                file=sys.stderr,
            )
            sys.exit(2)
        jf = Path(session).expanduser().resolve()
        if not jf.exists():
            print(f"Session file not found: {jf}", file=sys.stderr)
            sys.exit(1)
        conn = db_connect()
        signals, inserted = _ingest_file(conn, jf)
        conn.close()
        print(
            f"Session {jf.stem}: {signals} signals, {inserted} new (watermark advanced)",
            file=sys.stderr,
        )
        return

    cutoff = None
    if since_days is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=since_days)

    files = []
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        for jf in proj_dir.glob("*.jsonl"):
            if cutoff:
                mtime = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            files.append(jf)

    if limit:
        files = files[:limit]

    stats = Counter()
    written = 0

    with open(CORPUS_PATH, "w") as out:
        for i, jf in enumerate(files, 1):
            if i % 200 == 0:
                print(f"  [{i}/{len(files)}] scanned, {written} signals...", file=sys.stderr)

            stats["files_scanned"] += 1
            records = _extract_from_file(jf)
            for rec in records:
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                stats[f"signal:{rec['signal']}"] += 1
                written += 1
            stats["messages_scanned"] += 0  # message stats collapsed into records path

    print(f"\nExtraction complete:", file=sys.stderr)
    print(f"  files scanned:    {stats['files_scanned']}", file=sys.stderr)
    print(f"  signals written:  {written}", file=sys.stderr)
    for k in sorted(stats):
        if k.startswith("signal:"):
            print(f"    {k[7:]:12} {stats[k]}", file=sys.stderr)
    print(f"  corpus: {CORPUS_PATH}", file=sys.stderr)


def cmd_publish() -> None:
    """Load corpus.jsonl into the DB."""
    if not CORPUS_PATH.exists():
        print(f"No corpus at {CORPUS_PATH} — run extract first.", file=sys.stderr)
        sys.exit(1)
    records: list[dict] = []
    with open(CORPUS_PATH) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    conn = db_connect()
    inserted = _upsert_signals(conn, records)
    total = conn.execute("SELECT COUNT(*) FROM voice_signals").fetchone()[0]
    conn.close()
    print(
        f"Published: {len(records)} records, {inserted} new inserts, {total} total in DB",
        file=sys.stderr,
    )


def cmd_init() -> None:
    conn = db_connect()
    count = conn.execute("SELECT COUNT(*) FROM voice_signals").fetchone()[0]
    conn.close()
    print(f"Schema ready at {RECALL_DB} — voice_signals has {count} rows", file=sys.stderr)


def _resolve_project_filter(spec: str) -> str | None:
    """Translate --project value into a SQL LIKE pattern (None if no filter).
    'auto' resolves the current shell cwd. Anything else is matched as a
    case-insensitive suffix on the stored project label."""
    if not spec:
        return None
    if spec.lower() == "auto":
        cwd = str(Path.cwd())
        label = _cwd_to_label(cwd)
        # Match anywhere within the path so /Users/nino/Workspace/dev/wip/foo
        # matches both 'wip/foo' and 'wip/foo/subdir' rows.
        return f"%{label}%"
    return f"%{spec}%"


def cmd_query(terms: list[str], limit: int, project: str | None = None) -> None:
    """FTS5 search voice_signals, emit markdown block ready to paste."""
    _maybe_opportunistic_catchup()
    if not RECALL_DB.exists():
        print(f"No DB at {RECALL_DB} — run publish first.", file=sys.stderr)
        sys.exit(1)
    conn = db_connect()
    raw_query = " ".join(terms).strip()
    if not raw_query:
        print("Query terms required.", file=sys.stderr)
        sys.exit(1)

    # Build an FTS5 expression: tokenize on non-alphanumeric, drop short stops,
    # OR them together. Porter tokenizer handles morphological variation
    # (engineering ↔ engineered ↔ engineer) at index AND query time, so no
    # prefix-asterisk needed — and asterisk would actually bypass stemming.
    tokens = [t for t in re.split(r"\W+", raw_query.lower()) if len(t) >= 3]
    if tokens:
        query = " OR ".join(tokens)
    else:
        # Single short token (e.g. "go") — pass as-is.
        query = raw_query

    proj_filter = _resolve_project_filter(project)

    # FTS5 MATCH with phrase-first ranking
    try:
        if proj_filter:
            rows = conn.execute(
                """
                SELECT v.signal_type, v.label, v.phrase, v.project, v.session_id, v.timestamp, v.prior_assistant
                FROM voice_signals_fts f
                JOIN voice_signals v ON v.id = f.rowid
                WHERE voice_signals_fts MATCH ? AND v.project LIKE ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, proj_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT v.signal_type, v.label, v.phrase, v.project, v.session_id, v.timestamp, v.prior_assistant
                FROM voice_signals_fts f
                JOIN voice_signals v ON v.id = f.rowid
                WHERE voice_signals_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        # Fall back to LIKE if FTS5 syntax rejects the query
        like = f"%{raw_query}%"
        if proj_filter:
            rows = conn.execute(
                """
                SELECT signal_type, label, phrase, project, session_id, timestamp, prior_assistant
                FROM voice_signals
                WHERE (phrase LIKE ? OR message LIKE ?) AND project LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (like, like, proj_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT signal_type, label, phrase, project, session_id, timestamp, prior_assistant
                FROM voice_signals
                WHERE phrase LIKE ? OR message LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()

    conn.close()

    if not rows:
        print(f"No matches for: {raw_query}", file=sys.stderr)
        return

    # Header uses the raw query, not the FTS-encoded form.
    query = raw_query

    by_type: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        by_type[r[0]].append(r)

    print(f"# Poe on: {query}")
    print()
    print(f"_{len(rows)} matching signals from Nino's past sessions._")
    print()
    type_order = ["correction", "preference", "rationale", "rejection", "declaration", "approval"]
    headers = {
        "correction": "## Corrections (what Nino pushed back on)",
        "preference": "## Preferences (stated rules)",
        "rationale": "## Rationale (reasoning given)",
        "rejection": "## Alternatives (what Nino picked instead)",
        "declaration": "## Imperatives",
        "approval": "## Validated calls",
    }
    for t in type_order:
        if t not in by_type:
            continue
        print(headers[t])
        print()
        for stype, label, phrase, project, session_id, ts, prior in by_type[t]:
            phrase_clean = re.sub(r"\s+", " ", phrase).strip()
            proj_short = (project or "?").split("/")[-1] if project else "?"
            if label == "redirect-go" and prior:
                tail = re.sub(r"\s+", " ", prior).strip()[-200:]
                print(f"- Claude: \"…{tail}\" → Nino: **\"{phrase_clean}\"** _({proj_short})_")
            else:
                print(f"- \"{phrase_clean}\" _({proj_short}, `{label}`)_")
        print()


def cmd_assemble(_skip_catchup: bool = False) -> None:
    if not _skip_catchup:
        _maybe_opportunistic_catchup()
    by_signal: dict[str, list[dict]] = defaultdict(list)
    by_signal_label: dict[tuple[str, str], list[dict]] = defaultdict(list)
    projects = Counter()

    # Prefer DB as source of truth; fall back to corpus.jsonl
    if RECALL_DB.exists():
        conn = db_connect()
        db_count = conn.execute("SELECT COUNT(*) FROM voice_signals").fetchone()[0]
    else:
        db_count = 0
        conn = None

    tone_messages: list[str] = []
    if db_count > 0 and conn is not None:
        rows = conn.execute(
            "SELECT signal_type, label, phrase, project, prior_assistant, timestamp FROM voice_signals"
        ).fetchall()
        # Pull a deduped sample of raw messages for tone stats.
        msg_rows = conn.execute(
            "SELECT DISTINCT message FROM voice_signals WHERE message IS NOT NULL AND length(message) > 10"
        ).fetchall()
        tone_messages = [m[0] for m in msg_rows]
        conn.close()
        for stype, label, phrase, project, prior, ts in rows:
            age = _signal_age_days(ts)
            rec = {
                "signal": stype, "label": label, "phrase": phrase,
                "project": project or "?", "prior_assistant": prior or "",
                "age_days": age, "weight": _decay_weight(age),
            }
            by_signal[stype].append(rec)
            by_signal_label[(stype, label)].append(rec)
            projects[rec["project"]] += 1
    else:
        if conn is not None:
            conn.close()
        if not CORPUS_PATH.exists():
            print(f"No DB rows and no corpus at {CORPUS_PATH} — run extract first.", file=sys.stderr)
            sys.exit(1)
        with open(CORPUS_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                by_signal[rec["signal"]].append(rec)
                by_signal_label[(rec["signal"], rec["label"])].append(rec)
                projects[rec.get("project", "?")] += 1

    total = sum(len(v) for v in by_signal.values())

    def dedupe_by_phrase(recs: list[dict], limit: int = 20) -> list[dict]:
        """Keep one rep per near-duplicate phrase. Sort by recency weight so
        the freshest exemplar wins when duplicates exist."""
        recs_sorted = sorted(recs, key=lambda r: r.get("weight", 0.5), reverse=True)
        seen: set[str] = set()
        out = []
        for r in recs_sorted:
            key = re.sub(r"\s+", " ", r["phrase"].lower()).strip()[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= limit:
                break
        return out

    lines: list[str] = []
    lines.append("# Poe — A Serialized Nino")
    lines.append("")
    lines.append(
        "A character stack extracted from prior Claude Code sessions. Load this as "
        "system-prompt context when you want the assistant to vet ideas the way Nino "
        "would — with the same red lines, rationale, and taste."
    )
    lines.append("")
    lines.append(f"- **Corpus size**: {total} signals across {len(projects)} projects")
    lines.append(f"- **Generated**: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("")

    tone_stats = _tone_stats(tone_messages)
    lines.extend(_render_tone_section(tone_stats))
    lines.extend(_render_validated_section())

    section_order = [
        ("correction", "## Red lines — what Nino rejects", "These are patterns where Nino pushed back, corrected, or called something wrong. Treat them as non-negotiables unless the context clearly differs."),
        ("preference", "## Rules — how Nino wants things done", "Explicit conventions Nino has declared. Follow them by default."),
        ("rationale", "## Rationale — the 'because' behind decisions", "Reasons Nino has given for choices. Use these to explain trade-offs the way Nino would."),
        ("rejection", "## Alternatives — what Nino picks instead", "When Nino rejects an approach, these show what he reaches for instead."),
        ("declaration", "## Imperatives — first-move instructions", "Common opening rules Nino issues at the start of a task."),
    ]

    for stype, header, blurb in section_order:
        recs = by_signal.get(stype, [])
        if not recs:
            continue
        lines.append(header)
        lines.append("")
        lines.append(f"_{blurb}_")
        lines.append("")
        lines.append(f"**Signal count**: {len(recs)}")
        lines.append("")

        # Group by label, rank by recency-weighted score so old stale labels
        # sink below freshly-reinforced ones even at similar raw counts.
        label_scores: dict[str, tuple[float, int]] = {}
        for r in recs:
            cur = label_scores.get(r["label"], (0.0, 0))
            label_scores[r["label"]] = (cur[0] + r.get("weight", 0.5), cur[1] + 1)
        for label, (score, count) in sorted(
            label_scores.items(), key=lambda kv: kv[1][0], reverse=True
        ):
            label_recs = by_signal_label[(stype, label)]
            reps = dedupe_by_phrase(label_recs, limit=3)
            lines.append(f"### `{label}` ({count} occurrences, weight {score:.1f})")
            lines.append("")
            for r in reps:
                phrase = re.sub(r"\s+", " ", r["phrase"]).strip()
                if len(phrase) > 200:
                    phrase = phrase[:200].rsplit(" ", 1)[0] + "…"
                proj = r.get("project", "?")
                lines.append(f"- \"{phrase}\" — _{proj}_")
            lines.append("")

    # === Approvals: redirect-go gets its own section with prior_assistant context ===
    # This is the highest-signal pattern in the corpus: which question shapes from
    # Claude routinely get overridden with a single "go"/"continue". The phrase
    # alone is meaningless ("go"); the load-bearing data is what Claude asked.
    redirect_recs = by_signal_label.get(("approval", "redirect-go"), [])
    other_approvals = [
        r for r in by_signal.get("approval", []) if r["label"] != "redirect-go"
    ]

    if redirect_recs:
        lines.append("## Hesitation overrides — questions Nino said 'go' to")
        lines.append("")
        lines.append(
            "_Each entry pairs a question Claude asked with Nino's one-word "
            "override. Use these to recognize the shape of questions Nino "
            "considers unnecessary — and skip asking them._"
        )
        lines.append("")
        lines.append(f"**Signal count**: {len(redirect_recs)}")
        lines.append("")

        # Dedupe by prior_assistant tail (the actual question shape).
        seen_q: set[str] = set()
        shown = 0
        for r in redirect_recs:
            prior = re.sub(r"\s+", " ", (r.get("prior_assistant") or "")).strip()
            if not prior:
                continue
            # Use the last sentence of prior_assistant — usually the actual question.
            tail = prior[-180:]
            key = re.sub(r"\W+", " ", tail.lower())[:160]
            if key in seen_q:
                continue
            seen_q.add(key)
            phrase = re.sub(r"\s+", " ", r["phrase"]).strip()
            proj = r.get("project", "?")
            lines.append(f"- Claude asked: \"…{tail}\"")
            lines.append(f"  Nino replied: **\"{phrase}\"** — _{proj}_")
            lines.append("")
            shown += 1
            if shown >= 6:
                break
        lines.append("")

    if other_approvals:
        lines.append("## Validated judgment calls")
        lines.append("")
        lines.append("_Non-obvious approaches Nino confirmed worked. Don't re-litigate these._")
        lines.append("")
        lines.append(f"**Signal count**: {len(other_approvals)}")
        lines.append("")
        label_counts = Counter(r["label"] for r in other_approvals)
        for label, count in label_counts.most_common():
            label_recs = [r for r in other_approvals if r["label"] == label]
            reps = dedupe_by_phrase(label_recs, limit=3)
            lines.append(f"### `{label}` ({count} occurrences)")
            lines.append("")
            for r in reps:
                phrase = re.sub(r"\s+", " ", r["phrase"]).strip()
                if len(phrase) > 200:
                    phrase = phrase[:200].rsplit(" ", 1)[0] + "…"
                proj = r.get("project", "?")
                lines.append(f"- \"{phrase}\" — _{proj}_")
            lines.append("")

    # Project breakdown
    lines.append("## Project footprint")
    lines.append("")
    lines.append("Where these signals came from (top 20):")
    lines.append("")
    for proj, count in projects.most_common(20):
        lines.append(f"- `{proj}` — {count}")
    lines.append("")

    STACK_PATH.write_text("\n".join(lines))
    print(f"Stack written: {STACK_PATH} ({total} signals)", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="scan sessions and write corpus.jsonl (or DB for --session)")
    e.add_argument("--limit", type=int, default=None, help="max number of session files")
    e.add_argument("--since", type=int, default=None, help="only sessions newer than N days")
    e.add_argument("--session", type=str, default=None, help="single JSONL file -> DB (hook mode)")

    sub.add_parser("init", help="ensure DB schema exists")
    sub.add_parser("publish", help="load corpus.jsonl -> recall.db")
    sub.add_parser("assemble", help="build stack.md from DB (or corpus.jsonl)")

    c = sub.add_parser("catchup", help="watermark-driven idempotent sweep of all sessions")
    c.add_argument("--verbose", action="store_true", help="log even when nothing changed")

    d = sub.add_parser("drain-queue", help="drain ~/.claude/poe/queue then catchup-sweep")
    d.add_argument("--verbose", action="store_true", help="log even when nothing changed")

    sub.add_parser("enqueue", help="hook command: read stdin JSON, append transcript_path to queue")
    sub.add_parser("prompt-hook", help="UserPromptSubmit hook: emit Poe context for the prompt")

    hs = sub.add_parser("hook-stats", help="summarize prompt-hook telemetry")
    hs.add_argument("--days", type=int, default=7)

    df = sub.add_parser("drift", help="report stale labels (no reinforcement in 90+ days)")
    df.add_argument("--verbose", action="store_true", help="also show active labels")

    ml = sub.add_parser("memory-link", help="cross-reference voice corpus with MEMORY.md indexes")
    ml.add_argument("--threshold", type=int, default=5, help="min signal count for promotion candidate")
    ml.add_argument("--verbose", action="store_true")

    pc = sub.add_parser("poe-check", help="classify a draft against Poe situations")
    pc.add_argument("--text", type=str, default=None, help="draft text (default: stdin)")
    pc.add_argument("--limit", type=int, default=6, help="max grounding signals")

    q = sub.add_parser("query", help="FTS5 search Poe -> markdown block")
    q.add_argument("terms", nargs="+", help="search terms")
    q.add_argument("--limit", type=int, default=25, help="max results")
    q.add_argument("--project", type=str, default=None,
                   help="filter by project (label substring) or 'auto' for cwd-derived")

    sub.add_parser("run", help="extract + publish + assemble")

    args = p.parse_args()

    if args.cmd == "extract":
        cmd_extract(args.limit, args.since, args.session)
    elif args.cmd == "init":
        cmd_init()
    elif args.cmd == "publish":
        cmd_publish()
    elif args.cmd == "assemble":
        cmd_assemble()
    elif args.cmd == "catchup":
        cmd_catchup(verbose=args.verbose)
    elif args.cmd == "drain-queue":
        cmd_drain_queue(verbose=args.verbose)
    elif args.cmd == "enqueue":
        cmd_enqueue()
    elif args.cmd == "prompt-hook":
        cmd_prompt_hook()
    elif args.cmd == "hook-stats":
        cmd_hook_stats(days=args.days)
    elif args.cmd == "drift":
        cmd_drift(verbose=args.verbose)
    elif args.cmd == "memory-link":
        cmd_memory_link(promote_threshold=args.threshold, verbose=args.verbose)
    elif args.cmd == "poe-check":
        cmd_poe_check(args.text, args.limit)
    elif args.cmd == "query":
        cmd_query(args.terms, args.limit, project=args.project)
    elif args.cmd == "run":
        cmd_extract(None, None, None)
        cmd_publish()
        cmd_assemble()


if __name__ == "__main__":
    main()
