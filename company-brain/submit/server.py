"""Thin HTTP ingestion API. Collectors (Notion/Discord/Drive/Claude Code
adapters, built separately) POST raw events here; this process owns the
event store and matching logic."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))

from flask import Flask, jsonify, request  # noqa: E402

from flows import get_flow_type, list_flow_types  # noqa: E402
from matcher import ingest_raw_event  # noqa: E402
from schema import init_db  # noqa: E402
from tree import build_tree, list_episodes  # noqa: E402

app = Flask(__name__)

REQUIRED_FIELDS = {"platform", "native_id", "content", "event_type", "captured_at"}


@app.route("/ingest", methods=["POST"])
def ingest():
    body = request.get_json(force=True, silent=True) or {}
    missing = REQUIRED_FIELDS - body.keys()
    if missing:
        return jsonify({"error": f"missing fields: {sorted(missing)}"}), 400
    result = ingest_raw_event(
        platform=body["platform"],
        native_id=body["native_id"],
        content=body["content"],
        event_type=body["event_type"],
        captured_at=body["captured_at"],
        metadata=body.get("metadata"),
    )
    return jsonify(result)


@app.route("/episodes", methods=["GET"])
def episodes():
    return jsonify(list_episodes())


@app.route("/tree/<master_id>", methods=["GET"])
def tree(master_id):
    return jsonify(build_tree(master_id))


@app.route("/flow-types", methods=["GET"])
def flow_types():
    return jsonify(list_flow_types())


@app.route("/flow-types/<flow_type_id>", methods=["GET"])
def flow_type_detail(flow_type_id):
    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return jsonify({"error": "invalid pagination: integers required"}), 400
    if not 1 <= limit <= 200 or offset < 0:
        return jsonify(
            {"error": "invalid pagination: limit must be 1-200 and offset >= 0"}
        ), 400
    result = get_flow_type(flow_type_id, limit, offset)
    if result is None:
        return jsonify({"error": "flow type not found"}), 404
    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    # All HTTP collectors share this one writer. Keep the development server
    # explicitly single-threaded so matching and SQLite writes are serialized.
    app.run(host="127.0.0.1", port=8420, threaded=False)
