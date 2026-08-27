#!/usr/bin/env python3
"""SessionStart nudge for the recall context plane.

Two things are invisible without this hook. Memory candidates sit unreviewed,
and `search_memory` only returns Approved records, so an unreviewed candidate
reaches no agent. And the scheduled sync can stall without emitting an error,
which is how it stayed dead for three days.

Those two failures look identical from a candidate count alone: a stalled sync
and an empty queue both report zero. So liveness is reported first and
separately, and a stale heartbeat is never presented as a clear queue.

Always exits 0. A broken nudge must never block a session.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_PATH = Path(
    os.environ.get(
        "RECALL_CONTEXT_HEARTBEAT",
        Path.home() / ".claude" / "recall-context-heartbeat.json",
    )
)
RECALL_DB = Path(os.environ.get("RECALL_DB", Path.home() / ".claude" / "recall.db"))
NOMINATIONS_PATH = Path(
    os.environ.get(
        "RECALL_NOMINATIONS",
        Path.home() / ".claude" / "recall-nominations.json",
    )
)

# The sync runs every 15 minutes. This is set well above that so an overnight
# sleep or a lunch break stays quiet; a genuine stall still surfaces same-day.
STALE_HOURS = 6


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def describe_age(delta_hours: float) -> str:
    if delta_hours < 48:
        return f"{delta_hours:.0f}h ago"
    return f"{delta_hours / 24:.0f}d ago"


def nomination_line() -> str | None:
    """Sessions worth saving as recipes, scored by the scheduled scan.

    Separate file from the heartbeat on purpose: the heartbeat is written by
    the 15-minute sync and this is written by the scan, so sharing one file
    would race. One reader, two queues, at most one line each.
    """
    if not NOMINATIONS_PATH.exists():
        return None
    try:
        payload = json.loads(NOMINATIONS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    count = len(payload.get("nominations") or [])
    if not count:
        return None
    top = payload["nominations"][0].get("intent") or ""
    top = top.strip().replace("\n", " ")[:60]
    return (
        f"recall: {count} session{'s' if count != 1 else ''} worth saving as a "
        f"recipe — run /recall nominations. Top: {top!r}"
    )


def emit(lines: list[str], nomination: str | None) -> int:
    for line in lines:
        print(line)
    if nomination:
        print(nomination)
    return 0


def main() -> int:
    nomination = nomination_line()

    if not HEARTBEAT_PATH.exists():
        # Only speak up if the system is actually in use on this machine.
        head = (
            [
                "recall context-plane: no heartbeat yet — the scheduled sync may "
                "never have completed on this machine."
            ]
            if RECALL_DB.exists()
            else []
        )
        return emit(head, nomination)

    try:
        heartbeat = json.loads(HEARTBEAT_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return emit(
            [f"recall context-plane: heartbeat at {HEARTBEAT_PATH} is unreadable."],
            nomination,
        )

    lines: list[str] = []
    last_success = parse_timestamp(heartbeat.get("last_success"))
    age_hours = (
        (datetime.now(timezone.utc) - last_success).total_seconds() / 3600
        if last_success
        else None
    )
    stale = age_hours is None or age_hours > STALE_HOURS

    candidates = heartbeat.get("candidates")

    if stale:
        when = "never" if age_hours is None else describe_age(age_hours)
        error = heartbeat.get("error")
        detail = f" Last error: {error}." if error else ""
        caveat = " The count below is from that last success." if candidates else ""
        lines.append(
            f"recall context-plane: sync last succeeded {when}.{detail}{caveat}"
        )

    if candidates:
        lines.append(
            f"recall context-plane: {candidates} memory candidate"
            f"{'s' if candidates != 1 else ''} pending review — run /recall review."
        )

    return emit(lines, nomination)


if __name__ == "__main__":
    raise SystemExit(main())
