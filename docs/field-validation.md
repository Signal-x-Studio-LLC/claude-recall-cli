# Field-validation gate

The implementation is a project now. A rename and a reflective blog post are not earned yet.

The public tutorial and interactive demo can explain the architecture and its trust boundary. They must call the deployment a working private prototype until the checks below pass on real sessions.

## Evidence required before a rename

- Two physical machines ingest through separate durable outboxes.
- Claude Code, Codex, and Gemini CLI each contribute at least five manually promoted recipes that become processed candidates.
- At least one offline interval proves retry and later acknowledgment without duplicate memories.
- At least ten candidates receive human review across Approved, Contradicted, Superseded, and Stale outcomes.
- Each harness retrieves the same Approved memory and omits the same non-Approved memory by default.
- A superseded memory points to its replacement and preserves both event histories.
- A D1 Time Travel restore is rehearsed on a separate remote rehearsal database without overwriting the live database.
- An R2 snapshot is regenerated from D1 and compared with the Approved row count.
- Credential-shaped payloads and automatically mined voice signals are rejected. D1 and R2 may contain deliberately promoted recipe fields, but never a raw transcript, mined phrase, whole raw message, assistant response, or transcript-derived provenance excerpt.
- Both machines record separate before-and-after byte counts for raw transcripts, `recall.db`, the durable outbox, and linked-worktree build output. A total workspace size is not accepted as a memory-growth measurement.
- A retention dry run lists only archived transcripts whose exact path and modification time are covered by the ingest watermark. Active transcripts and unpromoted evidence remain ineligible for deletion.
- D1 and R2 storage are recorded after the field run so the release notes can state measured cloud growth instead of projecting from local transcript volume.

Record exact commands, timestamps, Worker deployment version, machine labels, event IDs, and memory IDs. Screenshots and session summaries can illustrate the run, but they do not replace the receipts.

## Deliverables after the gate

1. Rename the repository only if users understand the multi-harness scope and the old Claude-specific name is causing real confusion.
2. Publish the blog post as a measured field report: what the three harnesses produced, what failed, what the cloud layer changed, and which assumptions did not survive.
3. Update the tutorial from prototype instructions to a repeatable install path.
4. Tag the first release whose claims match the receipts.

Until then, keep the existing repository name and label the Cloudflare layer `0.1 prototype`.

## Run one machine

Use a stable public label such as `machine-a` or `machine-b`; do not put the hostname in a public receipt.

```bash
git clone git@github.com:nino-chavez/claude-recall-cli.git
cd claude-recall-cli
git switch codex/context-plane

op item list --vault "Developer Secrets" | rg 'Cloudflare recall-context-plane'

field_test_venv="$(mktemp -d "${TMPDIR:-/tmp}/recall-field-tests.XXXXXX")"
python3 -m venv "$field_test_venv"
"$field_test_venv/bin/python" -m pip install --quiet pytest==8.4.2
PYTHONDONTWRITEBYTECODE=1 "$field_test_venv/bin/python" -m pytest -q

python3 poe-extract.py drain-queue --include-codex --include-gemini
python3 context-plane.py baseline
python3 poe-extract.py retention-report --grace-days 7
python3 context-plane.py sanitize-outbox
python3 context-plane.py quarantine-local-only  # existing prototype outboxes only
python3 context-plane.py status

with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- \
  adapters/sync-context-plane.sh
```

Baseline only on a machine that has never synced this local database. On a previously initialized machine, omit `baseline`; running it again would hide unsent local records by moving the cursor.

Before any push, `status` must report both `unsafe_payloads: 0` and
`invalid_outbox_payloads: 0`. The Worker independently rejects automatic voice
signals, `provenance.evidence_excerpt`, and other extra provenance fields.
Existing prototype outboxes must run `sanitize-outbox`, then
`quarantine-local-only`, before retrying. The quarantine command replaces each
pending automatic-signal payload with a metadata-only local hash receipt; the
source signal remains in `recall.db`.

Read `/health` from the deployed Worker and record `version.id`; the response is the cross-machine deployment receipt and does not require Wrangler on the client machine.

The temporary virtual environment makes the local command independent of the
machine's ambient Python packages. `python3 -m unittest discover` is not an
equivalent fallback because it does not collect the pytest-style test functions.

Client validation does not require Wrangler access to the Cloudflare account.
Do not switch a machine's logged-in Wrangler account or provision another admin
credential merely to inspect deployments. The public `/health` version is the
deployment receipt; authenticated ingest and MCP use the scoped context-plane
bearer token from 1Password.

Each harness needs five real sessions whose durable lesson is worth promoting
to a recipe. Re-running a watermark sweep cannot create evidence, and synthetic
preferences do not satisfy this gate.

Install the three client adapters from `adapters/`, then perform the retrieval checks in [Connect Claude, Codex, and Gemini](connect-clients.md). A configuration listing is not a connection receipt. Record the client's connected status and the returned memory ID without copying retrieved text into the receipt.

Record local storage separately:

```bash
for path in \
  "$HOME/.claude/projects" \
  "$HOME/.codex/sessions" \
  "$HOME/.codex/archived_sessions" \
  "$HOME/.gemini/tmp" \
  "$HOME/.claude/recall.db"
do
  if [[ -e "$path" ]]; then du -sk "$path"; else echo "missing $path"; fi
done
```

Do not delete anything during field validation. The retention report always returns `deletion_authorized: false`; session closeout remains a separate human judgment.

## Current field runs

- [Machine A — 2026-08-03](field-runs/2026-08-03-machine-a.md): local baseline and retention receipt; multi-machine gate remains open.
- [Machine B partial run — 2026-08-03](field-runs/2026-08-03-machine-b-bootstrap.md): portable labels and local staging measured; stopped before push on a privacy violation.
