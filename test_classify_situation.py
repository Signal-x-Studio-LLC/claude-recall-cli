"""Golden test for classify_situation — the load-bearing classifier behind
both poe-check and the prompt-hook. Hand-labeled (draft, expected_situations)
pairs cover positive cases (each situation should fire) and negative cases
(neutral drafts should fire NOTHING).

Run with: /usr/bin/python3 -m pytest test_classify_situation.py -v
Or directly: /usr/bin/python3 test_classify_situation.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "poe-extract.py"
spec = importlib.util.spec_from_file_location("poe_extract", SCRIPT)
poe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poe)
classify_situation = poe.classify_situation
classify_prompt_intent = poe.classify_prompt_intent


# ============================================================
# POSITIVE FIXTURES — each draft should fire its expected situations.
# Format: (label, draft_text, expected_situations_set)
# ============================================================
POSITIVE_CASES: list[tuple[str, str, set[str]]] = [
    # --- hesitation (the highest-volume failure mode) ---
    ("hes-want-to", "I've finished the first slice. Want me to keep going to slice 2?", {"hesitation"}),
    ("hes-should-i", "The migration is ready. Should I proceed with the apply?", {"hesitation"}),
    ("hes-do-you-want", "Plan is drafted. Do you want me to start on the implementation now?", {"hesitation"}),
    ("hes-shall", "All tests pass. Shall I push to main?", {"hesitation"}),
    ("hes-let-me-know", "Refactor complete. Let me know if you want me to continue with the cleanup pass?", {"hesitation"}),
    ("hes-or-stop", "I can do A or B next — which should I do, or stop here?", {"hesitation"}),
    ("hes-proceed-q", "Ready when you are. Proceed?", {"hesitation"}),
    # Statement-form soft closers — these were the P1 Poe-ON test gap.
    ("hes-soft-ready", "Plan is laid out. Ready when you are.", {"hesitation"}),
    ("hes-soft-saytheword", "I've got the implementation drafted. Say the word and I'll commit.", {"hesitation"}),
    ("hes-soft-letme", "Migration script written. Let me know when to run it.", {"hesitation"}),
    ("hes-soft-whenever", "All tests pass. Whenever you're ready, I'll push.", {"hesitation"}),
    # Blank-line separator (paragraph break) before the soft closer — common
    # in real responses. \s vs \s+ regression guard.
    ("hes-soft-paragraph", "All steps drafted.\n\nReady when you are.", {"hesitation"}),

    # --- hedge-dense ---
    ("hedge-3x", "I think this might possibly work, but maybe we should verify it first.", {"hedge-dense"}),
    ("hedge-classic", "Perhaps the issue could be related to caching — I think we might see it if we look closer.", {"hedge-dense"}),

    # --- cheerleading ---
    ("cheer-great", "Great job on that fix! Looks perfect.", {"cheerleading"}),
    ("cheer-amazing", "That's amazing work — absolutely the right call.", {"cheerleading"}),

    # --- destructive-action ---
    ("dest-rm", "I'll run rm -rf /tmp/build to clean up before we restart.", {"destructive-action"}),
    ("dest-force-push", "Going to force-push to fix the wrong commit on main.", {"destructive-action"}),
    ("dest-drop-table", "We need to drop table sessions to reset the state.", {"destructive-action"}),
    ("dest-no-verify", "Will commit with --no-verify since the hook is broken.", {"destructive-action"}),

    # --- vague-rationale (long with no causal connective) ---
    ("vague-long", (
        "Here is the plan. We will modify the configuration to use the new format. "
        "First we update the schema. Then we run the migration. Then we verify the output. "
        "After that we deploy to staging. Finally we promote to production."
    ), {"vague-rationale"}),

    # --- over-engineered (multiple what-if branches) ---
    ("over-whatif", (
        "We need to handle several scenarios. What if the user has no permissions? "
        "What if the token expires mid-request? What if there are concurrent updates?"
    ), {"over-engineered"}),

    # --- trailing-summary ---
    ("summary-in-summary", "Made the edit. In summary, this fixes the bug by adjusting the threshold.", {"trailing-summary"}),
    ("summary-recap", "Done. To recap, we updated three files and added two tests.", {"trailing-summary"}),

    # --- combinations ---
    ("combo-hes-hedge", (
        "I think this might be ready — maybe we should test it more. "
        "Want me to keep going with the next slice or stop here?"
    ), {"hesitation", "hedge-dense"}),
    ("combo-cheer-summary", (
        "Great work on the architecture! In summary, the system is now fully migrated."
    ), {"cheerleading", "trailing-summary"}),
]


# ============================================================
# NEGATIVE FIXTURES — these should fire NOTHING.
# The hook produces noise when neutral drafts trigger classifiers,
# so false-positive prevention matters as much as recall.
# ============================================================
NEGATIVE_CASES: list[tuple[str, str]] = [
    ("neg-status-sentence", "v0.3 slice 1 landed; moving to slice 2 — wiring the runner now."),
    ("neg-imperative-direction", "Doing the migration next — flag if you want to stop."),
    ("neg-short-direct", "Done. Tests pass."),
    ("neg-with-because", (
        "We switched to porter stemming because FTS5's default tokenizer doesn't "
        "handle morphological variation. The change adds index time but improves recall."
    )),
    ("neg-question-not-hesitation", "What stack are you using for the frontend?"),
    ("neg-technical-explanation", (
        "The watermark table tracks last-ingested mtime per session file so catchup is "
        "idempotent. Each entry point is safe to call from any trigger."
    )),
    ("neg-list-no-whatif", (
        "Three changes landed: tone fingerprint, project filter, porter stemmer. "
        "All three are verified end-to-end."
    )),
    ("neg-question-mark-only", "Does the watermark survive a database reset?"),
    # Soft-closer phrase in mid-prose (a quote, explanation) — must NOT trigger.
    ("neg-soft-midprose", "The hook should detect 'ready when you are' patterns when they appear at the end of a response, not mid-prose like this."),
]


# ============================================================
# Runner — minimal, works without pytest.
# ============================================================

# ============================================================
# PROMPT-INTENT FIXTURES — for classify_prompt_intent.
# Operates on USER PROMPTS, not on drafts. Predicts what failure modes
# Claude is likely to commit in response.
# ============================================================
PROMPT_INTENT_POSITIVE: list[tuple[str, str, set[str]]] = [
    ("intent-hes-walk", "Walk me through how you would build a small URL shortener in Python.", {"invites-hesitation"}),
    ("intent-hes-checkin", "Lay out each step and let me know when you are ready to continue.", {"invites-hesitation"}),
    ("intent-hedge-mirror", "What might possibly be the issue if my Node.js server is returning 502 errors? I think it could be related to timeouts.", {"invites-hedging"}),
    ("intent-cheer", "Can you review the migration strategy and give me your honest thoughts?", {"invites-cheerleading"}),
    ("intent-over-eng-auth", "Design an auth system for a new app.", {"invites-over-engineering"}),
    ("intent-over-eng-pipeline", "Build a data pipeline for the analytics workflow.", {"invites-over-engineering"}),
    ("intent-vague-explain", "Explain how Promises work in JavaScript.", {"invites-vague-rationale"}),
    ("intent-vague-what-should", "What should we do about the slow API responses?", {"invites-vague-rationale"}),
    ("intent-destructive", "Clean up my git repo — delete the stale branches.", {"invites-destructive-action"}),
    ("intent-combo", "I am thinking maybe we should redesign the API. What do you think the right approach might be?", {"invites-hedging", "invites-cheerleading"}),
]

PROMPT_INTENT_NEGATIVE: list[tuple[str, str]] = [
    ("intent-neg-direct-task", "Now run step 2 of the migration plan."),
    ("intent-neg-status", "Just confirming: did the deploy finish successfully?"),
    ("intent-neg-bounded-design", "Design a simple stub auth that just checks a static token."),
    ("intent-neg-constrained-explain", "Briefly explain why we chose porter stemming, in two sentences."),
    ("intent-neg-bare-question", "Where is the watermark stored?"),
    ("intent-neg-direct-fix", "The test on line 42 is broken. Fix it."),
]


def run_intent_positive() -> tuple[int, int, list[str]]:
    passed = 0
    failed: list[str] = []
    for label, prompt, expected in PROMPT_INTENT_POSITIVE:
        hits = classify_prompt_intent(prompt)
        got = {sit for sit, _ in hits}
        missing = expected - got
        if not missing:
            passed += 1
        else:
            failed.append(f"  {label}: expected {expected}, got {got} (missing {missing})")
    return passed, len(PROMPT_INTENT_POSITIVE), failed


def run_intent_negative() -> tuple[int, int, list[str]]:
    passed = 0
    failed: list[str] = []
    for label, prompt in PROMPT_INTENT_NEGATIVE:
        hits = classify_prompt_intent(prompt)
        got = {sit for sit, _ in hits}
        if not got:
            passed += 1
        else:
            failed.append(f"  {label}: expected silence, got {got}")
    return passed, len(PROMPT_INTENT_NEGATIVE), failed


def run_positive() -> tuple[int, int, list[str]]:
    passed = 0
    failed: list[str] = []
    for label, draft, expected in POSITIVE_CASES:
        hits = classify_situation(draft)
        got = {sit for sit, _ in hits}
        missing = expected - got
        if not missing:
            passed += 1
        else:
            failed.append(f"  {label}: expected {expected}, got {got} (missing {missing})")
    return passed, len(POSITIVE_CASES), failed


def run_negative() -> tuple[int, int, list[str]]:
    passed = 0
    failed: list[str] = []
    for label, draft in NEGATIVE_CASES:
        hits = classify_situation(draft)
        got = {sit for sit, _ in hits}
        if not got:
            passed += 1
        else:
            failed.append(f"  {label}: expected silence, got {got}")
    return passed, len(NEGATIVE_CASES), failed


def main() -> int:
    p_pass, p_total, p_fail = run_positive()
    n_pass, n_total, n_fail = run_negative()
    ip_pass, ip_total, ip_fail = run_intent_positive()
    in_pass, in_total, in_fail = run_intent_negative()

    print("== classify_situation (draft classifier) ==")
    print(f"POSITIVE: {p_pass}/{p_total} passed")
    if p_fail:
        print("FAILURES:")
        for line in p_fail:
            print(line)
    print(f"NEGATIVE: {n_pass}/{n_total} passed")
    if n_fail:
        print("FAILURES:")
        for line in n_fail:
            print(line)
    print()
    print("== classify_prompt_intent (prompt classifier) ==")
    print(f"POSITIVE: {ip_pass}/{ip_total} passed")
    if ip_fail:
        print("FAILURES:")
        for line in ip_fail:
            print(line)
    print(f"NEGATIVE: {in_pass}/{in_total} passed")
    if in_fail:
        print("FAILURES:")
        for line in in_fail:
            print(line)
    print()
    total_pass = p_pass + n_pass + ip_pass + in_pass
    total = p_total + n_total + ip_total + in_total
    print(f"OVERALL: {total_pass}/{total} ({round(100*total_pass/total,1)}%)")
    all_fail = p_fail + n_fail + ip_fail + in_fail
    return 0 if not all_fail else 1


# pytest-style discoverable tests for the optional pytest path
def test_positive_cases() -> None:
    p_pass, p_total, p_fail = run_positive()
    assert p_fail == [], "\n".join(p_fail)


def test_negative_cases() -> None:
    n_pass, n_total, n_fail = run_negative()
    assert n_fail == [], "\n".join(n_fail)


if __name__ == "__main__":
    sys.exit(main())
