"""Dashboard-facing workflow grouping and artifact provenance read model."""

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from schema import get_conn

SCHEMA_VERSION = "1.0"
EXCERPT_LENGTH = 240

CATEGORIES = [
    ("invoice_creation", "請求書作成", ("請求書", "invoice", "billing")),
    ("estimate_creation", "見積書作成", ("見積", "estimate", "quotation")),
    (
        "meeting_minutes_creation",
        "議事録作成",
        ("議事録", "meeting minutes", "minutes of meeting"),
    ),
    ("proposal_creation", "提案書作成", ("提案", "proposal")),
    ("contract_creation", "契約書作成", ("契約", "contract", "agreement")),
    ("report_creation", "報告書作成", ("報告書", "月次報告", "report")),
]
DEFAULT_CATEGORY = ("other_document_creation", "その他の文書作成")
ID_TO_LABEL = {flow_type_id: label for flow_type_id, label, _ in CATEGORIES}
ID_TO_LABEL[DEFAULT_CATEGORY[0]] = DEFAULT_CATEGORY[1]
LABEL_TO_ID = {label: flow_type_id for flow_type_id, label in ID_TO_LABEL.items()}
_WHITESPACE = re.compile(r"\s+")


def _metadata(row) -> dict:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _read_content(row) -> str:
    try:
        return Path(row["content_ref"]).read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeError):
        return ""


def _event_title(row, metadata: dict) -> str:
    for key in ("title", "file_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    native_id = row["native_id"] or ""
    if "/" in native_id:
        return Path(native_id).name
    return native_id or row["platform"]


def _flow_type_id(label: str) -> str:
    known = LABEL_TO_ID.get(label)
    if known:
        return known
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:12]
    return f"custom-{digest}"


def _classify(rows) -> str:
    parts = []
    for row in rows:
        metadata = _metadata(row)
        parts.extend(
            str(metadata.get(key, "")) for key in ("title", "file_name")
        )
        parts.append(row["native_id"] or "")
        parts.append(_read_content(row)[:2000])
    corpus = " ".join(parts).lower()
    for _, label, keywords in CATEGORIES:
        if any(keyword.lower() in corpus for keyword in keywords):
            return label
    return DEFAULT_CATEGORY[1]


def _ensure_labels(conn) -> None:
    masters = conn.execute(
        "SELECT master_id FROM episodes "
        "WHERE task_label IS NULL OR TRIM(task_label) = ''"
    ).fetchall()
    changed = False
    for master in masters:
        rows = conn.execute(
            "SELECT * FROM events WHERE master_id = ? ORDER BY captured_at",
            (master["master_id"],),
        ).fetchall()
        label = _classify(rows)
        conn.execute(
            "UPDATE episodes SET task_label = ? WHERE master_id = ?",
            (label, master["master_id"]),
        )
        changed = True
    if changed:
        conn.commit()


def list_flow_types() -> dict:
    conn = get_conn()
    try:
        _ensure_labels(conn)
        rows = conn.execute(
            """
            SELECT ep.master_id, ep.task_label, ev.platform, ev.captured_at
            FROM episodes ep
            LEFT JOIN events ev ON ev.master_id = ep.master_id
            """
        ).fetchall()
        groups = {}
        for row in rows:
            label = row["task_label"] or DEFAULT_CATEGORY[1]
            group = groups.setdefault(
                label,
                {
                    "flow_type_id": _flow_type_id(label),
                    "label": label,
                    "masters": set(),
                    "event_count": 0,
                    "last_activity": None,
                    "platforms": set(),
                },
            )
            group["masters"].add(row["master_id"])
            if row["captured_at"] is not None:
                group["event_count"] += 1
                if (
                    group["last_activity"] is None
                    or row["captured_at"] > group["last_activity"]
                ):
                    group["last_activity"] = row["captured_at"]
            if row["platform"]:
                group["platforms"].add(row["platform"])
        result = []
        for group in groups.values():
            result.append(
                {
                    "flow_type_id": group["flow_type_id"],
                    "label": group["label"],
                    "instance_count": len(group["masters"]),
                    "event_count": group["event_count"],
                    "last_activity": group["last_activity"],
                    "platforms": sorted(group["platforms"]),
                }
            )
        result.sort(key=lambda item: item["last_activity"] or "", reverse=True)
        return {"schema_version": SCHEMA_VERSION, "flow_types": result}
    finally:
        conn.close()


