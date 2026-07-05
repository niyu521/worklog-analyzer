#!/usr/bin/env python3
"""Collect successful Claude Code Write/Edit/NotebookEdit operations from JSONL."""

import argparse
import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_ROOT = Path(
    os.getenv("CLAUDE_PROJECTS_ROOT", str(Path.home() / ".claude" / "projects"))
)
CACHE_FILE = Path(__file__).resolve().parent / ".ccwatch_cache.json"
INGEST_URL = os.getenv("COMPANY_BRAIN_INGEST_URL", "http://127.0.0.1:8420/ingest")
WATCHED_TOOLS = {"Write", "Edit", "NotebookEdit"}
EXCLUDED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".pdf", ".zip", ".gz", ".tar", ".pyc",
}
PENDING_TTL_SECONDS = 24 * 60 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_cache() -> dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        data.setdefault("version", 1)
        data.setdefault("files", {})
        return data
    except FileNotFoundError:
        return {"version": 1, "files": {}}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cache unreadable; starting fresh: %s", exc)
        return {"version": 1, "files": {}}


def save_cache(cache: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(CACHE_FILE)


def scan_projects() -> list:
    return sorted(PROJECTS_ROOT.rglob("*.jsonl")) if PROJECTS_ROOT.exists() else []


def normalize_timestamp(value: Optional[str], fallback: float) -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def normalized_path(raw_path: str, cwd: Optional[str]) -> str:
    path = Path(os.path.expanduser(raw_path))
    if not path.is_absolute():
        path = Path(cwd or "/srv") / path
    return os.path.normpath(str(path))


def is_excluded(file_path: str) -> bool:
    path = Path(file_path)
    excluded_roots = [
        PROJECT_ROOT / "data",
        Path.home() / ".claude",
        Path("/tmp"),
        Path("/var/tmp"),
    ]
    try:
        resolved = path.resolve(strict=False)
        if any(resolved == root or root in resolved.parents for root in excluded_roots):
            return True
    except OSError:
        return True
    return path.suffix.lower() in EXCLUDED_SUFFIXES


def restore_edit_content(pending: dict, tool_result: object) -> Optional[str]:
    tool_input = pending.get("input") or {}
    if pending["tool"] == "Write":
        if isinstance(tool_result, dict) and isinstance(tool_result.get("content"), str):
            return tool_result["content"]
        return tool_input.get("content")

    if pending["tool"] == "NotebookEdit":
        if isinstance(tool_result, dict):
            for key in ("content", "newSource", "new_source", "originalFile"):
                if isinstance(tool_result.get(key), str) and tool_result[key]:
                    return tool_result[key]
        path = pending.get("file_path")
        try:
            return Path(path).read_text(encoding="utf-8") if path else None
        except (OSError, UnicodeError):
            return None

    result = tool_result if isinstance(tool_result, dict) else {}
    original = result.get("originalFile")
    if not isinstance(original, str):
        original = tool_input.get("originalFile")
    old = tool_input.get("old_string", result.get("oldString"))
    new = tool_input.get("new_string", result.get("newString", ""))
    replace_all = bool(tool_input.get("replace_all", result.get("replaceAll", False)))
    if not isinstance(original, str) or not isinstance(old, str) or old not in original:
        return None
    return original.replace(old, new) if replace_all else original.replace(old, new, 1)


def _content_blocks(entry: dict) -> list:
    message = entry.get("message") or {}
    content = message.get("content")
    return content if isinstance(content, list) else []


def parse_line(
    entry: dict, pending: dict, source_jsonl: str, mtime: float
) -> tuple[list, set]:
    events, resolved = [], set()
    for block in _content_blocks(entry):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") in WATCHED_TOOLS:
            tool_input = block.get("input") or {}
            raw_path = (
                tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or tool_input.get("path")
            )
            if not raw_path:
                continue
            tool_id = block.get("id")
            if tool_id:
                pending[tool_id] = {
                    "tool": block["name"],
                    "input": tool_input,
                    "file_path": normalized_path(raw_path, entry.get("cwd")),
                    "created_at": normalize_timestamp(entry.get("timestamp"), mtime),
                    "context": {
                        key: entry.get(key)
                        for key in ("sessionId", "cwd", "isSidechain", "version")
                    },
                }
        if block.get("type") != "tool_result":
            continue
        tool_id = block.get("tool_use_id")
        item = pending.get(tool_id)
        if not item:
            continue
        resolved.add(tool_id)
        if block.get("is_error") or is_excluded(item["file_path"]):
            continue
        content = restore_edit_content(item, entry.get("toolUseResult"))
        if content is None:
            logger.warning(
                "Cannot restore %s result for %s", item["tool"], item["file_path"]
            )
            continue
        result = entry.get("toolUseResult")
        user_modified = result.get("userModified") if isinstance(result, dict) else None
        context = item.get("context", {})
        events.append(
            {
                "platform": "claude_code",
                "native_id": item["file_path"],
                "content": content,
                "event_type": "llm_output",
                "captured_at": normalize_timestamp(entry.get("timestamp"), mtime),
                "metadata": {
                    **context,
                    "tool": item["tool"],
                    "tool_use_id": tool_id,
                    "source_jsonl": source_jsonl,
                    "user_modified": user_modified,
                },
                "_tool_use_id": tool_id,
            }
        )
    return events, resolved


def expire_pending(pending: dict) -> None:
    now = datetime.now(timezone.utc)
    for tool_id, item in list(pending.items()):
        try:
            created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            created = now
        if (now - created).total_seconds() > PENDING_TTL_SECONDS:
            logger.warning("Dropping unresolved tool_use older than 24h: %s", tool_id)
            pending.pop(tool_id, None)


def read_complete_lines(path: Path, offset: int) -> tuple[list, int]:
    size = path.stat().st_size
    if size < offset:
        logger.warning("%s was truncated; resuming at current EOF", path)
        return [], size
    lines = []
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            start = handle.tell()
            line = handle.readline()
            if not line:
                return lines, handle.tell()
            if not line.endswith(b"\n"):
                return lines, start
            lines.append(line)


def post_ingest(event: dict) -> Optional[dict]:
    payload = {key: value for key, value in event.items() if not key.startswith("_")}
    for attempt in range(3):
        try:
            response = requests.post(INGEST_URL, json=payload, timeout=180)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.warning("Ingest failed (%s/3): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(30)
    return None


def poll_once(*, backfill: bool = False, dry_run: bool = False) -> int:
    cache = load_cache()
    parsed_files, all_events = {}, []
    paths = scan_projects()
    for path in paths:
        key = str(path)
        state = deepcopy(cache["files"].get(key, {}))
        state.setdefault("offset", 0)
        state.setdefault("pending", {})
        state.setdefault("processed_tool_use_ids", [])
        if backfill:
            state = {"offset": 0, "pending": {}, "processed_tool_use_ids": []}
        start_offset = state["offset"]
        raw_lines, end_offset = read_complete_lines(path, start_offset)
        pending = state["pending"]
        events, resolved_ids = [], set()
        for raw_line in raw_lines:
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Bad JSONL line in %s: %s", path, exc)
                continue
            found, resolved = parse_line(entry, pending, key, path.stat().st_mtime)
            events.extend(found)
            resolved_ids.update(resolved)
        for tool_id in resolved_ids:
            pending.pop(tool_id, None)
        expire_pending(pending)
        processed = set(state["processed_tool_use_ids"])
        events = [event for event in events if event["_tool_use_id"] not in processed]
        parsed_files[key] = {
            "start_offset": start_offset,
            "end_offset": end_offset,
            "pending": pending,
            "events": events,
            "failed": False,
            "processed": processed,
        }
        all_events.extend(events)

    all_events.sort(key=lambda event: event["captured_at"])
    if dry_run:
        for event in all_events:
            logger.info(
                "[DRY RUN] %s %s %s",
                event["metadata"]["tool"],
                event["native_id"],
                event["captured_at"],
            )
        logger.info("Dry run: %d events from %d JSONL files", len(all_events), len(paths))
        return len(all_events)

    ingested = 0
    for event in all_events:
        file_key = event["metadata"]["source_jsonl"]
        result = post_ingest(event)
        if not result:
            parsed_files[file_key]["failed"] = True
            continue
        ingested += 1
        parsed_files[file_key]["processed"].add(event["_tool_use_id"])
        file_cache = cache["files"].setdefault(file_key, {})
        file_cache["processed_tool_use_ids"] = sorted(
            parsed_files[file_key]["processed"]
        )
        save_cache(cache)  # per-item checkpoint
        logger.info(
            "Ingested %s %s -> master %s",
            event["metadata"]["tool"],
            event["native_id"],
            result["master_id"],
        )

    for key, parsed in parsed_files.items():
        file_cache = cache["files"].setdefault(key, {})
        if parsed["failed"]:
            file_cache["offset"] = parsed["start_offset"]
            file_cache["processed_tool_use_ids"] = sorted(parsed["processed"])
        else:
            file_cache["offset"] = parsed["end_offset"]
            file_cache["pending"] = parsed["pending"]
            file_cache["processed_tool_use_ids"] = []
        save_cache(cache)
    logger.info(
        "Scan complete: %d/%d ingested from %d JSONL files",
        ingested,
        len(all_events),
        len(paths),
    )
    return ingested


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.backfill or args.once:
        poll_once(backfill=args.backfill, dry_run=args.dry_run)
        return
    while True:
        try:
            poll_once()
        except Exception:
            logger.exception("Claude Code scan failed")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
