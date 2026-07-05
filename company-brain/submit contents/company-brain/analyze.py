"""Dump episode groupings + match metadata for sanity check (runs on VPS)."""
import json
import sqlite3

conn = sqlite3.connect("/srv/company-brain/data/events.db")
conn.row_factory = sqlite3.Row

eps = conn.execute("select count(*) c from episodes").fetchone()["c"]
evs = conn.execute("select count(*) c from events").fetchone()["c"]
print(f"episodes={eps} events={evs}\n")

rows = conn.execute(
    "select master_id, native_id, event_type, captured_at, metadata_json "
    "from events order by master_id, captured_at"
).fetchall()

by_master = {}
for r in rows:
    by_master.setdefault(r["master_id"], []).append(r)

for i, (mid, group) in enumerate(sorted(by_master.items(), key=lambda kv: kv[1][0]["captured_at"]), 1):
    print(f"--- episode {i} ({mid[:8]}) [{len(group)} events] ---")
    for r in group:
        md = json.loads(r["metadata_json"] or "{}")
        extra = []
        if "match_score" in md:
            extra.append(f"score={md['match_score']:.3f}")
        if "match_rationale" in md:
            extra.append(f"rationale={md['match_rationale']}")
        if "identity_break_check" in md:
            extra.append(f"idbreak={md['identity_break_check']}")
        print(f"  {r['captured_at'][:10]}  {r['native_id']}"
              + ("  [" + " | ".join(extra) + "]" if extra else ""))
    print()
