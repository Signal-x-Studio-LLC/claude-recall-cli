# Field-validation gate

The implementation is a project now. A rename and a reflective blog post are not earned yet.

The public tutorial and interactive demo can explain the architecture and its trust boundary. They must call the deployment a working private prototype until the checks below pass on real sessions.

## Evidence required before a rename

- Two physical machines ingest through separate durable outboxes.
- Claude Code, Codex, and Gemini CLI each contribute at least five processed candidates.
- At least one offline interval proves retry and later acknowledgment without duplicate memories.
- At least ten candidates receive human review across Approved, Contradicted, Superseded, and Stale outcomes.
- Each harness retrieves the same Approved memory and omits the same non-Approved memory by default.
- A superseded memory points to its replacement and preserves both event histories.
- A D1 Time Travel restore is rehearsed without overwriting the live database.
- An R2 snapshot is regenerated from D1 and compared with the Approved row count.
- Credential-shaped test payloads are rejected, and no raw transcript fields appear in D1 or R2.

Record exact commands, timestamps, Worker deployment version, machine labels, event IDs, and memory IDs. Screenshots and session summaries can illustrate the run, but they do not replace the receipts.

## Deliverables after the gate

1. Rename the repository only if users understand the multi-harness scope and the old Claude-specific name is causing real confusion.
2. Publish the blog post as a measured field report: what the three harnesses produced, what failed, what the cloud layer changed, and which assumptions did not survive.
3. Update the tutorial from prototype instructions to a repeatable install path.
4. Tag the first release whose claims match the receipts.

Until then, keep the existing repository name and label the Cloudflare layer `0.1 prototype`.

