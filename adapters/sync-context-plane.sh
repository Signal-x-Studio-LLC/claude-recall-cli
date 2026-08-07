#!/usr/bin/env bash
# Drain local transcripts, stage redacted candidates, and advance cloud receipts.

set -euo pipefail

recall_dir="${RECALL_CLI_DIR:-$HOME/Workspace/dev/tools/claude-recall-cli}"

# A wedged pipeline emits no error, so record which step died. Without this the
# only symptom is a log line that quietly stops appearing.
step="init"
trap 'python3 "$recall_dir/context-plane.py" heartbeat --failed-step "$step" >/dev/null 2>&1 || true' ERR

step="drain-queue"
python3 "$recall_dir/poe-extract.py" drain-queue --include-codex --include-gemini

# Non-fatal: sanitize exits 2 while an unsafe payload remains, but the only way to
# clear a *sent* unsafe row is the verify step below. push has its own identical
# guard, so failing the script here just wedges the pipeline before verify runs.
step="sanitize-outbox"
python3 "$recall_dir/context-plane.py" sanitize-outbox || true

step="stage"
python3 "$recall_dir/context-plane.py" stage

# A missed receipt check is harmless: sent events stay in the durable outbox.
step="verify"
python3 "$recall_dir/context-plane.py" verify || true

step="push"
python3 "$recall_dir/context-plane.py" push

# Scoring a session means reading its whole transcript, which is why this runs
# here and not in the SessionEnd hook — that one is deliberately sub-50ms.
step="nominate"
# Threshold 30 is measured, not guessed: over 153 sessions in 3 days the scorer
# peaked at 39 with a median of 7, so anything above ~40 nominates nothing ever.
python3 "$recall_dir/recall-scan.py" --days 3 --min-score 30 --limit 5 \
  --write-nominations >/dev/null || true

step="heartbeat"
python3 "$recall_dir/context-plane.py" heartbeat || true
