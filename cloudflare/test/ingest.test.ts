import { describe, expect, it } from "vitest";

import { handleIngest, type Env } from "../src/index";

const candidate = {
  event_id: "evt_0123456789abcdef0123456789abcdef",
  schema_version: 1,
  event_type: "memory_candidate",
  occurred_at: "2026-08-02T20:00:00Z",
  memory: {
    stable_key: "voice_signals:1",
    kind: "correction",
    title: "Use the canonical adapter",
    body: "Use the canonical adapter instead of another wrapper.",
    project: "tools/recall",
    status: "Candidate",
    confidence: 0.55,
  },
  provenance: {
    source_table: "voice_signals",
    source_row_id: "1",
    source_client: "codex",
    source_machine: "test-mac",
    session_id: "s1",
    source_timestamp: "2026-08-02T20:00:00Z",
  },
};

function fakeEnv(receiptStatus?: string) {
  const queued: unknown[] = [];
  const db = {
    prepare(sql: string) {
      return {
        bind(..._values: unknown[]) {
          return {
            async first() {
              return sql.includes("SELECT status") && receiptStatus
                ? { status: receiptStatus }
                : null;
            },
            async run() {
              return { success: true };
            },
          };
        },
      };
    },
  };
  const env = {
    DB: db,
    INGEST_QUEUE: {
      async send(value: unknown) {
        queued.push(value);
      },
    },
    CONTEXT_PLANE_TOKEN: "correct-token",
  } as unknown as Env;
  return { env, queued };
}

function request(token: string, body: unknown): Request {
  return new Request("https://context.example.test/ingest", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

describe("ingest boundary", () => {
  it("rejects an invalid bearer before reading the payload", async () => {
    const { env, queued } = fakeEnv();
    const response = await handleIngest(
      request("wrong-token", { events: [candidate] }),
      env,
    );
    expect(response.status).toBe(401);
    expect(queued).toHaveLength(0);
  });

  it("rejects transcript-shaped extra fields", async () => {
    const { env, queued } = fakeEnv();
    const response = await handleIngest(
      request("correct-token", {
        events: [{ ...candidate, transcript: "raw transcript must stay local" }],
      }),
      env,
    );
    expect(response.status).toBe(400);
    expect(queued).toHaveLength(0);
  });

  it("does not requeue an event with a processed receipt", async () => {
    const { env, queued } = fakeEnv("processed");
    const response = await handleIngest(
      request("correct-token", { events: [candidate] }),
      env,
    );
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ accepted: 0, already_processed: 1 });
    expect(queued).toHaveLength(0);
  });
});
