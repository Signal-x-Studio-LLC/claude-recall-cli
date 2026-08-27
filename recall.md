# /recall — Session Recall Manager

Save the current session as a reusable recall entry, or search past entries.

## Usage

- `/recall save` — Extract a recall entry from the current session
- `/recall find <query>` — Search saved entries by keyword
- `/recall list` — Show recent entries
- `/recall show <id>` — Show full entry details
- `/recall use <id>` — Get the prompt template ready to paste (with variable hints)
- `/recall use <id> --var key=value` — Get the prompt with variables filled in
- `/recall stats` — Show library statistics (counts, tags, cost)
- `/recall analyze` — Analyze current or specific session for quality
- `/recall quality` — Quality trends across recent sessions
- `/recall verify <id>` — Rate a session's outcome for quality correlation
- `/recall backfill` — Backfill analysis metrics on older entries
- `/recall review` — Verify pending cloud memory candidates and promote the ones that hold up
- `/recall nominations` — Walk sessions the scan flagged as worth saving as recipes

## Instructions

When the user runs `/recall save`:

1. Read the current session's JSONL file. The session ID is available from the current conversation context. The session file is at `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl`.

2. Extract the key information by analyzing the session transcript:
   - Filter to `type=user` messages for intent (especially the first one)
   - Filter to `type=assistant` messages with `tool_use` content blocks for actions taken
   - Identify the tools used, files touched, and commands run

3. Generate a structured entry with these fields:
   - **intent**: One sentence — what was the user trying to accomplish?
   - **sources**: JSON array of files, APIs, databases, or references consulted
   - **key_commands**: JSON array of the 3-5 most important tool calls (skip exploratory reads)
   - **outcome**: What was produced? Files created/modified, data generated
   - **prompt_template**: A reusable prompt with `{{variable}}` placeholders for parts that would change
   - **quality_class**: One of: `high_value`, `productive`, `neutral`, `churn`, `dead_end`
   - **quality_reason**: One sentence explaining the rating
   - **tags**: JSON array of 3-7 lowercase keywords

4. Run the save script:
   ```bash
   python3 ~/.claude/commands/recall-cli.py save \
     --session-id "<session-id>" \
     --project "<project-path>" \
     --intent "<intent>" \
     --sources '<json-array>' \
     --key-commands '<json-array>' \
     --outcome "<outcome>" \
     --prompt-template "<template>" \
     --quality-class "<class>" \
     --quality-reason "<reason>" \
     --tags '<json-array>'
   ```

5. Confirm to the user what was saved and show the entry ID.

When the user runs `/recall nominations`:

Capture is the binding constraint on this whole system. Seventeen recipes exist
across 141 days, and twelve of those were one backfill — so retrieval has almost
nothing to retrieve. This command exists to move capture off "remember to run
`/recall save`" and onto "the scan nominates, the operator confirms."

The scheduled sync writes the queue to `~/.claude/recall-nominations.json`. Read
it; do not re-scan. Each entry carries `session_id`, `project`, `intent`,
`score`, and `reason`.

1. For each nomination, read enough of the session transcript to judge whether a
   *reusable* procedure is in there. A high score means the session was
   productive, not that it generalizes. A one-off debugging slog can score well
   and be worth nothing to future work.

2. Recommend save or decline for each, in one line, with the reason. Then stop
   and let the operator choose. Do not save on your own judgment.

3. For accepted ones, run the `/recall save` workflow above against that
   session ID. Write the `prompt_template` for reuse rather than as a
   description of what happened — the template is the whole point, and a
   recipe whose template only narrates the past is dead weight.

4. For declined ones, record the dismissal so it stops resurfacing:

   ```bash
   python3 ~/.claude/commands/recall-scan.py --decline "<session-id>"
   ```

   Declining is not a soft no. An undeclined nomination reappears on every scan
   and trains the operator to ignore the nudge, which costs more than the
   nomination was worth.

When the user runs `/recall review`:

Candidates are unreviewed evidence. `search_memory` only ever returns `Approved`
records, so a candidate nobody reviews is a memory that never reaches any agent.
This command exists because nothing else in the loop promotes one.

Review is two jobs, and they are not equally automatable. **Verification** —
re-deriving each claim at its source — is yours to do, and it is most of the
work. **Authorization** — deciding this should guide future work — is the
operator's, and it is the entire reason the two-state design exists. Never call
`set_memory_status` on your own judgment. `Approved` has to keep meaning "Nino
vouched for this," not "a model didn't object."

