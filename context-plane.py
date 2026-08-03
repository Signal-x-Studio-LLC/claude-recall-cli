#!/usr/bin/env python3
"""Stage and deliver distilled recall records to the private context plane.

Raw transcripts never leave the machine. This client reads already-redacted
records from recall.db, writes them to a durable local outbox, and delivers
idempotent batches when a network connection is available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


RECALL_DB = Path(os.environ.get("RECALL_DB", Path.home() / ".claude" / "recall.db"))
DEFAULT_TOKEN_ENV = "RECALL_CONTEXT_TOKEN"
DEFAULT_URL_ENV = "RECALL_CONTEXT_URL"
STAGED_SIGNAL_TYPES = ("correction", "preference", "rationale", "declaration")
SECRET_PATTERNS = re.compile(
    r"""(
        sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}
      | sk-proj-[A-Za-z0-9_\-]{20,}
      | sk-or-v1-[A-Za-z0-9_\-]{20,}
      | sk-[A-Za-z0-9]{32,}
      | cfk_[A-Za-z0-9_\-]{24,}
      | sbp_[A-Za-z0-9]{20,}
      | sb(?:p|s)_[A-Za-z0-9_\-]{20,}
      | gh[pousr]_[A-Za-z0-9]{20,}
      | AKIA[0-9A-Z]{16}
      | xox[baprs]-[A-Za-z0-9\-]{10,}
      | ey[A-Za-z0-9_\-]{10,}\.ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}
    )""",
    re.VERBOSE,
)
REDACTION = "[redacted-secret]"


OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_outbox (
    event_id       TEXT PRIMARY KEY,
    source_table   TEXT NOT NULL,
    source_row_id  TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending'
                   CHECK(state IN ('pending', 'sent', 'acknowledged')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at        TEXT,
    UNIQUE(source_table, source_row_id)
);

CREATE INDEX IF NOT EXISTS idx_cloud_outbox_state_created
ON cloud_outbox(state, created_at);
"""


