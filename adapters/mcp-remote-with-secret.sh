#!/usr/bin/env bash
# Give an MCP client with stdio support access to the authenticated remote Worker.

set -euo pipefail

item="${RECALL_CONTEXT_1PASSWORD_ITEM:-Cloudflare recall-context-plane}"
context_url="${RECALL_CONTEXT_URL:?RECALL_CONTEXT_URL is required}"

exec with-secret "$item" --as RECALL_CONTEXT_TOKEN -- \
  npx -y mcp-remote@0.1.38 "$context_url/mcp" \
  --header 'Authorization: Bearer ${RECALL_CONTEXT_TOKEN}'
