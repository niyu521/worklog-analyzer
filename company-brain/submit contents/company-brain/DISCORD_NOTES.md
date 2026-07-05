# Discord collector notes

Discord collection is intentionally not implemented until a dedicated bot
token is available.

The future collector should follow the same boundary as the Google and Claude
Code collectors: read-only Discord access, normalize one message/edit into a
raw event, and send it to `POST /ingest`. It must never import the matcher or
write SQLite directly.

Recommended event mapping:

- `platform`: `discord`
- `native_id`: stable `channel_id/message_id`
- `event_type`: `message` for the initial message, `revision` for edits
- `captured_at`: Discord's UTC message/edit timestamp
- `content`: message text plus explicit attachment names/URLs, without
  downloading untrusted attachments in v1
- metadata: guild, channel, thread, author ID, reply target, attachment
  descriptors, and bot/webhook flags

Use gateway events when continuous privileged bot access is approved. Keep a
REST backfill cursor per channel for recovery, checkpoint only after a
successful `/ingest`, retry with a 180-second HTTP timeout, honor Discord rate
limit headers, and exclude messages emitted by the collector bot itself.

Secrets belong only in `/srv/company-brain/.env`; do not commit the token.
