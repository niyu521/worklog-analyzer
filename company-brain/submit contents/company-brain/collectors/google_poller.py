#!/usr/bin/env python3
"""Collect Google Docs revisions, Sheets snapshots, and Drive text files."""

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = Path(__file__).resolve().parent / ".google_cache.json"
INGEST_URL = os.getenv("COMPANY_BRAIN_INGEST_URL", "http://127.0.0.1:8420/ingest")
SA_KEY_PATH = os.getenv(
    "GOOGLE_SA_KEY_PATH", str(PROJECT_ROOT / "secrets" / "google-sa.json")
)
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
SUPPORTED_TEXT_MIMES = {"text/plain", "text/markdown", "text/csv"}
REQUEST_SLEEP = 0.2
MAX_RETRIES = 3
RETRY_DELAY = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cache unreadable; starting fresh: %s", exc)
        return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(CACHE_FILE)


class GoogleClient:
    def __init__(self, key_path: str):
        path = Path(key_path)
        if not path.exists():
            raise FileNotFoundError(f"service account key not found: {path}")
        self.creds = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES
        )
        self.session = requests.Session()
        logger.info("Google auth: %s", self.creds.service_account_email)

    def _headers(self) -> dict:
        if not self.creds.valid:
            self.creds.refresh(Request())
        return {"Authorization": f"Bearer {self.creds.token}"}

    def request(self, method: str, url: str, *, raw: bool = False, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                headers = dict(kwargs.pop("headers", {}))
                headers.update(self._headers())
                time.sleep(REQUEST_SLEEP)
                response = self.session.request(
                    method, url, headers=headers, timeout=30, **kwargs
                )
                response.raise_for_status()
                return response.text if raw else (
                    response.json() if response.content else {}
                )
            except requests.RequestException as exc:
                status = getattr(exc.response, "status_code", None)
                retryable = status == 429 or status is None or (status >= 500)
                if not retryable or attempt == MAX_RETRIES - 1:
                    raise
                wait = RETRY_DELAY * (attempt + 1)
                logger.warning("Google API error %s; retrying in %ss", exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"Google API retry exhaustion: {url}")

    def list_files(self, query: str) -> list:
        files, page_token = [], None
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime)",
                "pageSize": 1000,
                "spaces": "drive",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            result = self.request(
                "GET", "https://www.googleapis.com/drive/v3/files", params=params
            )
            files.extend(result.get("files", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return files

    def list_revisions(self, file_id: str) -> list:
        revisions, page_token = [], None
        while True:
            params = {
                "fields": "nextPageToken,revisions(id,modifiedTime,exportLinks)",
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            result = self.request(
                "GET",
                f"https://www.googleapis.com/drive/v3/files/{file_id}/revisions",
                params=params,
            )
            revisions.extend(result.get("revisions", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return revisions

    def export_revision(self, revision: dict) -> str:
        export_url = (revision.get("exportLinks") or {}).get("text/plain")
        if not export_url:
            raise ValueError("revision has no text/plain exportLink")
        return self.request("GET", export_url, raw=True)

    def sheet_titles(self, file_id: str) -> list:
        result = self.request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}",
            params={"fields": "sheets(properties(title))"},
        )
        return [item["properties"]["title"] for item in result.get("sheets", [])]

    def sheet_values(self, file_id: str, titles: list) -> dict:
        # A1 notation escapes an apostrophe inside a sheet name by doubling it.
        ranges = [f"'{title.replace(chr(39), chr(39) * 2)}'!A:Z" for title in titles]
        result = self.request(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{file_id}/values:batchGet",
            params=[("ranges", value) for value in ranges]
            + [("valueRenderOption", "FORMATTED_VALUE")],
        )
        return {
            title: value_range.get("values", [])
            for title, value_range in zip(titles, result.get("valueRanges", []))
        }

    def download_text(self, file_id: str) -> str:
        return self.request(
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media"},
            raw=True,
        )


def post_ingest(event: dict) -> Optional[dict]:
    for attempt in range(3):
        try:
            response = requests.post(INGEST_URL, json=event, timeout=180)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.warning("Ingest failed (%s/3): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(30)
    return None


def docs_events(client: GoogleClient, cache: dict) -> tuple[list, dict]:
    events, states = [], {}
    files = client.list_files(
        "mimeType='application/vnd.google-apps.document' and trashed=false"
    )
    logger.info("Found %d Docs", len(files))
    for file_info in files:
        file_id = file_info["id"]
        prior_ids = set(cache.get(file_id, {}).get("ingested_revisions", []))
        try:
            revisions = client.list_revisions(file_id)
        except Exception as exc:  # keep other files moving
            logger.error("[%s] revisions failed: %s", file_info["name"], exc)
            continue
        states[file_id] = {
            "modifiedTime": file_info.get("modifiedTime"),
            "all_revision_ids": [revision["id"] for revision in revisions],
        }
        for revision in revisions:
            if revision["id"] in prior_ids:
                continue
            try:
                content = client.export_revision(revision)
            except Exception as exc:
                logger.error(
                    "[%s] revision %s export failed: %s",
                    file_info["name"],
                    revision["id"],
                    exc,
                )
                continue
            events.append(
                {
                    "platform": "google_docs",
                    "native_id": file_id,
                    "content": content,
                    "event_type": "revision",
                    "captured_at": revision.get("modifiedTime")
                    or file_info["modifiedTime"],
                    "metadata": {
                        "file_name": file_info["name"],
                        "revision_id": revision["id"],
                    },
                }
            )
    return events, states


def snapshot_events(client: GoogleClient, cache: dict) -> list:
    events = []
    files = client.list_files(
        "(mimeType='application/vnd.google-apps.spreadsheet' "
        "or mimeType='text/plain' or mimeType='text/markdown' "
        "or mimeType='text/csv') and trashed=false"
    )
    sheets = sum(
        f["mimeType"] == "application/vnd.google-apps.spreadsheet" for f in files
    )
    logger.info("Found %d Sheets and %d Drive text files", sheets, len(files) - sheets)
    for file_info in files:
        file_id = file_info["id"]
        modified = file_info["modifiedTime"]
        if cache.get(file_id, {}).get("modifiedTime") == modified:
            continue
        try:
            if file_info["mimeType"] == "application/vnd.google-apps.spreadsheet":
                titles = client.sheet_titles(file_id)
                values = client.sheet_values(file_id, titles)
                lines = []
                for title in titles:
                    lines.append(f"## {title}")
                    lines.extend(
                        "\t".join(str(cell) for cell in row)
                        for row in values.get(title, [])
                    )
                    lines.append("")
                content = "\n".join(lines)
                platform = "google_sheets"
                extra = {"sheet_count": len(titles)}
            else:
                content = client.download_text(file_id)
                platform = "google_drive_text"
                extra = {"mime_type": file_info["mimeType"]}
            events.append(
                {
                    "platform": platform,
                    "native_id": file_id,
                    "content": content,
                    "event_type": "revision",
                    "captured_at": modified,
                    "metadata": {"file_name": file_info["name"], **extra},
                }
            )
        except Exception as exc:
            logger.error("[%s] snapshot failed: %s", file_info["name"], exc)
    return events


def poll_once(*, dry_run: bool = False, ignore_cache: bool = False) -> int:
    client = GoogleClient(SA_KEY_PATH)
    cache = {} if ignore_cache else load_cache()
    doc_events, doc_states = docs_events(client, cache)
    events = doc_events + snapshot_events(client, cache)
    events.sort(key=lambda event: event["captured_at"])
    if dry_run:
        for event in events:
            logger.info(
                "[DRY RUN] %s %s %s",
                event["platform"],
                event["native_id"],
                event["captured_at"],
            )
        return len(events)

    ingested = 0
    for event in events:
        result = post_ingest(event)
        if not result:
            logger.error("Giving up for now: %s", event["native_id"])
            continue
        ingested += 1
        file_id = event["native_id"]
        if event["platform"] == "google_docs":
            entry = cache.setdefault(file_id, {"ingested_revisions": []})
            revision_id = event["metadata"]["revision_id"]
            if revision_id not in entry["ingested_revisions"]:
                entry["ingested_revisions"].append(revision_id)
            state = doc_states[file_id]
            if set(entry["ingested_revisions"]) >= set(state["all_revision_ids"]):
                entry["modifiedTime"] = state["modifiedTime"]
        else:
            cache[file_id] = {"modifiedTime": event["captured_at"]}
        save_cache(cache)  # per-item checkpoint
        logger.info(
            "Ingested %s -> master %s", event["metadata"]["file_name"], result["master_id"]
        )
    logger.info("Poll complete: %d/%d ingested", ingested, len(events))
    return ingested


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ignore-cache",
        action="store_true",
        help="backfill even if cache says an item was ingested",
    )
    args = parser.parse_args()
    if args.once:
        poll_once(dry_run=args.dry_run, ignore_cache=args.ignore_cache)
        return
    while True:
        try:
            poll_once(dry_run=args.dry_run)
        except Exception:
            logger.exception("Google poll cycle failed")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
