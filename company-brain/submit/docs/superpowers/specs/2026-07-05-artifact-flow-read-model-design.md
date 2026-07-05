# Artifact Flow Read Model Design

## Goal

Expose stable, dashboard-ready groupings such as `請求書作成`, while preserving
the event-by-event provenance path of each individual output artifact.

## Scope

The existing matcher, `master_id`, event records, blobs, and collector behavior
remain unchanged. Different deliverables are not forced into one sequential
business process. Each current episode remains one artifact lineage.

## Data model

The existing `episodes.task_label` becomes the persisted workflow-type label.
Labels are assigned deterministically from event titles, file names, native IDs,
and content. Initial categories are:

- `請求書作成` (`invoice_creation`)
- `見積書作成` (`estimate_creation`)
- `議事録作成` (`meeting_minutes_creation`)
- `提案書作成` (`proposal_creation`)
- `契約書作成` (`contract_creation`)
- `報告書作成` (`report_creation`)
- `その他の文書作成` (`other_document_creation`)

An episode is exposed as a `flow_instance`. Its events are `nodes`; stored
`parent_event_id` links become `edges`. Edge relations are projected as
`revision`, `cross_platform_continuation`, or `derived_copy`.

## API contract

`GET /flow-types` returns:

```json
{
  "schema_version": "1.0",
  "flow_types": [{
    "flow_type_id": "invoice_creation",
    "label": "請求書作成",
    "instance_count": 12,
    "event_count": 31,
    "last_activity": "2026-07-05T08:30:00Z",
    "platforms": ["notion", "claude_code", "google_docs"]
  }]
}
```

`GET /flow-types/<flow_type_id>?limit=100&offset=0` returns:

```json
{
  "schema_version": "1.0",
  "flow_type": {
    "flow_type_id": "invoice_creation",
    "label": "請求書作成",
    "instance_count": 12
  },
  "instances": [{
    "flow_id": "master-id",
    "label": "A社 2026年7月請求書",
    "started_at": "2026-07-01T09:00:00Z",
    "completed_at": "2026-07-05T08:30:00Z",
    "latest_output": {
      "event_id": "event-id",
      "platform": "google_docs",
      "title": "A社請求書",
      "captured_at": "2026-07-05T08:30:00Z"
    },
    "nodes": [],
    "edges": []
  }],
  "pagination": {"limit": 100, "offset": 0, "returned": 1}
}
```

Each node contains `event_id`, `platform`, `native_id`, `event_type`, `title`,
`captured_at`, `content_excerpt`, and collector metadata. Each edge contains
`from`, `to`, `relation`, `confidence`, and optional `rationale`.

## Classification and refresh

API reads classify only episodes whose `task_label` is empty, then persist the
result. This covers collectors that still write directly to the matcher without
changing the single-writer ingestion design. Existing non-empty labels are
never overwritten automatically.

## Failure behavior

Unknown flow type IDs return HTTP 404. `limit` is constrained to 1–200 and
invalid pagination returns HTTP 400. Missing blobs produce an empty excerpt
without breaking the response. Unclassifiable content is retained under
`その他の文書作成`.

## Verification

Unit tests cover classification, stable grouping, instance labels, edge
relations, pagination, and missing blobs. Flask tests cover both endpoints and
error responses. Deployment verification checks schema version, counts against
SQLite, service health, and no regressions in the existing collector tests.
