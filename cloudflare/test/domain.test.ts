import { describe, expect, it } from "vitest";

import {
  CandidateEventSchema,
  deterministicDistillation,
  ftsQuery,
  parseModelDistillation,
} from "../src/domain";

const event = CandidateEventSchema.parse({
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
    source_client: "gemini",
    source_machine: "test-mac",
    session_id: "s1",
    source_timestamp: "2026-08-02T20:00:00Z",
  },
});

describe("context plane domain", () => {
  it("rejects raw transcript fields", () => {
    expect(() =>
      CandidateEventSchema.parse({ ...event, transcript: "raw transcript" }),
    ).toThrow();
  });

  it("rejects credential-shaped values at the server boundary", () => {
    expect(() =>
      CandidateEventSchema.parse({
        ...event,
        memory: {
          ...event.memory,
          body: "Use sk-proj-12345678901234567890123456789012",
        },
      }),
    ).toThrow(/credential-shaped/);
  });

  it("never changes candidate state during distillation", () => {
    const fallback = deterministicDistillation(event);
    expect(
      parseModelDistillation(
        JSON.stringify({ ...fallback, status: "Approved" }),
        fallback,
      ),
    ).toEqual(fallback);
  });

  it("falls back when a model invents a credential-shaped value", () => {
    const fallback = deterministicDistillation(event);
    expect(
      parseModelDistillation(
        JSON.stringify({
          ...fallback,
          body: "Use sk-proj-12345678901234567890123456789012",
        }),
        fallback,
      ),
    ).toEqual(fallback);
  });

  it("builds a bounded literal FTS query", () => {
    expect(ftsQuery("canonical adapter OR delete*")).toBe(
      '"canonical" AND "adapter" AND "OR" AND "delete"',
    );
  });
});
