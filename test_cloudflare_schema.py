import sqlite3
from pathlib import Path


MIGRATIONS = Path(__file__).parent / "cloudflare" / "migrations"
SCHEMA = MIGRATIONS / "0001_init.sql"
PRIVACY_MIGRATION = MIGRATIONS / "0002_remove_raw_provenance.sql"
LOCAL_ONLY_MIGRATION = MIGRATIONS / "0003_keep_voice_signals_local.sql"


def test_cloudflare_schema_enforces_state_and_fts_contracts():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text())
    values = (
        "mem_0123456789abcdef0123456789abcdef",
        "voice_signals:1",
        "correction",
        "Use the canonical adapter",
        "Use the canonical adapter instead of another wrapper.",
        "tools/recall",
        "codex",
        "test-mac",
        "s1",
        "2026-08-02T20:00:00Z",
        '{"source_table":"voice_signals","evidence_excerpt":"verbatim prompt"}',
    )
    conn.execute(
        """
        INSERT INTO memories
          (id, stable_key, kind, title, body, project, source_client,
           source_machine, source_session_id, source_timestamp, provenance_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    hit = conn.execute(
        """
        SELECT m.id FROM memories_fts
        JOIN memories m ON m.rowid = memories_fts.rowid
        WHERE memories_fts MATCH 'canonical'
        """
    ).fetchone()
    assert hit == (values[0],)

    try:
        conn.execute(
            "UPDATE memories SET status = 'MachineApproved' WHERE id = ?",
            (values[0],),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("invalid state bypassed the D1 CHECK constraint")

    try:
        conn.execute(
            """
            INSERT INTO memories
              (id, stable_key, kind, title, body, source_client,
               source_machine, provenance_json)
            VALUES (?, ?, 'recipe', 'Duplicate', 'Duplicate', 'claude', 'mac', '{}')
            """,
            ("mem_abcdef0123456789abcdef0123456789", "voice_signals:1"),
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate stable key bypassed idempotency constraint")

    conn.executescript(PRIVACY_MIGRATION.read_text())
    provenance, event_type, payload = conn.execute(
        """
        SELECT m.provenance_json, e.event_type, e.payload_json
        FROM memories m
        JOIN memory_events e ON e.memory_id = m.id
        WHERE m.id = ? AND e.event_type = 'provenance_redacted'
        """,
        (values[0],),
    ).fetchone()
    assert "evidence_excerpt" not in provenance
    assert event_type == "provenance_redacted"
    assert '"removed_excerpt_chars":15' in payload

    # The migration is safe to replay locally while testing recovery.
    conn.executescript(PRIVACY_MIGRATION.read_text())
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE event_type = 'provenance_redacted'"
    ).fetchone() == (1,)

    conn.executescript(LOCAL_ONLY_MIGRATION.read_text())
    title, body, project, status, confidence = conn.execute(
        "SELECT title, body, project, status, confidence FROM memories WHERE id = ?",
        (values[0],),
    ).fetchone()
    assert title == "Local-only voice signal removed"
    assert "stays local" in body
    assert "canonical adapter" not in body
    assert project is None
    assert status == "Stale"
    assert confidence == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE event_type = 'content_redacted'"
    ).fetchone() == (1,)
    assert conn.execute(
        "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'canonical'"
    ).fetchone() == (0,)

    conn.executescript(LOCAL_ONLY_MIGRATION.read_text())
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_events WHERE event_type = 'content_redacted'"
    ).fetchone() == (1,)
    conn.close()
