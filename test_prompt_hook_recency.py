"""Recency-floor + self-echo guards for the per-prompt Poe hook.

Regression test for the 2026-05-29 cross-session leak: an instruction typed into
a concurrent, still-running session surfaced in ANOTHER session's prompt-hook as
a "prior" (because the recall DB is machine-global and ingests every session
continuously). The fix is `_relevant_signals`: a recency floor (settled history
only) plus current-session exclusion.

Run: /usr/bin/python3 -m pytest test_prompt_hook_recency.py -v
Or:  /usr/bin/python3 test_prompt_hook_recency.py
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parent / "poe-extract.py"
spec = importlib.util.spec_from_file_location("poe_extract", SCRIPT)
poe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poe)

NOW = datetime.now(timezone.utc)
FLOOR = (NOW - timedelta(hours=poe.PRIOR_RECENCY_FLOOR_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
TOKENS = ["merge", "session", "pivot"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _mkconn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(poe.SCHEMA_SQL)
    return conn


def _insert(conn, *, session_id, ts, phrase, signal_type="correction", label="opening-negative"):
    conn.execute(
        "INSERT INTO voice_signals "
        "(session_id, project, timestamp, signal_type, label, phrase, phrase_hash) "
        "VALUES (?,?,?,?,?,?,?)",
        (session_id, "apps/rally-hq", ts, signal_type, label, phrase, phrase[:24]),
    )
    conn.commit()


def test_recent_concurrent_signal_excluded():
    """The exact bug: a fresh instruction from another live session must NOT surface."""
    conn = _mkconn()
    _insert(
        conn,
        session_id="other-live-session",
        ts=_iso(NOW - timedelta(minutes=31)),
        phrase="hold on the merge. another session will handle the pivot",
    )
    out = poe._relevant_signals(conn, TOKENS, FLOOR, current_session="me")
    assert out == [], f"fresh concurrent-session signal must not surface, got {out}"


def test_settled_signal_surfaces():
    """A genuinely old prior is still surfaced — the fix must not mute history."""
    conn = _mkconn()
    _insert(
        conn,
        session_id="old-session",
        ts=_iso(NOW - timedelta(days=10)),
        phrase="we should merge with a branch or worktree instead of a separate session",
    )
    out = poe._relevant_signals(conn, TOKENS, FLOOR, current_session="me")
    assert len(out) == 1, f"settled prior should surface, got {out}"


def test_self_echo_excluded():
    """A session must not be fed its own messages back as 'priors'."""
    conn = _mkconn()
    _insert(
        conn,
        session_id="me",
        ts=_iso(NOW - timedelta(days=10)),
        phrase="merge the session pivot work into the main branch",
    )
    out = poe._relevant_signals(conn, TOKENS, FLOOR, current_session="me")
    assert out == [], f"current session's own signal must not echo back, got {out}"


def test_signal_inside_floor_excluded():
    """Anything inside the recency window (even from another session) is excluded."""
    conn = _mkconn()
    _insert(
        conn,
        session_id="other",
        ts=_iso(NOW - timedelta(hours=poe.PRIOR_RECENCY_FLOOR_HOURS - 1)),
        phrase="merge the session pivot now",
    )
    out = poe._relevant_signals(conn, TOKENS, FLOOR, current_session="me")
    assert out == [], "a signal inside the recency floor must be excluded"


def test_empty_tokens_returns_nothing():
    conn = _mkconn()
    _insert(conn, session_id="old", ts=_iso(NOW - timedelta(days=10)), phrase="merge session pivot")
    assert poe._relevant_signals(conn, [], FLOOR, current_session="me") == []


if __name__ == "__main__":
    import sys

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
