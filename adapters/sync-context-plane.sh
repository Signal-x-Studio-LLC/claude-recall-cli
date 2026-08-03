#!/usr/bin/env bash
# Drain local transcripts, stage redacted candidates, and advance cloud receipts.

set -euo pipefail

recall_dir="${RECALL_CLI_DIR:-$HOME/Workspace/dev/tools/claude-recall-cli}"

python3 "$recall_dir/poe-extract.py" drain-queue --include-codex --include-gemini
python3 "$recall_dir/context-plane.py" stage

# A missed receipt check is harmless: sent events stay in the durable outbox.
python3 "$recall_dir/context-plane.py" verify || true
python3 "$recall_dir/context-plane.py" push

