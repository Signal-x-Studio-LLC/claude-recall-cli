"""Rule-drift tests.

`drift` used to report stale voice *labels* — regex families like
`correction/undo`. That answered "which of my phrasings went quiet", which is
not a question anyone has. Retargeted 2026-08-26 at rules in the governing
docs, so a promoted rule has a demotion path.
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("poe-extract.py")
SPEC = importlib.util.spec_from_file_location("poe_extract_drift", MODULE_PATH)
poe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(poe)


DOC = """# Title

## Enforced rule

Guarded mechanically by `worktree-guard.py` on every commit.

## Parent rule

Intro paragraph with no distinctive content of its own.

### Sub-rule with the substance

The mechanism is `zzq-widget-tool` against the `zzq-standing-profile`.

## Generic rule

Be reasonable.

## Quiet rule

Uses `zzq-abandoned-helper` and `zzq-forgotten-path/` and nothing else.
"""


def run_drift(path: Path, days: int = 90):
    os.environ["POE_RULE_DOCS"] = str(path)
    poe.RECALL_DB = Path("/nonexistent/recall.db")  # no corpus -> no reinforcement
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            poe.cmd_drift(days=days, as_json=True)
    finally:
        os.environ.pop("POE_RULE_DOCS", None)
    return {r["rule"]: r for r in json.loads(buf.getvalue())["rules"]}


class RuleSectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = Path(self.tmp.name) / "rules.md"
        self.doc.write_text(DOC)
        self.addCleanup(self.tmp.cleanup)

    def test_parent_rule_owns_its_subsections(self):
        """A `##` runs to the next `##`, not the next heading of any level.

        Scoring a parent on its intro paragraph alone flagged 'Never fabricate
        Nino's interior state' as drift while its own sub-rule was the most
        reinforced section in the file.
        """
        sections = {s["title"]: s for s in poe._rule_sections(self.doc)}
        self.assertIn("zzq-widget-tool", sections["Parent rule"]["body"])
        self.assertNotIn("zzq-abandoned-helper", sections["Parent rule"]["body"])

    def test_terms_keep_identifiers_and_drop_generic_words(self):
        sections = {s["title"]: s for s in poe._rule_sections(self.doc)}
        terms = poe._rule_terms(sections["Parent rule"])
        self.assertIn("zzq-widget-tool", terms)
        self.assertNotIn("be", terms)

    def test_enforced_rule_is_alive_without_any_mention(self):
        """A hook enforcing a rule is stronger evidence than anyone talking
        about it. Never demote one for going quiet."""
        r = run_drift(self.doc)["Enforced rule"]
        self.assertEqual(r["verdict"], "enforced")
        self.assertIn("worktree-guard.py", r["enforcers"])

    def test_single_generic_term_is_unscorable_not_drift(self):
        """Absence of the word 'reasonable' is not evidence a rule died."""
        self.assertEqual(run_drift(self.doc)["Generic rule"]["verdict"], "unscorable")

    def test_distinctive_unmentioned_rule_is_a_demotion_candidate(self):
        self.assertEqual(run_drift(self.doc)["Quiet rule"]["verdict"], "drift")

    def test_explicit_doc_override_is_exhaustive(self):
        """POE_RULE_DOCS must not be silently widened with repo docs."""
        os.environ["POE_RULE_DOCS"] = str(self.doc)
        try:
            self.assertEqual([p.name for p in poe._rule_docs()], ["rules.md"])
        finally:
            os.environ.pop("POE_RULE_DOCS", None)


if __name__ == "__main__":
    unittest.main()
