"""Regression tests for Claude, Codex, and Gemini transcript normalization."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).parent / "poe-extract.py"
spec = importlib.util.spec_from_file_location("poe_extract_cross_client", SCRIPT)
poe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poe)


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_codex_uses_event_messages_and_ignores_injected_user_context(tmp_path):
    session_id = "019fc4a9-f447-70e2-9ab6-746e66cfe97f"
    transcript = _write_jsonl(
        tmp_path / f"rollout-2026-08-02T17-48-20-{session_id}.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-08-02T17:48:20Z",
                "payload": {
                    "id": session_id,
                    "cwd": "/Users/nino/Workspace/dev/tools/claude-recall-cli",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-02T17:48:21Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>machine supplied</environment_context>",
                        }
                    ],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T17:48:22Z",
                "payload": {
                    "type": "user_message",
                    "message": "I prefer editing the existing parser because duplicate miners drift.",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T17:48:23Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "I am checking the current parser before changing it.",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T17:48:24Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": (
                        "The parser now normalizes both transcript formats while preserving "
                        "source provenance and excluding injected context from user evidence."
                    ),
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-02T17:48:25Z",
                "payload": {"type": "user_message", "message": "go"},
            },
        ],
    )

    assert poe.transcript_source(transcript) == "codex"
    assert poe.session_id_for(transcript) == session_id
    assert poe.project_label_for(transcript) == "tools/claude-recall-cli"

    messages = list(poe.iter_user_messages(transcript))
    assert [message for _ts, message, _prior in messages] == [
        "I prefer editing the existing parser because duplicate miners drift.",
        "go",
    ]
    assert "environment_context" not in " ".join(message for _ts, message, _ in messages)

    pairs = list(poe.iter_pairs(transcript))
    assert len(pairs) == 1
    assert pairs[0][1] == "go"
    assert "normalizes both transcript formats" in pairs[0][0]

    signals = poe._extract_from_file(transcript)
    assert signals
    assert {record["source_client"] for record in signals} == {"codex"}
    assert {record["session_id"] for record in signals} == {session_id}

    validated = poe._extract_validated_from_file(transcript)
    assert len(validated) == 1
    assert validated[0]["source_client"] == "codex"


def test_claude_transcript_behavior_is_preserved(tmp_path):
    transcript = _write_jsonl(
        tmp_path / "claude-session.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-08-01T10:00:00Z",
                "cwd": "/Users/nino/Workspace/dev/apps/example",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "The implementation is complete and verified against the "
                                "repository tests, with the remaining human gate documented."
                            ),
                        }
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "2026-08-01T10:01:00Z",
                "message": {"content": "go"},
            },
        ],
    )

    assert poe.transcript_source(transcript) == "claude"
    assert poe.project_label_for(transcript) == "apps/example"
    assert len(list(poe.iter_pairs(transcript))) == 1
    signals = poe._extract_from_file(transcript)
    assert len(signals) == 1
    assert signals[0]["source_client"] == "claude"


def test_gemini_json_transcript_is_normalized(tmp_path):
    session_id = "a3b47b2c-7dde-41bf-b101-8c67262cad62"
    project_dir = tmp_path / "project-hash"
    chats_dir = project_dir / "chats"
    chats_dir.mkdir(parents=True)
    (project_dir / ".project_root").write_text(
        "/Users/nino/Workspace/dev/apps/gemini-example\n"
    )
    transcript = chats_dir / "session-2026-08-02T20-00-a3b47b2c.json"
    transcript.write_text(
        json.dumps(
            {
                "sessionId": session_id,
                "projectHash": "project-hash",
                "messages": [
                    {
                        "id": "u1",
                        "timestamp": "2026-08-02T20:00:00Z",
                        "type": "user",
                        "content": "I prefer one shared instruction source across every harness.",
                    },
                    {
                        "id": "g1",
                        "timestamp": "2026-08-02T20:00:01Z",
                        "type": "gemini",
                        "content": (
                            "The shared source is wired through a thin Gemini adapter and "
                            "the transcript remains local until redacted records are staged."
                        ),
                    },
                    {
                        "id": "u2",
                        "timestamp": "2026-08-02T20:00:02Z",
                        "type": "user",
                        "content": "go",
                    },
                ],
            }
        )
    )

    assert poe.transcript_source(transcript) == "gemini"
    assert poe.session_id_for(transcript) == session_id
    assert poe.project_label_for(transcript) == "apps/gemini-example"
    pairs = list(poe.iter_pairs(transcript))
    assert len(pairs) == 1
    assert "shared source is wired" in pairs[0][0]
    signals = poe._extract_from_file(transcript)
    assert signals
    assert {record["source_client"] for record in signals} == {"gemini"}


def test_codex_queue_entry_resolves_after_archive_move(tmp_path):
    session_id = "019fc4a9-f447-70e2-9ab6-746e66cfe97f"
    sessions = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    sessions.mkdir()
    archived.mkdir()
    moved = archived / f"rollout-2026-08-02T17-48-20-{session_id}.jsonl"
    moved.write_text("{}\n")

    old_sessions = poe.CODEX_SESSIONS_DIR
    old_archived = poe.CODEX_ARCHIVED_SESSIONS_DIR
    poe.CODEX_SESSIONS_DIR = sessions
    poe.CODEX_ARCHIVED_SESSIONS_DIR = archived
    try:
        resolved = poe._resolve_queued_transcript(
            {
                "source_client": "codex",
                "session_id": session_id,
                "transcript_path": str(sessions / "missing.jsonl"),
            }
        )
    finally:
        poe.CODEX_SESSIONS_DIR = old_sessions
        poe.CODEX_ARCHIVED_SESSIONS_DIR = old_archived

    assert resolved == moved


def _retention_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE ingest_watermark (
            session_path TEXT PRIMARY KEY,
            source_client TEXT NOT NULL,
            last_mtime_ns INTEGER NOT NULL,
            last_ingested TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def test_retention_report_only_covers_unchanged_archived_files_after_grace(tmp_path):
    archive = tmp_path / "archived_sessions"
    archive.mkdir()
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    covered = archive / "covered.jsonl"
    changed = archive / "changed.jsonl"
    recent = archive / "recent.jsonl"
    uncovered = archive / "uncovered.jsonl"
    for path in (covered, changed, recent, uncovered):
        path.write_text("{}\n")

    old_ns = int((now - timedelta(days=10)).timestamp() * 1_000_000_000)
    recent_ns = int((now - timedelta(days=1)).timestamp() * 1_000_000_000)
    os.utime(covered, ns=(old_ns, old_ns))
    os.utime(changed, ns=(old_ns, old_ns))
    os.utime(uncovered, ns=(old_ns, old_ns))
    os.utime(recent, ns=(recent_ns, recent_ns))

    conn = _retention_conn()
    conn.execute(
        "INSERT INTO ingest_watermark VALUES (?, 'codex', ?, datetime('now'))",
        (str(covered), covered.stat().st_mtime_ns),
    )
    conn.execute(
        "INSERT INTO ingest_watermark VALUES (?, 'codex', ?, datetime('now'))",
        (str(changed), changed.stat().st_mtime_ns - 1),
    )
    conn.execute(
        "INSERT INTO ingest_watermark VALUES (?, 'codex', ?, datetime('now'))",
        (str(recent), recent.stat().st_mtime_ns),
    )

    report = poe.build_retention_report(
        conn, archive, grace_days=7, now=now, list_covered=True
    )

    assert report["scanned_files"] == 4
    assert report["watermark_covered"]["files"] == 1
    assert report["changed_after_ingest"]["files"] == 1
    assert report["too_recent"]["files"] == 1
    assert report["uncovered"]["files"] == 1
    assert report["errors"]["files"] == 0
    assert report["deletion_authorized"] is False
    assert [item["path"] for item in report["covered_files"]] == [str(covered)]


def test_retention_report_fails_closed_for_missing_archive_or_bad_grace(tmp_path):
    conn = _retention_conn()

    try:
        poe.build_retention_report(conn, tmp_path / "missing")
        assert False, "missing archive directory should fail"
    except FileNotFoundError:
        pass

    archive = tmp_path / "archived_sessions"
    archive.mkdir()
    try:
        poe.build_retention_report(conn, archive, grace_days=-1)
        assert False, "negative grace should fail"
    except ValueError:
        pass
