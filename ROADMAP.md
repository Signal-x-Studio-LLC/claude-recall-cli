# Recall CLI — Quality Measurement Roadmap

Paused 2026-03-24. Resume ~2026-06-24 with 3 months of accumulated session data.

## Resume run executed 2026-07-02

`extract --days 90` + `correlate` on n=838 sessions (pass rate 0.498, followup-fix rate 0.142). Followup-fix predictors, sorted:

| Metric | r (Jul, n=838) | r (Mar, n=349) |
|--------|----------------|----------------|
| prompt_count | +0.227 | +0.18 |
| anti_pattern_count | +0.213 | +0.15 |
| edit_count | +0.202 | +0.18 |
| focused_thrash | +0.198 | +0.14 |
| tool_misuses | +0.192 | +0.12 |
| thrash_ratio | +0.135 | +0.03 |
| compliance_score | -0.096 | -0.08 |

Every live signal strengthened with 2.4x the data, but none cleared the r > 0.25 gate for building `/recall calibrate` — max r² ≈ 0.05. Verdict per the gate: do not build calibrate; the "accept the ceiling" branch (item 6) applies unless external signals (git diffs, CI results) get wired in. The quality-measurement half costs nothing passively (extraction is manual), so it stays as-is rather than being archived. Separate observation from the same audit: the recipe library has had no saves since 2026-03-19 — the save/reuse habit never formed; the live value is Poe + followup-fix detection.

---

## What we built (2026-03-24)

### Recall library (working, stable)
- `/recall save|find|list|show|use|stats` — session recipe library with SQLite + FTS5
- `/recall-scan` — batch scan sessions for recall-worthy patterns
- Auto-population of analysis metrics on save

### Quality analysis (working, honest about limitations)
- `/recall analyze` — single-session compliance grade + process metrics
- `/recall quality` — trends across recent sessions
- `/recall verify` — manual outcome labeling (satisfaction, pass/fail, followup)
- `/recall backfill` — retroactive analysis on older entries
- Two-layer architecture: compliance (graded, from baseline.json) vs process (descriptive, from thresholds.json)

### Automated outcome pipeline (working, most valuable piece)
- `/recall extract --days N` — batch feature extraction from all sessions
- Followup-fix auto-detection via cross-session file overlap (4-hour window)
- Commit auto-detection from Bash tool calls
- `/recall correlate` — Pearson correlation with p-values, zero dependencies
- DB: `session_features` table with 349 rows, 24 columns, fully automated

---

## What we learned (the important part)

### Correlation results (n=349, 30 days, heuristic v4)

**Followup fix prediction (sorted by effect size):**

| Metric | r | p | Significant? | New in v4? |
|--------|---|---|-------------|------------|
| edit_count | +0.18 | 0.0006 | Yes | |
| prompt_count | +0.18 | 0.0008 | Yes | |
| anti_pattern_count | +0.15 | 0.006 | Yes | |
| focused_thrash | +0.14 | 0.007 | Yes | New |
| tool_misuses | +0.12 | 0.027 | Yes | |
| compliance_score | -0.08 | 0.14 | No | |
| tokens_per_output | +0.07 | 0.17 | No | |
| process_score | -0.05 | 0.38 | No | |
| model_switches | -0.04 | 0.45 | No | New, dead |
| thrash_ratio | +0.03 | 0.60 | No | |
| duration_min | +0.01 | 0.85 | No | New, dead |
| late_error_rate | +0.00 | 1.00 | No | New, dead |

