# Deploy the private Cloudflare context plane

This deploys one private Worker backed by D1, Queues, R2, and an optional AI Gateway normalization step. The Worker URL is public network infrastructure, but every ingest, receipt, snapshot, and MCP request requires the private bearer token. Only `/health` is unauthenticated.

## 1. Install and verify locally

```bash
cd cloudflare
npm install
npm test
npm run check
npx wrangler whoami
```

The repository pins Wrangler. Use that copy rather than a global install.

## 2. Create the Cloudflare resources

```bash
npx wrangler d1 create recall-context-plane
npx wrangler r2 bucket create recall-context-snapshots
npx wrangler queues create recall-context-ingest
npx wrangler queues create recall-context-dead-letter
```

Copy the D1 database ID from the first command into `cloudflare/wrangler.jsonc`. The other names already match that file.

Apply the schema locally first, then remotely:

```bash
npx wrangler d1 migrations apply recall-context-plane --local
npx wrangler d1 migrations apply recall-context-plane --remote
```

## 3. Create the bearer token in 1Password

Check for an existing item before creating one:

```bash
op item list --vault 'Developer Secrets'
```

The expected item is `Cloudflare recall-context-plane`, with the token in the concealed `credential` field. If it does not exist, create it through the 1Password desktop app or an authenticated user CLI session. The shell's service-account session is read-only and cannot create the item.

Install the value as a Worker secret without printing it:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- \
  /bin/zsh -lc 'printf %s "$RECALL_CONTEXT_TOKEN" | npx wrangler secret put CONTEXT_PLANE_TOKEN'
```

Read the 1Password field back and verify `/health` plus one authenticated request before treating the write as complete.

## 4. Deploy

AI normalization is disabled in the initial configuration. This proves the storage, queue, receipt, and MCP path before a model can rewrite candidate text.

```bash
npm run deploy
curl -fsS "https://recall-context-plane.<account>.workers.dev/health"
```

Expected health response:

```json
{"ok":true,"service":"recall-context-plane","version":{"id":"<worker-version-id>","tag":"<tag>","timestamp":"<created-at>"}}
```

Then run one local sync with the secret injected:

```bash
export RECALL_CONTEXT_URL="https://recall-context-plane.<account>.workers.dev"
python3 ../poe-extract.py drain-queue --include-codex --include-gemini
python3 ../context-plane.py baseline
python3 ../context-plane.py sanitize-outbox
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- \
  ../adapters/sync-context-plane.sh
```

Check the outbox:

```bash
python3 ../context-plane.py status
```

An event moves `pending` → `sent` → `acknowledged`. A `sent` item is waiting for a Queue receipt; it is not yet proof that D1 contains the memory.

The outbox status must show `unsafe_payloads: 0` before push. The Worker rejects transcript-derived provenance excerpts even if an older client tries to send one.

## 5. Enable AI Gateway only after the deterministic path passes

Create an AI Gateway named `recall-context` and a dynamic route named `recall-distill`. Configure that route to use an approved model and provider credentials. The Worker sends only already-redacted candidate text and sets `collectLog: false`.

Change this setting in `wrangler.jsonc` and redeploy:

```json
"ENABLE_AI_DISTILLATION": "true"
```

Normalization can change `title`, `body`, `kind`, and `confidence`. It cannot change memory status or invent an approval.

## 6. Recovery and retention

- D1 is primary. Use D1 Time Travel for recovery.
- R2 contains daily replaceable snapshots of Approved records only.
- Ingest receipts expire after 90 days by default.
- Candidate and Approved records become Stale only when they have an explicit `expires_at` date.
- Queue delivery is at least once. Stable IDs and unique constraints make replay safe.
- Raw transcripts remain local and follow the local watermark plus archive-grace policy.
- Migration `0002_remove_raw_provenance.sql` removes the prototype's old `evidence_excerpt` field and writes a non-content audit event. On an existing deployment, record a recovery bookmark and obtain approval before applying that destructive redaction remotely.

Trigger a snapshot after the first Approved record:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- \
  /bin/zsh -lc 'curl -fsS -X POST -H "Authorization: Bearer $RECALL_CONTEXT_TOKEN" "$RECALL_CONTEXT_URL/admin/snapshot"'
```
