# Connect Claude Code, Codex, and Gemini CLI

Do this after the Worker is deployed. Every client needs the same Worker URL and the same bearer token. Keep the token in 1Password; do not paste it into a checked-in config file.

## Shared environment

Set the non-secret values in your shell or dotfiles:

```bash
export RECALL_CLI_DIR="$HOME/Workspace/dev/tools/claude-recall-cli"
export RECALL_CONTEXT_URL="https://recall-context-plane.<account>.workers.dev"
export RECALL_CONTEXT_1PASSWORD_ITEM="Cloudflare recall-context-plane"
```

Use the existing 1Password wrapper whenever a process needs the token:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- <command>
```

## Session ingestion

Add the same cheap `SessionEnd` command to all three harnesses:

```bash
python3 ~/Workspace/dev/tools/claude-recall-cli/poe-extract.py enqueue
```

Claude Code and Codex hook examples already use this command. For Gemini, merge `adapters/gemini-settings.json` into `~/.gemini/settings.json`. Gemini supplies `session_id`, `transcript_path`, and `cwd` on stdin; its hook is best effort, so the scheduled catch-up sweep remains necessary.

Run the local worker on a schedule through 1Password:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- \
  "$RECALL_CLI_DIR/adapters/sync-context-plane.sh"
```

That command drains queued Claude, Codex, and Gemini sessions, stages redacted candidates, verifies prior receipts, and pushes the next pending batch. An offline push leaves the batch in SQLite for the next run.

## Codex MCP

Merge `adapters/codex-config.toml` into `~/.codex/config.toml`, replace its Worker URL, then start Codex with the token available:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- codex
```

The adapter uses Codex's native Streamable HTTP transport and `bearer_token_env_var`. Its `writes` approval mode prompts for `set_memory_status` because that tool changes reviewed state.

## Claude Code MCP

Merge `adapters/claude-mcp.json` into an MCP configuration and start Claude Code with the token available:

```bash
with-secret 'Cloudflare recall-context-plane' \
  --as RECALL_CONTEXT_TOKEN -- claude
```

The configuration uses Claude Code's environment expansion for the URL and authorization header. Keep the server at user scope if it should be available in every repository. Do not mark the server or its write tool as pre-approved.

For a desktop-launched session that does not inherit the token, use Claude Code's `headersHelper` with the supplied 1Password adapter:

```json
{
  "type": "http",
  "url": "https://recall-context-plane.<account>.workers.dev/mcp",
  "headersHelper": "with-secret 'Cloudflare recall-context-plane' --as RECALL_CONTEXT_TOKEN -- python3 /absolute/path/to/adapters/mcp-auth-headers.py"
}
```

The helper runs on connection and writes only the JSON header object that Claude Code expects.

## Gemini CLI MCP

Gemini's documented environment expansion applies to the `env` block, not to native HTTP headers. The supplied adapter therefore launches the pinned `mcp-remote` bridge through 1Password:

```json
{
  "command": "/bin/zsh",
  "args": ["-lc", "exec \"$RECALL_CLI_DIR/adapters/mcp-remote-with-secret.sh\""],
  "env": {
    "RECALL_CLI_DIR": "$RECALL_CLI_DIR",
    "RECALL_CONTEXT_URL": "$RECALL_CONTEXT_URL",
    "RECALL_CONTEXT_1PASSWORD_ITEM": "$RECALL_CONTEXT_1PASSWORD_ITEM"
  },
  "trust": false
}
```

`trust: false` preserves tool confirmation. The bridge receives the bearer value in its environment; the token is not stored in Gemini settings.

## Verify each client

For each harness, use its MCP status command and then run these checks:

1. `search_memory` returns only Approved records by default.
2. `list_memory_candidates` can see Candidate records but labels them as unreviewed.
3. `set_memory_status` asks for approval and rejects a missing reason.
4. A search for a Contradicted, Superseded, or Stale item returns nothing unless that status is requested explicitly.
5. `get_memory` shows source provenance and the status-change event trail.
