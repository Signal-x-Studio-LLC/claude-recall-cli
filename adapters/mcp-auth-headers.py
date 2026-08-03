#!/usr/bin/env python3
"""Emit MCP request headers for clients that support a headers helper."""

import json
import os
import sys


token = os.environ.get("RECALL_CONTEXT_TOKEN", "").strip()
if not token:
    print("RECALL_CONTEXT_TOKEN is required", file=sys.stderr)
    raise SystemExit(1)

print(json.dumps({"Authorization": f"Bearer {token}"}, separators=(",", ":")))

