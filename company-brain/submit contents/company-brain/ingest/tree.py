"""Read-side: turn stored events into a lineage tree (樹形図) for one episode."""
import json

from schema import get_conn


def list_episodes() -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT e.master_id, e.task_label, e.created_at,
                   COUNT(ev.event_id) AS event_count,
                   MAX(ev.captured_at) AS last_activity
            FROM episodes e
            LEFT JOIN events ev ON ev.master_id = e.master_id
            GROUP BY e.master_id
            ORDER BY last_activity DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def build_tree(master_id: str) -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM events WHERE master_id = ? ORDER BY captured_at ASC",
            (master_id,),
        ).fetchall()
        nodes = []
        for r in rows:
            node = dict(r)
            try:
                node["metadata"] = json.loads(node.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                node["metadata"] = {}
            nodes.append(node)

        edges = [
            {"from": n["parent_event_id"], "to": n["event_id"]}
            for n in nodes
            if n["parent_event_id"]
        ]
        # roots = nodes with no parent (should normally be exactly one per
        # master_id, but the identity-break path can, in principle, leave a
        # dangling case worth surfacing rather than hiding)
        roots = [n["event_id"] for n in nodes if not n["parent_event_id"]]

        return {"master_id": master_id, "nodes": nodes, "edges": edges, "roots": roots}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python tree.py <master_id>", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(build_tree(sys.argv[1]), ensure_ascii=False, indent=2))