**Max r² = 0.03.** Process metrics explain ~3% of followup-fix variance. The rest is task complexity, domain knowledge, code quality (which we can't observe from session structure), and noise.

### What was wrong

1. **Compliance scoring penalized productivity.** Sessions with commits scored 79.2 vs 85.9 for no-commit sessions. More tool calls = more chances to trigger misuse flags. Fixed by normalizing to misuse *rate* (per 100 Bash calls), which reduced the gap from 19 points to 7.

2. **Prompt clarity punished deliberate architecture discussions.** The original metric penalized sessions where coding started late. Fixed by classifying session *shape* (research_then_build, direct_execution, etc.) instead of scoring prompt-to-output latency.

3. **Composite scores (compliance_score, process_score) don't predict anything.** They measure session complexity, not session quality. The individual raw signals (edit_count, focused_thrash, anti_pattern_count) are weak but real predictors; the composites wash them out.

4. **Thrash ratio is noise.** r=+0.03, p=0.60. Edits per unique file doesn't predict followup fixes. But `focused_thrash` (max edits to a single file) does (r=+0.14, p=0.007). The signal is in *concentrated* rework, not distributed rework.

5. **Three of four new exploratory features were dead signals.** Late error rate (JSONL error structure unreliable), duration (wall-clock time irrelevant), model switches (doesn't predict anything). Only focused_thrash carried signal.

6. **The original plan assumed manual labeling.** We designed `/recall verify` for manual outcome labels, estimated 3 weeks to 50 entries. Instead, automated commit detection + followup-fix cross-referencing labeled 349 sessions in seconds with zero manual input.

### What's actually valuable

In order of utility:

1. **Followup-fix auto-detection.** Cross-session file overlap analysis. Zero manual input. The most novel and useful thing in the repo.
2. **The correlation framework.** Proves what works and what doesn't. Prevents scoring theater.
3. **The recall library itself.** Save/search/reuse session patterns. The original feature, still the daily driver.
4. **Compliance checker.** Binary rule checks from baseline.json. Legitimate but doesn't predict outcomes.
5. **Process metrics.** Descriptive telemetry. Useful for outlier detection, not quality judgment.

---

## What to do when resuming (~June 2026)

### Immediate (day 1)

1. **Re-extract with 3 months of data:**
   ```
   /recall extract --days 90
   /recall correlate
   ```
   With ~1000+ sessions, weak signals may strengthen or disappear. This is the first thing to do.

2. **Update baseline.json** if Claude Code has updated its system prompt (check version with `claude --version`, compare to `_claude_code_version` in baseline.json).

### If correlations strengthen (any r > 0.25 for followup fix)

3. **Build `/recall calibrate`** — derive thresholds from percentiles of known-good sessions (sessions with commits and no followup fix). Write to thresholds.json with `_calibrated_from` metadata.

4. **Try interaction terms:**
   - `edit_count * focused_thrash` — big session hammering one file
   - `anti_pattern_count / prompt_count` — anti-pattern density (normalized)
   - `focused_thrash / unique_files` — concentration of rework

5. **Try the logistic regression** from IMPLEMENTATION.md Level 5. With 1000+ sessions, even weak individual predictors might combine usefully.

### If correlations stay flat (all r < 0.20)

6. **Accept the ceiling.** Process metrics from session JSONL are structurally limited — they can't see code correctness, test results, or domain appropriateness. Document this honestly and focus the tool on what it does well: the recall library and followup-fix detection.

7. **Consider external signals** that might break through:
   - **Git diff size** — run `git diff --stat` after sessions with commits. Large diffs that get reverted = quality signal.
   - **CI/CD results** — if a session's commits trigger CI, did CI pass? (Requires project-specific integration.)
   - **File age** — editing old stable files vs new files. Regressions in stable code are worse.

   These require reading git state, not just session JSONL. They're a fundamentally different data source.

### What NOT to revisit

- **late_error_rate** — JSONL error structure is unreliable. Don't retry without a better error detection method.
- **duration_min** — wall-clock time is noise. Don't try to make it work.
- **model_switches** — no signal. Not worth exploring further.
- **AI-graded quality** — using Claude to judge Claude is circular. Stick to observable outcomes.
- **Prompt content analysis** — privacy risk, NLP complexity, low signal-to-noise. The structural metrics are already at the ceiling.

---

## Data state at pause

| Table | Rows | Key columns |
|-------|------|-------------|
| recipes | 12 | 6 with analysis metrics, 1 manually verified |
| recipes_fts | 12 | Full-text search index |
| session_features | 349 | 24 feature columns, auto-labeled outcomes |

- `recall.db` at `~/.claude/recall.db`
- `baseline.json` pinned to Claude Code 2.1.81
- `thresholds.json` — arbitrary (not calibrated from data)
- `HEURISTIC_VERSION = 4`

---

## Maturity model

| Level | Name | Status | What we can say |
|-------|------|--------|-----------------|
| 0 | Vibes | Done | "That felt productive" |
| 1 | Compliance | Done | "Claude followed rules at 79% rate" |
| 2 | Descriptive | Done | "Research-then-build session, 2.7 thrash ratio" |
| 3 | Correlated | **Done** | "Compliance doesn't predict fixes. Focused thrash does, weakly (r=0.14)." |
| 4 | Calibrated | Blocked | Needs stronger signal or more data |
| 5 | Predictive | Blocked | r²=0.03 is too low for useful prediction |
| 6 | Benchmarked | Future | Needs multiple users |

**The honest conclusion:** Session-level process metrics from JSONL are inherently weak predictors of code quality. The maximum variance explained is ~3%. This may improve with more data (1000+ sessions) or external signals (git diffs, CI results), but it may also be a hard ceiling. The tool's real value is the recall library and the followup-fix detection infrastructure, not the quality scores.
