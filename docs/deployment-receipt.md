# Private prototype deployment receipt

Deployed on 2026-08-02 CDT to `https://recall-context-plane.biq.workers.dev`.

This receipt proves the private Cloudflare path works. It does not satisfy the multi-machine field-validation gate.

## Deployed resources

| Resource | Name | Verified behavior |
|---|---|---|
| Worker | `recall-context-plane` | Version `edbd8c95-a671-44bf-83a8-ad8d688f6777`; `/health` returned 200 |
| D1 | `recall-context-plane` | Migration `0001_init.sql` applied remotely; FTS and state constraints present |
| Queue | `recall-context-ingest` | Synthetic event reached the consumer and received a processed receipt |
| Dead-letter Queue | `recall-context-dead-letter` | Bound after five configured retries; failure-path field test remains open |
| R2 | `recall-context-snapshots` | `snapshots/2026-08-03/approved.json` regenerated from D1 |
| AI Gateway | `recall-context` | Payload logging off, gateway authentication on, Zero Data Retention on |

AI normalization remains disabled. The gateway exists and the Worker binding is deployed, but no dynamic model route will process candidate text until provider choice and deterministic-path field evidence are reviewed.

## End-to-end smoke result

- An unauthenticated ingest request returned 401.
- A bearer-authenticated synthetic event returned `accepted: 1`.
- Queue processing produced receipt `evt_11111111111111111111111111111111` with status `processed` and memory `mem_06252788dc0969eb62b62a9a906717ac`.
- Default `search_memory` returned no result because the record was still Candidate.
- `list_memory_candidates` returned the same record.
- The audited MCP write path changed it from Candidate to Stale with review event `review_e93215b4-dc1d-4814-bb57-a78e807fe0fa`.
- The R2 snapshot contained zero memories, matching D1's zero Approved records.
- MCP initialization negotiated protocol `2025-11-25`, returned the five expected tools, and supplied the Git-authority and human-approval instructions.

The synthetic record was preserved as Stale instead of deleted, so the status transition and provenance remain inspectable.

## Privacy-boundary redeploy — 2026-08-03

Machine B proved that the original client copied whole local messages into
`provenance.evidence_excerpt`. The forward guard was deployed before allowing
another field push:

- Worker version: `92707d40-85e6-41d6-ad2d-bdd2ef804ae0`
- `/health`: HTTP 200 with the same version ID and its creation timestamp
- synthetic ingest with `evidence_excerpt`: HTTP 400, rejected as an
  unrecognized provenance key
- synthetic payload: no real session content and no credential-shaped value
- Queue/D1 effect: none; validation rejected the event before enqueue

The deployed Worker now rejects stale clients that send transcript-derived
provenance.

## Existing-data privacy remediation — 2026-08-03

After explicit operator approval, migration
`0002_remove_raw_provenance.sql` was applied remotely with repository-pinned
Wrangler `4.114.0`.

- Pre-migration Time Travel bookmark:
  `00000009-00000000-000050bc-e4c359e6461ea634c09bae36e555f41b`
- Migration scope: remove only `provenance.evidence_excerpt` and write a
  non-content `provenance_redacted` event for each affected memory
- Post-migration D1 aggregate: 18 memories, zero excerpt fields, 17 redaction
  events
- Verification query effect: zero rows written
- R2 reconciliation: 76-byte Approved snapshot, zero memories, zero excerpt
  fields, SHA-256
  `ed230cc686255067114638d382955196d4e8368ee8a10c382c7a1d70c46b3090`

No memory body, transcript text, or excerpt text was emitted during the
verification. The temporary R2 download was removed afterward.

## Recipe-only boundary redeploy — 2026-08-03

Machine B then proved that removing `provenance.evidence_excerpt` was
insufficient: older `voice_signals` payloads also copied transcript-derived
phrases into `memory.title` and `memory.body`. The boundary was corrected rather
than weakened.

- Worker version: `83ad6b76-45ec-43fe-abc2-ba55fbc1750b`
- Git commit: `1e7ce91ff133590df9483312bed0a2b74c0cd3d7`
- CI run: `30823424259`, successful
- live synthetic `voice_signals` ingest: HTTP 400 before enqueue
- synthetic Queue/D1 effect: zero receipts and zero memories
- accepted source contract: `source_table=recipes` with
  `curation_level=manual_recipe`
- local automatic signals: blocked from push and eligible for metadata-only
  quarantine receipts

A read-only aggregate query found 19 older cloud `voice_signals` memories: 3
Candidate and 16 Stale. Migration `0003_keep_voice_signals_local.sql` is
prepared and locally tested to replace their title, body, and project content
with a local-only marker while preserving identifiers and event history. It has
not been applied remotely; that destructive write requires separate approval.

## D1 Time Travel rehearsal — 2026-08-03

The restore gate was rehearsed against a separate remote database, never the
live Worker binding:

- database: `recall-context-recovery-20260803`
- database ID: `c836f0df-3eca-495b-847c-d294992aed88`
- schema: `0001_init.sql`, including FTS5 and its triggers
- pre-mutation bookmark:
  `00000000-0000000e-000050bc-9b8e74abe1e6ce1642bffff36284136f`
- pre-restore bookmark returned for undo:
  `00000000-ffffffff-000050bc-7145b35accff7385bca71c462e7261ab`
- restored fixture count: 1
- post-bookmark fixture count: 0
- restored FTS match count: 1
- verification query effect: zero rows written

This proves Time Travel with the production FTS5 schema. D1 export is not part
of the recovery path and remains unsupported while the database contains a
virtual table.
