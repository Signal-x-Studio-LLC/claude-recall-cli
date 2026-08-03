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
provenance. Existing D1 data is unchanged: 17 `voice_signals` rows still contain
the old excerpt field. Migration `0002_remove_raw_provenance.sql` is prepared
and locally tested, but applying that destructive redaction remotely requires
separate approval and a recovery receipt.
