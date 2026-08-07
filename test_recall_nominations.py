"""Tests for the recipe-nomination queue and the SessionStart nudge.

Capture is the binding constraint on recall: seventeen recipes across 141 days.
These cover the two ways the nomination path fails silently — a threshold no
session can clear, and a decline that does not stick.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _load(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parent / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


recall_scan = _load("recall_scan", "recall-scan.py")
nudge = _load("review_nudge", "adapters/session-start-review-nudge.py")


class MachineAuthoredTests(unittest.TestCase):
    """137 of 144 sessions in a 3-day window were auto commit-review runs."""

    def test_commit_review_template_is_machine_authored(self):
        self.assertTrue(
            recall_scan.is_machine_authored(
                'Review a single git commit in the repository "site-docs"'
            )
        )

    def test_agent_and_command_wrappers_are_machine_authored(self):
        for prompt in (
            "<task-notification>\nbackground task done",
            "<command-name>/recall</command-name>",
            "Another Claude session sent a message",
        ):
            self.assertTrue(recall_scan.is_machine_authored(prompt), prompt)

    def test_real_prompts_pass(self):
        for prompt in (
            "use browse tool to look at https://example.com/events",
            "it feels like the story telling could be tighter",
            "how do we automate candidate review?",
        ):
            self.assertFalse(recall_scan.is_machine_authored(prompt), prompt)

    def test_empty_prompt_is_not_machine_authored(self):
        self.assertFalse(recall_scan.is_machine_authored(None))
        self.assertFalse(recall_scan.is_machine_authored(""))


class NominationQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nominations.json"
        recall_scan.NOMINATIONS_PATH = self.path
        nudge.NOMINATIONS_PATH = self.path

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, nominations, declined):
        self.path.write_text(
            json.dumps({"nominations": nominations, "declined": declined})
        )

    def test_write_preserves_declined_across_rescans(self):
        """A rescan regenerates nominations but must not forget a decline.

        Without this the same session is re-nominated every 15 minutes and the
        nudge trains the operator to ignore it.
        """
        self._write([], ["sess-declined"])
        recall_scan.write_nominations([{"session_id": "sess-new", "intent": "x"}])
        after = json.loads(self.path.read_text())
        self.assertEqual(after["declined"], ["sess-declined"])
        self.assertEqual(len(after["nominations"]), 1)

    def test_read_declined_survives_a_corrupt_file(self):
        self.path.write_text("not json")
        self.assertEqual(recall_scan.read_declined(), set())

    def test_read_declined_on_missing_file(self):
        self.assertEqual(recall_scan.read_declined(), set())


class NudgeNominationLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nominations.json"
        nudge.NOMINATIONS_PATH = self.path

    def tearDown(self):
        self.temp.cleanup()

    def test_silent_when_queue_is_empty(self):
        self.path.write_text(json.dumps({"nominations": [], "declined": []}))
        self.assertIsNone(nudge.nomination_line())

    def test_silent_when_file_missing(self):
        self.assertIsNone(nudge.nomination_line())

    def test_silent_when_file_corrupt(self):
        self.path.write_text("{{{")
        self.assertIsNone(nudge.nomination_line())

    def test_reports_count_and_top_intent(self):
        self.path.write_text(
            json.dumps(
                {
                    "nominations": [
                        {"session_id": "a", "intent": "use browse tool on the site"},
                        {"session_id": "b", "intent": "second one"},
                    ],
                    "declined": [],
                }
            )
        )
        line = nudge.nomination_line()
        self.assertIn("2 sessions", line)
        self.assertIn("/recall nominations", line)
        self.assertIn("use browse tool", line)

    def test_singular_wording_for_one(self):
        self.path.write_text(
            json.dumps({"nominations": [{"session_id": "a", "intent": "x"}]})
        )
        self.assertIn("1 session worth", nudge.nomination_line())


if __name__ == "__main__":
    unittest.main()
