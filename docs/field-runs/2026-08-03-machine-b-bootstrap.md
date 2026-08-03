# Machine B bootstrap audit — 2026-08-03

Status: **audit only**. Machine B was not configured as an ingest client during this run.

## What the audit established

Machine B began without a local clone, recall command, or `recall.db`. Its home directory also differed from Machine A. Any handoff built around `/Users/nino/...` was therefore unusable. The field runbook now uses `$HOME` in commands and repository-relative links in documentation.

The audit checked two separate repositories:

| Repository | Pull request at audit time | Audited head | State |
|---|---|---|---|
| `nino-chavez/claude-recall-cli` | [#2](https://github.com/nino-chavez/claude-recall-cli/pull/2) | `0c04827c608b…` | Draft, mergeable |
| `nino-chavez/nc-demos` | [#2](https://github.com/nino-chavez/nc-demos/pull/2) | `31a937c76fb…` | Draft, mergeable |

The repeated pull-request number is repository-local. Future receipts name the repository beside the number.

## What the first test receipt did not prove

Pytest was not installed. The five Python test files were invoked directly and returned zero, but two of them—`test_cloudflare_schema.py` and `test_cross_client_transcripts.py`—only define pytest functions. Running those files as scripts does not call those functions. Their silent exit was therefore not a test pass.

The other three files contain direct runners and did execute assertions. That is useful partial evidence, not a complete suite receipt.

GitGuardian was the only automated check on the context-plane pull request at audit time. A passing secret scan proves that the scanner found no exposed secret under its rules. It does not prove parser, outbox, state, retry, schema, MCP, or Worker behavior.

## Correction

The branch now adds `.github/workflows/ci.yml`, following GitHub's documented `setup-python`, `setup-node`, `pytest`, and `npm ci` pattern. CI runs:

- all five Python test files through pytest on Python 3.11 and 3.14
- Cloudflare Worker tests through Vitest
- the Worker's TypeScript check

This audit does not satisfy any multi-machine ingest requirement. Machine B still needs its own durable outbox, three connected harnesses, real processed candidates, storage baseline, and retention report.
