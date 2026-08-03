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

The outbox status must show `unsafe_payloads: 0` before push. The Worker rejects
automatic voice signals and transcript-derived provenance excerpts even if an
older client tries to send them.

## 5. Enable AI Gateway only after the deterministic path passes

Create an AI Gateway named `recall-context` and a dynamic route named `recall-distill`. Configure that route to use an approved model and provider credentials. The Worker sends only explicitly curated recipe text and sets `collectLog: false`.

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
- Migration `0003_keep_voice_signals_local.sql` replaces older cloud voice-signal content with a local-only marker and writes a non-content audit event. Record a recovery bookmark and obtain approval before applying it remotely.
- Rehearse Time Travel against a separate remote D1 database built from the same schema. Cloudflare restores a database in place and does not currently clone a production bookmark into another database, so the rehearsal proves the restore mechanism without touching live data.

### Time Travel rehearsal

Use a uniquely named remote database. Do not bind it to the Worker and do not
substitute the live database name in any command:

```bash
rehearsal_db="recall-context-recovery-$(date -u +%Y%m%d%H%M%S)"
npx wrangler d1 create "$rehearsal_db"
npx wrangler d1 execute "$rehearsal_db" --remote --file migrations/0001_init.sql --yes
npx wrangler d1 execute "$rehearsal_db" --remote --yes --command \
  "INSERT INTO memories (id, stable_key, kind, title, body, status, confidence, source_client, source_machine, provenance_json) VALUES ('mem_recovery_test', 'recovery:test', 'recipe', 'Recovery fixture', 'before restore', 'Candidate', 1, 'codex', 'rehearsal', '{\"source_table\":\"recipes\",\"source_row_id\":\"fixture\",\"source_client\":\"codex\",\"source_machine\":\"rehearsal\",\"curation_level\":\"manual_recipe\"}');"
bookmark="$(npx wrangler d1 time-travel info "$rehearsal_db" --json | jq -r '.bookmark')"
test -n "$bookmark" && test "$bookmark" != "null"
printf 'pre-mutation bookmark: %s\n' "$bookmark"
```

Record the printed bookmark, mutate only the rehearsal row, then restore the
same rehearsal database to that bookmark:

```bash
npx wrangler d1 execute "$rehearsal_db" --remote --yes --command \
  "UPDATE memories SET body = 'after bookmark' WHERE id = 'mem_recovery_test';"
npx wrangler d1 time-travel restore "$rehearsal_db" --bookmark="$bookmark"
npx wrangler d1 execute "$rehearsal_db" --remote --json --command \
  "SELECT COUNT(*) AS restored FROM memories WHERE id = 'mem_recovery_test' AND body = 'before restore'; SELECT COUNT(*) AS post_bookmark_value FROM memories WHERE id = 'mem_recovery_test' AND body = 'after bookmark';"
```

A valid receipt reports `restored: 1` and `post_bookmark_value: 0`, plus the
bookmark Wrangler returns for undoing the restore. Keep or delete the rehearsal
database only under the operator's normal Cloudflare resource policy.

Trigger a snapshot after the first Approved record:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- \
  /bin/zsh -lc 'curl -fsS -X POST -H "Authorization: Bearer $RECALL_CONTEXT_TOKEN" "$RECALL_CONTEXT_URL/admin/snapshot"'
```
