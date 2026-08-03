-- Automatically mined voice signals are transcript-derived evidence and stay
-- local. Preserve their identifiers and state history, but remove cloud content
-- staged by older clients before this boundary was enforced.

INSERT OR IGNORE INTO memory_events
  (event_id, memory_id, event_type, from_status, to_status, reason, actor, payload_json)
SELECT
  'privacy_local_only_' || id,
  id,
  'content_redacted',
  status,
  'Stale',
  'Removed automatically mined voice-signal content; only manually promoted recipes may sync.',
  'system:migration-0003',
  json_object('source_table', 'voice_signals', 'removed_fields', json_array('title', 'body', 'project'))
FROM memories
WHERE json_valid(provenance_json)
  AND json_extract(provenance_json, '$.source_table') = 'voice_signals';

UPDATE memories
SET title = 'Local-only voice signal removed',
    body = 'Automatic transcript-derived evidence stays local. Promote a curated recipe to sync a durable lesson.',
    project = NULL,
    status = 'Stale',
    confidence = 0,
    updated_at = datetime('now')
WHERE json_valid(provenance_json)
  AND json_extract(provenance_json, '$.source_table') = 'voice_signals';
