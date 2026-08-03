PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memories (
  id                TEXT PRIMARY KEY,
  stable_key        TEXT NOT NULL UNIQUE,
  kind              TEXT NOT NULL,
  title             TEXT NOT NULL,
  body              TEXT NOT NULL,
  project           TEXT,
  status            TEXT NOT NULL DEFAULT 'Candidate'
                    CHECK(status IN ('Candidate', 'Approved', 'Contradicted', 'Superseded', 'Stale')),
  confidence        REAL NOT NULL DEFAULT 0.5,
  source_client     TEXT NOT NULL,
  source_machine    TEXT NOT NULL,
  source_session_id TEXT,
  source_timestamp  TEXT,
  provenance_json   TEXT NOT NULL,
  related_memory_id TEXT,
  expires_at        TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(related_memory_id) REFERENCES memories(id)
);

CREATE INDEX IF NOT EXISTS idx_memories_status_updated
ON memories(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_project_status
ON memories(project, status);
CREATE INDEX IF NOT EXISTS idx_memories_source_session
ON memories(source_client, source_session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  title,
  body,
  project,
  content='memories',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, title, body, project)
  VALUES (new.rowid, new.title, new.body, new.project);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, title, body, project)
  VALUES ('delete', old.rowid, old.title, old.body, old.project);
  INSERT INTO memories_fts(rowid, title, body, project)
  VALUES (new.rowid, new.title, new.body, new.project);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, title, body, project)
  VALUES ('delete', old.rowid, old.title, old.body, old.project);
END;

CREATE TABLE IF NOT EXISTS memory_events (
  event_id      TEXT PRIMARY KEY,
  memory_id     TEXT NOT NULL,
  event_type    TEXT NOT NULL,
  from_status   TEXT,
  to_status     TEXT,
  reason        TEXT,
  actor         TEXT NOT NULL,
  payload_json  TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(memory_id) REFERENCES memories(id)
);

CREATE INDEX IF NOT EXISTS idx_memory_events_memory_created
ON memory_events(memory_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ingest_receipts (
  event_id       TEXT PRIMARY KEY,
  status         TEXT NOT NULL CHECK(status IN ('queued', 'processed', 'failed')),
  memory_id      TEXT,
  source_client  TEXT,
  session_id     TEXT,
  error          TEXT,
  received_at    TEXT NOT NULL DEFAULT (datetime('now')),
  processed_at   TEXT,
  FOREIGN KEY(memory_id) REFERENCES memories(id)
);

CREATE INDEX IF NOT EXISTS idx_ingest_receipts_session
ON ingest_receipts(source_client, session_id, status);
