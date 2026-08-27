-- Remove the transcript-derived evidence_excerpt field accepted by the v1
-- prototype. Preserve a non-content audit receipt before redacting the JSON.

INSERT OR IGNORE INTO memory_events
  (event_id, memory_id, event_type, reason, actor, payload_json)
SELECT
  'privacy_redaction_' || id,
  id,
  'provenance_redacted',
  'Removed transcript-derived evidence_excerpt to enforce the local-only raw evidence boundary.',
  'system:migration-0002',
  json_object(
    'removed_fields', json_array('evidence_excerpt'),
    'removed_excerpt_chars', length(json_extract(provenance_json, '$.evidence_excerpt'))
  )
FROM memories
WHERE json_valid(provenance_json)
  AND json_type(provenance_json, '$.evidence_excerpt') = 'text';

UPDATE memories
SET provenance_json = json_remove(provenance_json, '$.evidence_excerpt'),
    updated_at = datetime('now')
WHERE json_valid(provenance_json)
  AND json_type(provenance_json, '$.evidence_excerpt') = 'text';
