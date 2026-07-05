"""Core matching + ingestion logic for Company Brain Step1.

Design (finalized after two adversarial review passes):
- native_id known -> Layer1 trivial chain (cheap identity-break guard via LLM
  only when similarity to own prior tail is suspiciously low).
- native_id unknown -> compare against the most recent 200 open episodes'
  tail content (count-bounded, not time-bounded).
  - ratio >= 0.90            -> auto-attach, no LLM call
  - ratio <  0.55            -> new master_id, no LLM call
  - 0.55 <= ratio < 0.90     -> ambiguous band, ask an LLM (identity-field-aware)
  - top-2 candidates within 0.05 of each other -> also route to the LLM
    (template-boilerplate resonance guard)
- content < 200 normalized tokens -> require ratio >= 0.95 even to be a
  candidate (short-content noise guard); never routed to the LLM (no
  semantic signal to reason about).
- LLM verdict's rationale is persisted in metadata_json, not just the score
  (per third review pass: the verdict is re-derivable, the *reasoning* is not).
"""
import hashlib
import re
import subprocess
import sys
import uuid
from difflib import SequenceMatcher

from schema import BLOBS_DIR, get_conn

CANDIDATE_LIMIT = 200
AUTO_ATTACH_RATIO = 0.90
CLEAR_MISS_RATIO = 0.55
IDENTITY_BREAK_RATIO = 0.30
SHORT_CONTENT_TOKENS = 30
SHORT_CONTENT_RATIO = 0.95
AMBIGUITY_GUARD_DELTA = 0.05
LLM_TEXT_CAP = 3000

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def tokenize(text: str) -> list:
    return normalize(text).split(" ")


def token_ratio(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    return SequenceMatcher(None, ta, tb, autojunk=False).ratio()


def llm_judge(text_a: str, text_b: str) -> dict:
    """Ask Claude Code CLI to judge same-lineage vs independent-instance.

    Returns {"verdict": "MATCH"|"INDEPENDENT", "rationale": str}.
    Falls back to a conservative INDEPENDENT verdict if the CLI call fails
    or the response can't be parsed (a wrong-split is recoverable later;
    a wrong-merge is not).
    """
    prompt = f"""You are judging document lineage for a business-workflow tracking system.

Text A (existing, earlier):
---
{text_a[:LLM_TEXT_CAP]}
---

Text B (new, candidate continuation or reuse):
---
{text_b[:LLM_TEXT_CAP]}
---

Question: Is Text B a revision/edit of the SAME document as Text A, or is it an
INDEPENDENT document created from the same template/boilerplate (e.g. a
different client, different invoice/document number, different date or
period, different recipient)?

Pay special attention to identity-bearing fields: client/company name,
invoice or document number, dates, amounts, recipient. If ANY such field
differs while the surrounding structure is similar, treat it as INDEPENDENT
- do not be fooled by shared boilerplate text.

Respond in EXACTLY this format, nothing else:
VERDICT: MATCH or VERDICT: INDEPENDENT
RATIONALE: <one short sentence>"""

    try:
        proc = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=45,
        )
        out = proc.stdout.strip()
        verdict_m = re.search(r"VERDICT:\s*(MATCH|INDEPENDENT)", out, re.IGNORECASE)
        rationale_m = re.search(r"RATIONALE:\s*(.+)", out)
        verdict = verdict_m.group(1).upper() if verdict_m else "INDEPENDENT"
        rationale = rationale_m.group(1).strip() if rationale_m else f"unparsed LLM output: {out[:200]}"
        return {"verdict": verdict, "rationale": rationale}
    except Exception as exc:  # noqa: BLE001 - hackathon-scope: any failure -> safe default
        return {"verdict": "INDEPENDENT", "rationale": f"LLM call failed ({exc}); defaulted to split"}


def _write_blob(event_id: str, content: str) -> str:
    BLOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = BLOBS_DIR / f"{event_id}.txt"
    path.write_text(content, encoding="utf-8")
    return str(path)


def _read_blob(content_ref: str) -> str:
    try:
        with open(content_ref, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _latest_tails(conn, limit: int = CANDIDATE_LIMIT):
    """Latest event per master_id, most recently active masters first."""
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY master_id ORDER BY captured_at DESC
            ) AS rn
            FROM events
        )
        WHERE rn = 1
        ORDER BY captured_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows


def _latest_for_native(conn, platform: str, native_id: str):
    return conn.execute(
        """
        SELECT * FROM events
        WHERE platform = ? AND native_id = ?
        ORDER BY captured_at DESC
        LIMIT 1
        """,
        (platform, native_id),
    ).fetchone()


