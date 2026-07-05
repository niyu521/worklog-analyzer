# Artifact Flow Read Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add stable workflow-type grouping and artifact provenance JSON APIs for an independently built dashboard.

**Architecture:** A focused `ingest/flows.py` read-model module classifies unlabeled episodes into persisted `episodes.task_label` values and projects existing event lineage into versioned JSON. `server.py` exposes two read-only routes and leaves matching and collection untouched.

**Tech Stack:** Python 3.13, Flask, SQLite, standard-library `unittest`

## Global Constraints

- Do not modify the matching algorithm or collector event contract.
- Preserve `master_id`, `event_id`, `parent_event_id`, and stored blobs.
- API schema version is exactly `1.0`.
- Pagination limit is 1–200.
- Existing non-empty `task_label` values are never overwritten.

---

### Task 1: Flow classification and projection

**Files:**
- Create: `ingest/flows.py`
- Create: `tests/test_flows.py`

**Interfaces:**
- Consumes: existing `schema.get_conn()` and event rows.
- Produces: `list_flow_types() -> dict` and `get_flow_type(flow_type_id, limit, offset) -> dict | None`.

- [ ] **Step 1: Write failing classification and graph-projection tests**

Create fixtures with invoice and meeting-minute episodes. Assert that empty
labels become `請求書作成` and `議事録作成`, and that parent links project to
`revision` or `cross_platform_continuation`.

- [ ] **Step 2: Run tests and confirm the missing module failure**

Run: `./.venv/bin/python -m unittest tests.test_flows -v`

Expected: `ModuleNotFoundError: No module named 'flows'`.

- [ ] **Step 3: Implement the minimal read model**

Implement deterministic keyword categories, title extraction from
`metadata_json`, safe blob excerpts, stable flow type IDs, aggregate listing,
detail projection, and limit/offset slicing.

- [ ] **Step 4: Run flow tests**

Run: `./.venv/bin/python -m unittest tests.test_flows -v`

Expected: all flow tests pass.

### Task 2: Flask API routes

**Files:**
- Modify: `server.py`
- Create: `tests/test_flow_api.py`

**Interfaces:**
- Consumes: `list_flow_types()` and `get_flow_type()`.
- Produces: `GET /flow-types` and `GET /flow-types/<flow_type_id>`.

- [ ] **Step 1: Write failing endpoint tests**

Use Flask's test client with patched read-model functions. Assert response
schema, 404 for unknown IDs, and 400 for invalid pagination.

- [ ] **Step 2: Run tests and confirm 404 failures**

Run: `./.venv/bin/python -m unittest tests.test_flow_api -v`

Expected: route tests fail because endpoints do not exist.

- [ ] **Step 3: Add the two routes**

Parse integer pagination, enforce `1 <= limit <= 200` and `offset >= 0`, return
the read model as JSON, and preserve all existing routes.

- [ ] **Step 4: Run the full suite**

Run: `./.venv/bin/python -m unittest discover -s tests -v`

Expected: all tests pass.

### Task 3: Deploy, verify, and hand off

**Files:**
- Create: `DASHBOARD_CONNECTION_PROMPT.md`
- Copy deliverables to: `/srv/YC/submit contents/company-brain/`

**Interfaces:**
- Consumes: live SQLite data and the new localhost API.
- Produces: tested VPS endpoints plus a frontend-agent connection prompt.

- [ ] **Step 1: Back up the database and deploy files**

Copy `events.db` to a timestamped backup, sync the tested files, compile Python,
and restart only `company-brain-server.service`.

- [ ] **Step 2: Verify live API and persistence**

Call both endpoints, compare aggregate instance and event counts to SQLite,
restart the server, call again, and verify labels remain stable.

- [ ] **Step 3: Write the dashboard connection prompt**

Document base URL assumptions, exact response fields, loading/empty/error
states, routing behavior, and a prohibition on changing the existing visual
design.

- [ ] **Step 4: Place the submission copy**

Create `/srv/YC/submit contents/company-brain/` and copy the source, API
contract, design, tests, and connection prompt without secrets, runtime caches,
the virtualenv, or production data.
