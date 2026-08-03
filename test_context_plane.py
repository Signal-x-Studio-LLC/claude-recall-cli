import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("context-plane.py")
SPEC = importlib.util.spec_from_file_location("context_plane", MODULE_PATH)
context_plane = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(context_plane)


class ContextPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "recall.db"
        context_plane.RECALL_DB = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE voice_signals (
              id INTEGER PRIMARY KEY, session_id TEXT, source_client TEXT,
              project TEXT, timestamp TEXT, signal_type TEXT, label TEXT,
              phrase TEXT, message TEXT
            );
            CREATE TABLE recipes (
              id TEXT PRIMARY KEY, session_id TEXT, source_client TEXT,
              project_path TEXT, created_at TEXT, intent TEXT,
              outcome TEXT, prompt_template TEXT
            );
            INSERT INTO voice_signals VALUES
              (1, 's1', 'gemini', 'demo', '2026-08-02T10:00:00Z',
               'correction', 'instead', 'Use the canonical adapter.',
               'Use the canonical adapter instead of another wrapper.'),
              (2, 's1', 'gemini', 'demo', '2026-08-02T10:01:00Z',
               'approval', 'yes', 'yes', 'yes');
            INSERT INTO recipes VALUES
              ('r1', 's1', 'codex', '/demo', '2026-08-02T11:00:00Z',
               'Recover an interrupted upload', 'Resume succeeded',
               'Run resume with sk-proj-12345678901234567890123456789012.');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_stage_is_idempotent_and_omits_approvals(self):
        with patch.dict(os.environ, {"RECALL_MACHINE_ID": "test-mac"}):
            self.assertEqual(context_plane.cmd_stage(backfill=True), 0)
            self.assertEqual(context_plane.cmd_stage(backfill=True), 0)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT payload_json FROM cloud_outbox ORDER BY source_table"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        payloads = [json.loads(row[0]) for row in rows]
        self.assertEqual({p["memory"]["kind"] for p in payloads}, {"correction", "recipe"})
        self.assertTrue(all(p["memory"]["status"] == "Candidate" for p in payloads))
        self.assertTrue(all(p["provenance"]["source_machine"] == "test-mac" for p in payloads))
        serialized = json.dumps(payloads)
        self.assertNotIn("sk-proj-12345678901234567890123456789012", serialized)
        self.assertIn("[redacted-secret]", serialized)
        self.assertNotIn("evidence_excerpt", serialized)
        self.assertNotIn("instead of another wrapper", serialized)

    def test_baseline_skips_history_and_stages_future_rows(self):
        self.assertEqual(context_plane.cmd_baseline(), 0)
        self.assertEqual(context_plane.cmd_stage(), 0)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM cloud_outbox").fetchone()[0], 0
        )
        conn.execute(
            """
            INSERT INTO voice_signals VALUES
              (3, 's2', 'claude', 'demo', '2026-08-03T12:00:00Z',
               'preference', 'prefer', 'Prefer the smaller change.',
               'Prefer the smaller change when both satisfy the contract.')
            """
        )
        conn.commit()
        conn.close()
        self.assertEqual(context_plane.cmd_stage(), 0)
        conn = sqlite3.connect(self.db_path)
        payloads = conn.execute("SELECT payload_json FROM cloud_outbox").fetchall()
        conn.close()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(json.loads(payloads[0][0])["provenance"]["source_row_id"], "3")

    def test_failed_push_stays_pending_then_recovers(self):
        context_plane.cmd_stage(backfill=True)
        with patch.dict(os.environ, {"TEST_CONTEXT_TOKEN": "secret"}), patch.object(
            context_plane, "deliver", side_effect=OSError("offline")
        ):
            result = context_plane.cmd_push(
                "https://context.example.test", "TEST_CONTEXT_TOKEN", 50, 0.1
            )
        self.assertEqual(result, 1)

        delivered = []

        def capture(_url, _token, events, _timeout):
            delivered.extend(events)
            return {"accepted": len(events)}

        with patch.dict(os.environ, {"TEST_CONTEXT_TOKEN": "secret"}), patch.object(
            context_plane, "deliver", side_effect=capture
        ):
            result = context_plane.cmd_push(
                "https://context.example.test", "TEST_CONTEXT_TOKEN", 50, 2
            )
        self.assertEqual(result, 0)
        self.assertEqual(len(delivered), 2)
        conn = sqlite3.connect(self.db_path)
        state, attempts = conn.execute(
            "SELECT state, MIN(attempts) FROM cloud_outbox"
        ).fetchone()
        conn.close()
        self.assertEqual(state, "sent")
        self.assertEqual(attempts, 2)

        with patch.dict(os.environ, {"TEST_CONTEXT_TOKEN": "secret"}), patch.object(
            context_plane,
            "fetch_receipts",
            return_value={
                "receipts": [
                    {"event_id": event["event_id"], "status": "processed"}
                    for event in delivered
                ]
            },
        ):
            result = context_plane.cmd_verify(
                "https://context.example.test", "TEST_CONTEXT_TOKEN", 100, 2
            )
        self.assertEqual(result, 0)
        conn = sqlite3.connect(self.db_path)
        states = conn.execute(
            "SELECT DISTINCT state FROM cloud_outbox"
        ).fetchall()
        conn.close()
        self.assertEqual(states, [("acknowledged",)])

    def test_missing_receipt_returns_event_to_pending(self):
        context_plane.cmd_stage(backfill=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE cloud_outbox SET state = 'sent'")
        conn.commit()
        conn.close()
        with patch.dict(os.environ, {"TEST_CONTEXT_TOKEN": "secret"}), patch.object(
            context_plane, "fetch_receipts", return_value={"receipts": []}
        ):
            result = context_plane.cmd_verify(
                "https://context.example.test", "TEST_CONTEXT_TOKEN", 100, 2
            )
        self.assertEqual(result, 0)
        conn = sqlite3.connect(self.db_path)
        states = conn.execute(
            "SELECT DISTINCT state, last_error FROM cloud_outbox"
        ).fetchall()
        conn.close()
        self.assertEqual(
            states, [("pending", "receipt missing; retrying idempotently")]
        )

    def test_stage_requires_a_baseline_or_explicit_backfill(self):
        self.assertEqual(context_plane.cmd_stage(), 2)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM cloud_outbox").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_baseline_and_stage_tolerate_a_missing_recipes_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE recipes")
        conn.commit()
        conn.close()

        self.assertEqual(context_plane.cmd_baseline(), 0)
        self.assertEqual(context_plane.cmd_stage(), 0)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT value FROM context_plane_meta WHERE key = 'stage_cursor:recipes'"
            ).fetchone(),
            ("0",),
        )
        conn.close()

    def test_existing_raw_outbox_payload_is_sanitized_before_retry(self):
        self.assertEqual(context_plane.cmd_stage(backfill=True), 0)
        conn = sqlite3.connect(self.db_path)
        event_id, serialized = conn.execute(
            "SELECT event_id, payload_json FROM cloud_outbox LIMIT 1"
        ).fetchone()
        payload = json.loads(serialized)
        payload["provenance"]["evidence_excerpt"] = "verbatim local prompt text"
        conn.execute(
            "UPDATE cloud_outbox SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), event_id),
        )
        conn.commit()
        conn.close()

        deliver = patch.object(context_plane, "deliver")
        with patch.dict(
            os.environ,
            {"TEST_CONTEXT_TOKEN": "secret"},
        ), deliver as deliver_mock:
            self.assertEqual(
                context_plane.cmd_push(
                    "https://context.example.test", "TEST_CONTEXT_TOKEN", 50, 1
                ),
                2,
            )
            deliver_mock.assert_not_called()

        self.assertEqual(context_plane.cmd_sanitize_outbox(), 0)
        conn = sqlite3.connect(self.db_path)
        cleaned = json.loads(
            conn.execute(
                "SELECT payload_json FROM cloud_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()[0]
        )
        conn.close()
        self.assertNotIn("evidence_excerpt", cleaned["provenance"])


if __name__ == "__main__":
    unittest.main()