def connect() -> sqlite3.Connection:
    RECALL_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(RECALL_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript(OUTBOX_SCHEMA)
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def machine_id() -> str:
    configured = os.environ.get("RECALL_MACHINE_ID", "").strip()
    if configured:
        return configured
    return platform.node() or "unknown-machine"


def stable_event_id(source_table: str, source_row_id: str) -> str:
    digest = hashlib.sha256(f"{source_table}:{source_row_id}".encode()).hexdigest()
    return f"evt_{digest[:32]}"


def redact_secrets(text: str | None) -> str:
    """Remove credential-shaped strings again at the cloud boundary."""
    return SECRET_PATTERNS.sub(REDACTION, text or "")


def compact_title(text: str, fallback: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return fallback
    sentence = normalized.split(". ", 1)[0]
    return sentence[:120]


def stage_signal(row: sqlite3.Row) -> dict:
    source_row_id = str(row["id"])
    phrase = redact_secrets(row["phrase"] or row["message"]).strip()
    signal_type = str(row["signal_type"])
    return {
        "event_id": stable_event_id("voice_signals", source_row_id),
        "schema_version": 1,
        "event_type": "memory_candidate",
        "occurred_at": row["timestamp"] or utc_now(),
        "memory": {
            "stable_key": f"voice_signals:{source_row_id}",
            "kind": signal_type,
            "title": compact_title(phrase, signal_type.replace("_", " ").title()),
            "body": phrase[:2000],
            "project": row["project"],
            "status": "Candidate",
            "confidence": 0.55,
        },
        "provenance": {
            "source_table": "voice_signals",
            "source_row_id": source_row_id,
            "source_client": row["source_client"] or "claude",
            "source_machine": machine_id(),
            "session_id": row["session_id"],
            "source_timestamp": row["timestamp"],
            "signal_label": row["label"],
            "evidence_excerpt": redact_secrets(row["message"] or phrase)[:1200],
        },
    }


def stage_recipe(row: sqlite3.Row) -> dict:
    source_row_id = str(row["id"])
    intent = redact_secrets(row["intent"])
    body_parts = [intent]
    if row["outcome"]:
        body_parts.append(f"Outcome: {redact_secrets(row['outcome'])}")
    if row["prompt_template"]:
        body_parts.append(f"Reusable prompt: {redact_secrets(row['prompt_template'])}")
    body = "\n\n".join(part.strip() for part in body_parts if part and part.strip())
    return {
        "event_id": stable_event_id("recipes", source_row_id),
        "schema_version": 1,
        "event_type": "memory_candidate",
        "occurred_at": row["created_at"] or utc_now(),
        "memory": {
            "stable_key": f"recipes:{source_row_id}",
            "kind": "recipe",
            "title": compact_title(intent, "Reusable procedure"),
            "body": body[:6000],
            "project": row["project_path"],
            "status": "Candidate",
            "confidence": 0.8,
        },
        "provenance": {
            "source_table": "recipes",
            "source_row_id": source_row_id,
            "source_client": row["source_client"] or "claude",
            "source_machine": machine_id(),
            "session_id": row["session_id"],
            "source_timestamp": row["created_at"],
            "curation_level": "manual_recipe",
        },
    }


def insert_outbox(conn: sqlite3.Connection, payload: dict) -> bool:
    provenance = payload["provenance"]
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO cloud_outbox
            (event_id, source_table, source_row_id, payload_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            payload["event_id"],
            provenance["source_table"],
            provenance["source_row_id"],
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


def cmd_stage(include_approvals: bool = False) -> int:
    conn = connect()
    signal_types = list(STAGED_SIGNAL_TYPES)
    if include_approvals:
        signal_types.append("approval")
    placeholders = ",".join("?" for _ in signal_types)
    staged = 0
    try:
        signal_rows = conn.execute(
            f"""
            SELECT id, session_id, source_client, project, timestamp,
                   signal_type, label, phrase, message
            FROM voice_signals
            WHERE signal_type IN ({placeholders})
            ORDER BY id
            """,
            signal_types,
        ).fetchall()
        for row in signal_rows:
            staged += int(insert_outbox(conn, stage_signal(row)))

        recipe_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(recipes)").fetchall()
        }
        source_expr = "source_client" if "source_client" in recipe_columns else "'claude'"
        recipe_rows = conn.execute(
            f"""
            SELECT id, session_id, {source_expr} AS source_client, project_path,
                   created_at, intent, outcome, prompt_template
            FROM recipes
            ORDER BY created_at
            """
        ).fetchall()
        for row in recipe_rows:
            staged += int(insert_outbox(conn, stage_recipe(row)))
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({"staged": staged, "database": str(RECALL_DB)}))
    return 0


def pending_batch(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT event_id, payload_json
        FROM cloud_outbox
        WHERE state = 'pending'
        ORDER BY created_at, event_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def resolve_push_settings(url: str | None, token_env: str) -> tuple[str, str]:
    base_url = (url or os.environ.get(DEFAULT_URL_ENV, "")).strip().rstrip("/")
    if not base_url:
        raise ValueError(f"context URL required via --url or {DEFAULT_URL_ENV}")
    token = os.environ.get(token_env, "").strip()
    if not token:
        raise ValueError(f"bearer token missing from {token_env}")
    return base_url, token


def deliver(url: str, token: str, events: list[dict], timeout: float) -> dict:
    request = urllib.request.Request(
        f"{url}/ingest",
        data=json.dumps({"events": events}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "claude-recall-context-plane/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        if response.status not in {200, 202}:
            raise RuntimeError(f"ingest returned HTTP {response.status}")
        return json.loads(body) if body else {}


def fetch_receipts(
    url: str, token: str, event_ids: list[str], timeout: float
) -> dict:
    request = urllib.request.Request(
        f"{url}/receipts",
        data=json.dumps({"ids": event_ids}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "claude-recall-context-plane/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        if response.status != 200:
            raise RuntimeError(f"receipts returned HTTP {response.status}")
        return json.loads(body) if body else {"receipts": []}


def cmd_push(url: str | None, token_env: str, limit: int, timeout: float) -> int:
    try:
        base_url, token = resolve_push_settings(url, token_env)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    conn = connect()
    rows = pending_batch(conn, limit)
    if not rows:
        conn.close()
        print(json.dumps({"sent": 0, "pending": 0}))
        return 0
    events = [json.loads(row["payload_json"]) for row in rows]
    event_ids = [row["event_id"] for row in rows]
    try:
        receipt = deliver(base_url, token, events, timeout)
    except (
        OSError,
        RuntimeError,
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ) as error:
        message = str(error)[:500]
        conn.executemany(
            """
            UPDATE cloud_outbox
            SET attempts = attempts + 1, last_error = ?
            WHERE event_id = ?
            """,
            [(message, event_id) for event_id in event_ids],
        )
        conn.commit()
        conn.close()
        print(json.dumps({"sent": 0, "pending": len(event_ids), "error": message}))
        return 1

    conn.executemany(
        """
        UPDATE cloud_outbox
        SET state = 'sent', attempts = attempts + 1,
            last_error = NULL, sent_at = ?
        WHERE event_id = ?
        """,
        [(utc_now(), event_id) for event_id in event_ids],
    )
    conn.commit()
    pending = conn.execute(
        "SELECT COUNT(*) FROM cloud_outbox WHERE state = 'pending'"
    ).fetchone()[0]
    conn.close()
    print(json.dumps({"sent": len(event_ids), "pending": pending, "receipt": receipt}))
    return 0


def cmd_verify(url: str | None, token_env: str, limit: int, timeout: float) -> int:
    try:
        base_url, token = resolve_push_settings(url, token_env)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    conn = connect()
    rows = conn.execute(
        """
        SELECT event_id FROM cloud_outbox
        WHERE state = 'sent'
        ORDER BY sent_at, event_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    event_ids = [row["event_id"] for row in rows]
    if not event_ids:
        conn.close()
        print(json.dumps({"acknowledged": 0, "waiting": 0, "retrying": 0}))
        return 0
    try:
        response = fetch_receipts(base_url, token, event_ids, timeout)
    except (
        OSError,
        RuntimeError,
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ) as error:
        conn.close()
        print(json.dumps({"acknowledged": 0, "waiting": len(event_ids), "error": str(error)[:500]}))
        return 1

    by_id = {
        receipt["event_id"]: receipt
        for receipt in response.get("receipts", [])
        if isinstance(receipt, dict) and receipt.get("event_id")
    }
    acknowledged = 0
    retrying = 0
    for event_id in event_ids:
        receipt = by_id.get(event_id)
        status = receipt.get("status") if receipt else None
        if status == "processed":
            conn.execute(
                "UPDATE cloud_outbox SET state = 'acknowledged', last_error = NULL WHERE event_id = ?",
                (event_id,),
            )
            acknowledged += 1
        elif status == "failed":
            conn.execute(
                "UPDATE cloud_outbox SET state = 'pending', last_error = ? WHERE event_id = ?",
                ((receipt.get("error") or "server processing failed")[:500], event_id),
            )
            retrying += 1
        elif receipt is None:
            conn.execute(
                """
                UPDATE cloud_outbox
                SET state = 'pending', last_error = 'receipt missing; retrying idempotently'
                WHERE event_id = ?
                """,
                (event_id,),
            )
            retrying += 1
    conn.commit()
    waiting = conn.execute(
        "SELECT COUNT(*) FROM cloud_outbox WHERE state = 'sent'"
    ).fetchone()[0]
    conn.close()
    print(json.dumps({"acknowledged": acknowledged, "waiting": waiting, "retrying": retrying}))
    return 0


def cmd_status() -> int:
    conn = connect()
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM cloud_outbox GROUP BY state ORDER BY state"
    ).fetchall()
    failures = conn.execute(
        "SELECT COUNT(*) FROM cloud_outbox WHERE state = 'pending' AND attempts > 0"
    ).fetchone()[0]
    conn.close()
    print(json.dumps({"outbox": {row["state"]: row["n"] for row in rows}, "retrying": failures}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="stage redacted signals and recipes in the local outbox")
    stage.add_argument(
        "--include-approvals",
        action="store_true",
        help="include approval phrases; omitted by default because they are high-volume",
    )

    push = sub.add_parser("push", help="deliver one retryable outbox batch")
    push.add_argument("--url", help=f"Worker base URL (default: ${DEFAULT_URL_ENV})")
    push.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    push.add_argument("--limit", type=int, default=50)
    push.add_argument("--timeout", type=float, default=15.0)

    verify = sub.add_parser("verify", help="confirm that queued events were processed")
    verify.add_argument("--url", help=f"Worker base URL (default: ${DEFAULT_URL_ENV})")
    verify.add_argument("--token-env", default=DEFAULT_TOKEN_ENV)
    verify.add_argument("--limit", type=int, default=100)
    verify.add_argument("--timeout", type=float, default=15.0)

    sub.add_parser("status", help="show pending and delivered outbox counts")

    args = parser.parse_args()
    if args.command == "stage":
        return cmd_stage(include_approvals=args.include_approvals)
    if args.command == "push":
        return cmd_push(args.url, args.token_env, max(1, min(args.limit, 100)), args.timeout)
    if args.command == "verify":
        return cmd_verify(args.url, args.token_env, max(1, min(args.limit, 100)), args.timeout)
    if args.command == "status":
        return cmd_status()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