1. List the queue with the `recall-context` MCP server's `list_memory_candidates`.
   If it returns nothing, check `~/.claude/recall-context-heartbeat.json` before
   reporting an empty queue — a stalled sync and a clear queue look identical
   from the count alone.

2. For each candidate, find the load-bearing claims — anything asserting that
   something was built, shipped, proven, verified, or measured — and re-derive
   each one where the evidence actually lives. The candidate's `project` field
   is the starting point; claims often reference other repos.

   Read the file, run the grep, check the schema. Never accept the candidate's
   own claim of having been verified as evidence that it was: that is the
   charitable self-attestation failure in the global CLAUDE.md, and a review
   built on it is circular. A receipt with concrete numbers (counts, commit
   SHAs, line references) is real evidence; prose asserting diligence is not.

3. Check provenance health. If `project` names a path that no longer exists —
   commonly a reaped worktree — say so. `set_memory_status` cannot repair the
   field, so the operator is choosing whether to accept a record that
   project-scoped search will not match.

4. For candidates carrying external or high-stakes claims, run a non-Claude pass
   via the `adversarial-review` skill (Mode A, local `codex` CLI). It has
   filesystem access, so it can re-derive rather than take the candidate's word.
   Skip this for self-contained method recipes.

5. Present each candidate with a recommendation, the specific evidence you
   checked, and anything that failed to verify. Then stop and ask for the call.

6. Only after the operator answers, call `set_memory_status` per candidate. The
   `reason` must name what was verified and against what, plus any defect being
   knowingly accepted. Write it for someone reading the audit trail in a year
   with none of this context.

When the user runs `/recall find <query>`:

```bash
python3 ~/.claude/commands/recall-cli.py find "<query>"
```

Display the results in a readable format with intent, quality class, tags, and date.

When the user runs `/recall list`:

```bash
python3 ~/.claude/commands/recall-cli.py list
```

When the user runs `/recall show <id>`:

```bash
python3 ~/.claude/commands/recall-cli.py show "<id>"
```

Display the full entry including the prompt template.

When the user runs `/recall use <id>` (with optional `--var key=value`):

```bash
python3 ~/.claude/commands/recall-cli.py use "<id>" [--var key=value ...]
```

Display the filled prompt template prominently. If there are unfilled `{{variables}}`, ask the user for values before proceeding. Once all variables are filled, ask: "Want me to run this now?" If yes, execute the filled prompt as if the user had typed it.

When the user runs `/recall stats`:

```bash
python3 ~/.claude/commands/recall-cli.py stats
```

Display a clean summary of: total entries, quality breakdown, top tags, total tracked cost.

When the user runs `/recall-scan` (or `/recall-scan N`):

```bash
python3 ~/.claude/commands/recall-scan.py --days <N|all> --min-score 30 --limit 15
```

Display candidates ranked by score. For each high-scoring candidate, offer to extract an entry using the `/recall save` workflow.

When the user runs `/recall analyze`:

Analyze the current session or a specific one. To analyze the current session, find the session JSONL file from context. To analyze a specific session:

```bash
python3 ~/.claude/commands/recall-cli.py analyze --session-id "<session-id>"
```

Or by file path:

```bash
python3 ~/.claude/commands/recall-cli.py analyze --file "<path-to-jsonl>"
```

Display the results as a quality report card with:
- Overall grade (A-F) and score (0-100)
- Five category scores: tool selection, planning (thrash), prompt clarity, cost efficiency, anti-patterns
- Specific issues found (tool misuses, re-edited files, repeated commands, exploration dead-ends)
- Actionable recommendations based on the weakest categories

When the user runs `/recall quality` (with optional `--days N`):

```bash
python3 ~/.claude/commands/recall-cli.py quality --days <N|all> --limit 50
```

Display a trends dashboard with:
- Compliance grade distribution (graded, from baseline.json)
- Process metric averages and session shape distribution (descriptive, not graded)
- Total cost and tokens across the period
- Worst compliance sessions (investigate these)
- Recent sessions with compliance grades and process scores

When the user runs `/recall verify <id>`:

```bash
python3 ~/.claude/commands/recall-cli.py verify "<id>" --outcome pass|fail --satisfaction 1-5 --followup yes|no
```

All flags are optional but at least one must be provided. This labels saved entries with outcome data for future quality correlation. After saving a new entry, always prompt the user to verify it.

When the user runs `/recall backfill`:

```bash
python3 ~/.claude/commands/recall-cli.py backfill
```

Retroactively fills analysis metrics (compliance grade, process score, session shape, thrash ratio, etc.) on existing entries that were saved before the analysis feature existed. Only works for entries whose session JSONL files still exist.
