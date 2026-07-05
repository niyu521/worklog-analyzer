# Company Brain Flow API Contract

The dashboard consumes a versioned read-only API. Company Brain listens only
on the VPS loopback interface:

```text
http://127.0.0.1:8420
```

Browser code should not call this address directly. A dashboard running on the
same VPS should proxy it through its own server/API routes.

## List workflow groups

```http
GET /flow-types
```

```json
{
  "schema_version": "1.0",
  "flow_types": [
    {
      "flow_type_id": "invoice_creation",
      "label": "請求書作成",
      "instance_count": 18,
      "event_count": 20,
      "last_activity": "2026-07-03T00:54:24.133247",
      "platforms": ["local_pdf"]
    }
  ]
}
```

Use `flow_type_id` as the stable route/key. Display `label`.

## Get one workflow group

```http
GET /flow-types/invoice_creation?limit=100&offset=0
```

`limit` must be 1–200 and `offset` must be zero or greater.

```json
{
  "schema_version": "1.0",
  "flow_type": {
    "flow_type_id": "invoice_creation",
    "label": "請求書作成",
    "instance_count": 18
  },
  "instances": [
    {
      "flow_id": "stable-master-id",
      "label": "A社 7月請求書",
      "started_at": "2026-07-01T09:00:00Z",
      "completed_at": "2026-07-05T08:30:00Z",
      "latest_output": {
        "event_id": "event-id",
        "platform": "google_docs",
        "title": "A社 7月請求書",
        "captured_at": "2026-07-05T08:30:00Z"
      },
      "nodes": [
        {
          "event_id": "event-id",
          "platform": "notion",
          "native_id": "notion-page-id",
          "event_type": "revision",
          "title": "A社請求情報",
          "captured_at": "2026-07-01T09:00:00Z",
          "content_excerpt": "株式会社A 7月分…",
          "metadata": {}
        }
      ],
      "edges": [
        {
          "from": "parent-event-id",
          "to": "child-event-id",
          "relation": "cross_platform_continuation",
          "confidence": 0.94,
          "rationale": "same recipient and amount"
        }
      ]
    }
  ],
  "pagination": {
    "limit": 100,
    "offset": 0,
    "returned": 1
  }
}
```

Relations:

- `revision`: same platform and same native artifact
- `cross_platform_continuation`: artifact continued on another platform
- `derived_copy`: different native artifact on the same platform

## Errors

- Unknown `flow_type_id`: `404 {"error":"flow type not found"}`
- Invalid pagination: `400 {"error":"invalid pagination: ..."}`

The schema is additive within version `1.0`. Clients must ignore unknown fields.
