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
# Four tokens: MIN_TOKEN_OVERLAP is 4, so a 3-token fixture could never match.
TOKENS = ["merge", "session", "pivot", "worktree"]


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
        # Must share MIN_TOKEN_OVERLAP tokens with TOKENS to clear the relevance
        # bar — this test guards the recency window, not the threshold.
        phrase="we should merge that session into a worktree instead of a pivot",
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


# --- relevance bar (2026-07-24) -------------------------------------------
# The hook's contract is "silence is the default", but measured emission was
# 99.2% over 1244 invocations. Cause: FTS matched against `message` (the whole
# user turn, incl. pasted content) with OR across tokens, so one common word
# surfaced a prior. These guard the tightened bar.

def test_weak_topical_match_excluded():
    """A prior sharing only one token with the prompt is not a prior."""
    conn = _mkconn()
    _insert(
        conn,
        session_id="old-session",
        ts=_iso(NOW - timedelta(days=10)),
        phrase="rotate the hero images on the gallery site every session",
    )
    out = poe._relevant_signals(conn, TOKENS, FLOOR, current_session="me")
    assert out == [], f"single-token match must not surface, got {out}"


def test_near_duplicate_of_prompt_excluded():
    """Restating what Nino just typed reads as insight but carries no information."""
    conn = _mkconn()
    phrase = "merge the session pivot work before the next branch cut"
    _insert(
        conn,
        session_id="old-session",
        ts=_iso(NOW - timedelta(days=10)),
        phrase=phrase,
    )
    out = poe._relevant_signals(
        conn, TOKENS, FLOOR, current_session="me", prompt=phrase
    )
    assert out == [], f"near-duplicate of the prompt must not surface, got {out}"


def test_pasted_image_reference_excluded():
    """[Image #N] rows are transcript artifacts, never voice."""
    conn = _mkconn()
    _insert(
        conn,
        session_id="old-session",
        ts=_iso(NOW - timedelta(days=10)),
        phrase="[Image #3] merge the session pivot into this layout",
    )
    out = poe._relevant_signals(conn, TOKENS, FLOOR, current_session="me")
    assert out == [], f"image-reference row must not surface, got {out}"


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
