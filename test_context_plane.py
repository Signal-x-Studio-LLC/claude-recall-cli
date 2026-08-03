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
            self.assertEqual(context_plane.cmd_stage(), 0)
            self.assertEqual(context_plane.cmd_stage(), 0)
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

    def test_failed_push_stays_pending_then_recovers(self):
        context_plane.cmd_stage()
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
        context_plane.cmd_stage()
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


if __name__ == "__main__":
    unittest.main()