def _label_for_id(conn, flow_type_id: str):
    known = ID_TO_LABEL.get(flow_type_id)
    if known:
        return known
    labels = conn.execute(
        "SELECT DISTINCT task_label FROM episodes WHERE task_label IS NOT NULL"
    ).fetchall()
    for row in labels:
        if _flow_type_id(row["task_label"]) == flow_type_id:
            return row["task_label"]
    return None


def _node(row) -> dict:
    metadata = _metadata(row)
    content = _WHITESPACE.sub(" ", _read_content(row)).strip()
    return {
        "event_id": row["event_id"],
        "platform": row["platform"],
        "native_id": row["native_id"],
        "event_type": row["event_type"],
        "title": _event_title(row, metadata),
        "captured_at": row["captured_at"],
        "content_excerpt": content[:EXCERPT_LENGTH],
        "metadata": metadata,
    }


def _edge(child, parent) -> dict:
    metadata = _metadata(child)
    if (
        child["platform"] == parent["platform"]
        and child["native_id"] == parent["native_id"]
    ):
        relation, confidence = "revision", 1.0
    elif child["platform"] != parent["platform"]:
        relation = "cross_platform_continuation"
        confidence = float(metadata.get("match_score", 0.5))
    else:
        relation = "derived_copy"
        confidence = float(metadata.get("match_score", 0.5))
    edge = {
        "from": parent["event_id"],
        "to": child["event_id"],
        "relation": relation,
        "confidence": confidence,
    }
    rationale = metadata.get("match_rationale")
    if rationale:
        edge["rationale"] = rationale
    return edge


def _project_instance(master_id: str, rows: list) -> dict:
    ordered = sorted(rows, key=lambda row: row["captured_at"])
    nodes = [_node(row) for row in ordered]
    by_id = {row["event_id"]: row for row in ordered}
    edges = [
        _edge(row, by_id[row["parent_event_id"]])
        for row in ordered
        if row["parent_event_id"] in by_id
    ]
    latest = nodes[-1]
    return {
        "flow_id": master_id,
        "label": latest["title"],
        "started_at": nodes[0]["captured_at"],
        "completed_at": latest["captured_at"],
        "latest_output": {
            key: latest[key]
            for key in ("event_id", "platform", "title", "captured_at")
        },
        "nodes": nodes,
        "edges": edges,
    }


def get_flow_type(flow_type_id: str, limit: int = 100, offset: int = 0):
    conn = get_conn()
    try:
        _ensure_labels(conn)
        label = _label_for_id(conn, flow_type_id)
        if label is None:
            return None
        total = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE task_label = ?", (label,)
        ).fetchone()[0]
        masters = conn.execute(
            """
            SELECT ep.master_id, MAX(ev.captured_at) AS completed_at
            FROM episodes ep
            LEFT JOIN events ev ON ev.master_id = ep.master_id
            WHERE ep.task_label = ?
            GROUP BY ep.master_id
            ORDER BY completed_at DESC
            LIMIT ? OFFSET ?
            """,
            (label, limit, offset),
        ).fetchall()
        instances = []
        for master in masters:
            rows = conn.execute(
                "SELECT * FROM events WHERE master_id = ? ORDER BY captured_at",
                (master["master_id"],),
            ).fetchall()
            if rows:
                instances.append(_project_instance(master["master_id"], rows))
        return {
            "schema_version": SCHEMA_VERSION,
            "flow_type": {
                "flow_type_id": flow_type_id,
                "label": label,
                "instance_count": total,
            },
            "instances": instances,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(instances),
            },
        }
    finally:
        conn.close()
