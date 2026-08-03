import sqlite3
from pathlib import Path


SCHEMA = Path(__file__).parent / "cloudflare" / "migrations" / "0001_init.sql"


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
        "{}",
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
    conn.close()
