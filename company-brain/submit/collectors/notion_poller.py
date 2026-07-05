"""Notion collector for Company Brain.

Polls the Notion API for every page / database row visible to the
integration, flattens each page's block tree to plain text, and feeds
changed pages into the lineage engine via ingest_raw_event().

Usage:
    python notion_poller.py --once   # single pass (testing)
    python notion_poller.py          # poll forever, 45s interval
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ingest"))

from matcher import ingest_raw_event  # noqa: E402
from schema import init_db  # noqa: E402

NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
CACHE_PATH = Path(__file__).resolve().parent / ".notion_cache.json"
POLL_INTERVAL_SEC = 45
REQUEST_PAUSE_SEC = 0.34  # stay under Notion's ~3 req/s limit

# block types whose text lives in block[type]["rich_text"]
RICH_TEXT_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "quote", "callout", "toggle", "code",
}


def load_env():
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("FATAL: NOTION_TOKEN not found in environment / .env", file=sys.stderr)
        sys.exit(1)
    return token


class NotionClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, payload: dict = None) -> dict:
        url = f"{NOTION_BASE}{path}"
        for attempt in range(5):
            time.sleep(REQUEST_PAUSE_SEC)
            resp = self.session.request(method, url, json=payload, timeout=30)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", "2"))
                print(f"  rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Notion API gave up after retries: {method} {path}")

    def paginate(self, method: str, path: str, payload: dict = None):
        """Yield results across all pages of a paginated endpoint."""
        payload = dict(payload or {})
        payload["page_size"] = 100
        while True:
            data = self._request(method, path, payload)
            yield from data.get("results", [])
            if not data.get("has_more"):
                break
            payload["start_cursor"] = data["next_cursor"]

    # --- discovery -------------------------------------------------------
    def search_all(self):
        pages, databases = {}, {}
        for obj in self.paginate("POST", "/search", {}):
            if obj.get("object") == "page":
                pages[obj["id"]] = obj
            elif obj.get("object") == "database":
                databases[obj["id"]] = obj
        return pages, databases

    def query_database(self, database_id: str):
        try:
            yield from self.paginate("POST", f"/databases/{database_id}/query", {})
        except requests.HTTPError as exc:
            print(f"  WARN: cannot query database {database_id}: {exc}")

    # --- content extraction ----------------------------------------------
    @staticmethod
    def _rich_text_to_plain(rich_text_array) -> str:
        return "".join(rt.get("plain_text", "") for rt in (rich_text_array or []))

    def extract_block_text(self, block_id: str, depth: int = 0) -> list:
        """Recursively flatten a block tree to a list of text lines."""
        if depth > 10:  # safety valve against pathological nesting
            return []
        lines = []
        try:
            children = list(self.paginate("GET", f"/blocks/{block_id}/children"))
        except requests.HTTPError as exc:
            print(f"  WARN: cannot read blocks of {block_id}: {exc}")
            return lines
        for block in children:
            btype = block.get("type", "")
            payload = block.get(btype, {}) or {}
            text = ""
            if btype in RICH_TEXT_TYPES:
                text = self._rich_text_to_plain(payload.get("rich_text"))
            elif btype == "child_page":
                text = payload.get("title", "")
            elif btype == "table_row":
                text = " | ".join(
                    self._rich_text_to_plain(cell) for cell in payload.get("cells", [])
                )
            elif "rich_text" in payload:  # fallback for uncovered text-bearing types
                text = self._rich_text_to_plain(payload.get("rich_text"))
            if text.strip():
                lines.append(text.strip())
            if block.get("has_children") and btype != "child_page":
                lines.extend(self.extract_block_text(block["id"], depth + 1))
        return lines

    @staticmethod
    def page_title(page: dict) -> str:
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") == "title":
                return "".join(rt.get("plain_text", "") for rt in prop.get("title", []))
        return ""

    def page_plain_text(self, page: dict) -> str:
        parts = []
        title = self.page_title(page)
        if title:
            parts.append(title)
        parts.extend(self.extract_block_text(page["id"]))
        return "\n".join(parts)


# --- cache -----------------------------------------------------------------

def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("WARN: cache unreadable, starting fresh")
    return {}


def save_cache(cache: dict):
    CACHE_PATH.write_text(json.dumps(cache, indent=1), encoding="utf-8")


# --- main poll pass ----------------------------------------------------------

def poll_once(client: NotionClient):
    cache = load_cache()
    pages, databases = client.search_all()

    # database rows are pages too; tag them with their parent database
    row_parent = {}
    for db_id in databases:
        for row in client.query_database(db_id):
            pages[row["id"]] = row
            row_parent[row["id"]] = db_id

    # also detect rows whose parent is a database (search sometimes returns them directly)
    for pid, page in pages.items():
        parent = page.get("parent") or {}
        if parent.get("type") == "database_id" and pid not in row_parent:
            row_parent[pid] = parent["database_id"]

    changed = [
        page for pid, page in pages.items()
        if cache.get(pid) != page.get("last_edited_time")
    ]
    # ingest in chronological order so revision chains build correctly
    changed.sort(key=lambda p: p.get("last_edited_time", ""))

    print(f"found {len(pages)} pages ({len(row_parent)} database rows, "
          f"{len(databases)} databases), {len(changed)} changed")

    new_eps, attached = 0, 0
    for page in changed:
        pid = page["id"]
        content = client.page_plain_text(page)
        if not content.strip():
            print(f"  skip {pid}: empty content")
            cache[pid] = page.get("last_edited_time")
            continue
        metadata = {
            "notion_object_type": "database_row" if pid in row_parent else "page",
            "title": client.page_title(page),
        }
        if pid in row_parent:
            metadata["parent_database_id"] = row_parent[pid]
        result = ingest_raw_event(
            platform="notion",
            native_id=pid,
            content=content,
            event_type="revision",
            captured_at=page.get("last_edited_time"),
            metadata=metadata,
        )
        cache[pid] = page.get("last_edited_time")
        save_cache(cache)  # persist per page so a crash doesn't re-ingest everything... twice
        if result.get("new_episode"):
            new_eps += 1
        else:
            attached += 1
        print(f"  ingested {pid} ({metadata['notion_object_type']}, "
              f"'{metadata['title'][:40]}') -> master {result['master_id'][:8]} "
              f"{'NEW' if result.get('new_episode') else 'attached'}")

    save_cache(cache)
    print(f"pass done: {new_eps} new episodes, {attached} attached, "
          f"{len(pages) - len(changed)} unchanged")


def main():
    parser = argparse.ArgumentParser(description="Notion collector for Company Brain")
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SEC,
        help=f"polling interval in seconds (default: {POLL_INTERVAL_SEC})",
    )
    args = parser.parse_args()

    token = load_env()
    init_db()
    client = NotionClient(token)

    if args.once:
        poll_once(client)
        return
    while True:
        try:
            poll_once(client)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"ERROR during poll pass: {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