def _insert_event(conn, master_id, parent_event_id, platform, native_id,
                   event_type, content, captured_at, metadata: dict):
    import json

    event_id = uuid.uuid4().hex
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    content_ref = _write_blob(event_id, content)
    conn.execute(
        """
        INSERT INTO events (event_id, master_id, parent_event_id, platform,
                             native_id, event_type, content_hash, content_ref,
                             metadata_json, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, master_id, parent_event_id, platform, native_id, event_type,
         content_hash, content_ref, json.dumps(metadata, ensure_ascii=False), captured_at),
    )
    return event_id


def ingest_raw_event(platform: str, native_id: str, content: str,
                      event_type: str, captured_at: str, metadata: dict = None) -> dict:
    """Main entry point. See module docstring for the algorithm.

    metadata may be pre-populated by the caller (collector-specific fields);
    this function adds matching-related keys (match_score, match_rationale,
    secondary_parent_event_id, secondary_match_score) as applicable.
    """
    metadata = dict(metadata or {})
    conn = get_conn()
    try:
        token_count = len(tokenize(content))
        min_ratio_floor = SHORT_CONTENT_RATIO if token_count < SHORT_CONTENT_TOKENS else 0.0

        # --- Layer1: known native_id ---
        prior = _latest_for_native(conn, platform, native_id)
        if prior is not None:
            prior_content = _read_blob(prior["content_ref"])
            ratio = token_ratio(prior_content, content)
            if ratio < IDENTITY_BREAK_RATIO:
                verdict = llm_judge(prior_content, content)
                metadata["identity_break_check"] = verdict
                if verdict["verdict"] == "INDEPENDENT":
                    master_id = uuid.uuid4().hex
                    conn.execute(
                        "INSERT INTO episodes (master_id, task_label, created_at) VALUES (?, NULL, ?)",
                        (master_id, captured_at),
                    )
                    event_id = _insert_event(conn, master_id, None, platform, native_id,
                                              event_type, content, captured_at, metadata)
                    conn.commit()
                    return {"event_id": event_id, "master_id": master_id, "new_episode": True}
            # same lineage, trivial Layer1 chain
            event_id = _insert_event(conn, prior["master_id"], prior["event_id"], platform,
                                      native_id, event_type, content, captured_at, metadata)
            conn.commit()
            return {"event_id": event_id, "master_id": prior["master_id"], "new_episode": False}

        # --- native_id unseen: candidate search among latest tails ---
        candidates = _latest_tails(conn)
        scored = []
        for cand in candidates:
            cand_content = _read_blob(cand["content_ref"])
            ratio = token_ratio(cand_content, content)
            if ratio < min_ratio_floor:
                continue
            scored.append((ratio, cand, cand_content))
        scored.sort(key=lambda t: t[0], reverse=True)

        best = scored[0] if scored else None
        second = scored[1] if len(scored) > 1 else None
        ambiguity_guard = bool(best and second and (best[0] - second[0]) <= AMBIGUITY_GUARD_DELTA)

        attach_to = None
        if best and min_ratio_floor >= SHORT_CONTENT_RATIO:
            # short-content: numeric floor only, never LLM (no semantic signal)
            if best[0] >= min_ratio_floor:
                attach_to = best
        elif best and best[0] >= AUTO_ATTACH_RATIO and not ambiguity_guard:
            attach_to = best
        elif best and best[0] < CLEAR_MISS_RATIO:
            attach_to = None
        elif best:
            # ambiguous band (0.55-0.90) or ambiguity guard triggered
            verdict = llm_judge(best[2], content)
            metadata["match_score"] = best[0]
            metadata["match_rationale"] = verdict["rationale"]
            if verdict["verdict"] == "MATCH":
                attach_to = best

        if attach_to:
            ratio, cand, _ = attach_to
            metadata.setdefault("match_score", ratio)
            if second and second is not attach_to:
                metadata["secondary_parent_event_id"] = second[1]["event_id"]
                metadata["secondary_match_score"] = second[0]
            event_id = _insert_event(conn, cand["master_id"], cand["event_id"], platform,
                                      native_id, event_type, content, captured_at, metadata)
            conn.commit()
            return {"event_id": event_id, "master_id": cand["master_id"], "new_episode": False}

        # no match -> new episode root ("start flag" = the no-match fallback)
        master_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO episodes (master_id, task_label, created_at) VALUES (?, NULL, ?)",
            (master_id, captured_at),
        )
        event_id = _insert_event(conn, master_id, None, platform, native_id,
                                  event_type, content, captured_at, metadata)
        conn.commit()
        return {"event_id": event_id, "master_id": master_id, "new_episode": True}
    finally:
        conn.close()


if __name__ == "__main__":
    from schema import init_db

    init_db()
    print("matcher self-test:")
    r1 = ingest_raw_event("claude_code", "session-1/invoice.md",
                          "Invoice for Client A. Amount: 100000 yen. Due: 2026-08-01.",
                          "llm_output", "2026-07-05T10:00:00")
    print(r1)
    r2 = ingest_raw_event("google_docs", "gdoc-abc123",
                          "Invoice for Client A. Amount: 100000 yen. Due: 2026-08-01.",
                          "revision", "2026-07-05T10:05:00")
    print(r2)
    assert r2["master_id"] == r1["master_id"], "expected same lineage to merge"
    print("OK: same-content cross-platform merge worked", file=sys.stderr)
