# Machine B partial field run — 2026-08-03

Status: **partial, stopped before push**. Machine B staged local candidates but sent nothing to D1, R2, or the Queue.

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

The first CI run also found a portability defect that Machine A's local run could
not expose: project-label normalization compared recorded transcript paths only
with the ingest machine's current home directory. On a GitHub runner, three
cross-client tests therefore retained `/Users/nino/...` instead of producing
portable `tools/...` and `apps/...` labels. The branch now normalizes canonical
`Workspace` paths independently of the ingest machine's home and includes a
`/Users/nino.chavez/...` regression case.

The initial bootstrap audit did not satisfy any multi-machine ingest requirement. The partial run below supersedes that limited status, but it still does not satisfy the release gate.

## Partial-run follow-up

Machine B later completed the local portion of the run at `c8f66df` and posted
the canonical receipts to [issue #3](https://github.com/nino-chavez/claude-recall-cli/issues/3):

- [partial receipts](https://github.com/nino-chavez/claude-recall-cli/issues/3#issuecomment-5166946365)
- [seven blockers](https://github.com/nino-chavez/claude-recall-cli/issues/3#issuecomment-5166962203)

The run proved cross-machine label portability, three-client local catch-up,
durable staging, authentication rejection, credential-shaped payload rejection,
and per-artifact byte counts. It stopped with 94 pending local candidates after
proving that every staged `provenance.evidence_excerpt` was byte-identical to
the local raw message. Nothing was pushed.

The remaining findings were a fresh-database baseline crash when `recipes` was
absent, a missing-archive exception in the retention report, a swallowed stack
rebuild failure, sparse real Gemini signals, the `chats` Gemini fallback label,
the Machine-A-specific encoded-path fallback, and the lack of a self-serve
Worker version in `/health`. Human review and the remaining multi-machine gates
were deliberately left open.
