import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))

import flows


DDL = """
CREATE TABLE episodes (
    master_id TEXT PRIMARY KEY,
    task_label TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    master_id TEXT NOT NULL,
    parent_event_id TEXT,
    platform TEXT NOT NULL,
    native_id TEXT,
    event_type TEXT NOT NULL,
    content_hash TEXT,
    content_ref TEXT,
    metadata_json TEXT,
    captured_at TEXT NOT NULL
);
"""


class FlowReadModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "events.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(DDL)
        conn.close()
        self.get_conn_patch = patch.object(flows, "get_conn", self.get_conn)
        self.get_conn_patch.start()

    def tearDown(self):
        self.get_conn_patch.stop()
        self.temp.cleanup()

    def get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def add_episode(self, master_id, events, task_label=None):
        conn = self.get_conn()
        conn.execute(
            "INSERT INTO episodes VALUES (?, ?, ?)",
            (master_id, task_label, events[0]["captured_at"]),
        )
        for event in events:
            content_path = self.root / f"{event['event_id']}.txt"
            if event.get("content") is not None:
                content_path.write_text(event["content"], encoding="utf-8")
            conn.execute(
                """
                INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    master_id,
                    event.get("parent_event_id"),
                    event["platform"],
                    event["native_id"],
                    event.get("event_type", "revision"),
                    str(content_path),
                    json.dumps(event.get("metadata", {}), ensure_ascii=False),
                    event["captured_at"],
                ),
            )
        conn.commit()
        conn.close()

    def test_unlabeled_episodes_are_classified_and_persisted(self):
        self.add_episode(
            "invoice-flow",
            [{
                "event_id": "invoice-1",
                "platform": "notion",
                "native_id": "notion-page",
                "captured_at": "2026-07-01T09:00:00Z",
                "content": "株式会社A 7月分 請求書 金額100,000円",
                "metadata": {"title": "A社請求情報"},
            }],
        )
        self.add_episode(
            "minutes-flow",
            [{
                "event_id": "minutes-1",
                "platform": "notion",
                "native_id": "meeting-page",
                "captured_at": "2026-07-02T09:00:00Z",
                "content": "定例会議 議事録 決定事項",
                "metadata": {"title": "開発定例"},
            }],
        )

        result = flows.list_flow_types()

        labels = {item["label"] for item in result["flow_types"]}
        self.assertEqual(labels, {"請求書作成", "議事録作成"})
        self.assertEqual(result["schema_version"], "1.0")
        conn = self.get_conn()
        persisted = dict(conn.execute(
            "SELECT master_id, task_label FROM episodes"
        ).fetchall())
        conn.close()
        self.assertEqual(persisted["invoice-flow"], "請求書作成")
        self.assertEqual(persisted["minutes-flow"], "議事録作成")

    def test_detail_projects_nodes_edges_and_latest_output(self):
        self.add_episode(
            "invoice-flow",
            [
                {
                    "event_id": "draft",
                    "platform": "notion",
                    "native_id": "notion-page",
                    "captured_at": "2026-07-01T09:00:00Z",
                    "content": "A社 請求書 下書き",
                    "metadata": {"title": "A社請求書"},
                },
                {
                    "event_id": "final",
                    "parent_event_id": "draft",
                    "platform": "google_docs",
                    "native_id": "google-file",
                    "captured_at": "2026-07-02T09:00:00Z",
                    "content": "A社 請求書 確定版",
                    "metadata": {
                        "file_name": "A社 7月請求書",
                        "match_score": 0.94,
                        "match_rationale": "same recipient and amount",
                    },
                },
            ],
            task_label="請求書作成",
        )

        result = flows.get_flow_type("invoice_creation", limit=100, offset=0)

        self.assertEqual(result["flow_type"]["label"], "請求書作成")
        self.assertEqual(len(result["instances"]), 1)
        instance = result["instances"][0]
        self.assertEqual(instance["flow_id"], "invoice-flow")
        self.assertEqual(instance["label"], "A社 7月請求書")
        self.assertEqual(instance["latest_output"]["event_id"], "final")
        self.assertEqual(len(instance["nodes"]), 2)
        self.assertEqual(instance["edges"], [{
            "from": "draft",
            "to": "final",
            "relation": "cross_platform_continuation",
            "confidence": 0.94,
            "rationale": "same recipient and amount",
        }])

    def test_same_native_id_projects_revision_edge(self):
        self.add_episode(
            "report-flow",
            [
                {
                    "event_id": "v1",
                    "platform": "google_docs",
                    "native_id": "file-1",
                    "captured_at": "2026-07-01T09:00:00Z",
                    "content": "月次報告 初版",
                },
                {
                    "event_id": "v2",
                    "parent_event_id": "v1",
                    "platform": "google_docs",
                    "native_id": "file-1",
                    "captured_at": "2026-07-02T09:00:00Z",
                    "content": "月次報告 改訂版",
                },
            ],
            task_label="報告書作成",
        )

        result = flows.get_flow_type("report_creation", 100, 0)

        self.assertEqual(result["instances"][0]["edges"][0]["relation"], "revision")
        self.assertEqual(result["instances"][0]["edges"][0]["confidence"], 1.0)

    def test_missing_blob_returns_empty_excerpt(self):
        self.add_episode(
            "missing-flow",
            [{
                "event_id": "missing",
                "platform": "notion",
                "native_id": "missing-page",
                "captured_at": "2026-07-01T09:00:00Z",
                "content": None,
            }],
            task_label="その他の文書作成",
        )

        result = flows.get_flow_type("other_document_creation", 100, 0)

        self.assertEqual(
            result["instances"][0]["nodes"][0]["content_excerpt"], ""
        )

    def test_pagination_is_applied_after_recency_sort(self):
        for index in range(3):
            self.add_episode(
                f"flow-{index}",
                [{
                    "event_id": f"event-{index}",
                    "platform": "notion",
                    "native_id": f"page-{index}",
                    "captured_at": f"2026-07-0{index + 1}T09:00:00Z",
                    "content": "請求書",
                }],
                task_label="請求書作成",
            )

        result = flows.get_flow_type("invoice_creation", limit=1, offset=1)

        self.assertEqual(result["instances"][0]["flow_id"], "flow-1")
        self.assertEqual(
            result["pagination"], {"limit": 1, "offset": 1, "returned": 1}
        )


if __name__ == "__main__":
    unittest.main()
