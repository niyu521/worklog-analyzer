"""Bulk-ingest extracted invoice PDFs into Company Brain (runs on VPS)."""
import json
import sys
import time

sys.path.insert(0, "/srv/company-brain/ingest")
from matcher import ingest_raw_event  # noqa: E402
from schema import init_db  # noqa: E402

init_db()

with open("/srv/company-brain/invoice_bulk.json", encoding="utf-8") as f:
    records = json.load(f)

records.sort(key=lambda r: r["mtime_iso"])  # oldest first
print(f"ingesting {len(records)} records", flush=True)

for i, rec in enumerate(records, 1):
    t0 = time.time()
    res = ingest_raw_event(
        platform="local_pdf",
        native_id=rec["relative_path"],
        content=rec["content"],
        event_type="revision",
        captured_at=rec["mtime_iso"],
        metadata={"source_file": rec["relative_path"]},
    )
    dt = time.time() - t0
    tag = "NEW " if res["new_episode"] else "JOIN"
    print(f"[{i:2d}/{len(records)}] {tag} master={res['master_id'][:8]} "
          f"({dt:.1f}s) {rec['relative_path']}", flush=True)

print("done", flush=True)
