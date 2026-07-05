#!/usr/bin/env python3
"""
analyze_worklogs.py

Local, offline analyzer for Claude Code (~/.claude) and Codex (~/.codex) work
logs. Reads session transcripts / history / memory files from the local
filesystem only, normalizes them into a common event schema, decomposes
prompts into atomic activities, groups events into task segments, re-stitches
segments that belong to the same underlying task, detects recurring work
routines, proposes automation/skill candidates, and renders everything as a
single self-contained HTML report plus a JSON dump.

Nothing in this script makes network calls. Secret-looking files are skipped
entirely, and secret-looking substrings inside otherwise-safe text are masked
before they are written to the JSON/HTML output.

Run:
    python3 analyze_worklogs.py

Outputs (written next to this script):
    worklog_report_last_7_days.html
    parsed_worklog_last_7_days.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from difflib import SequenceMatcher
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    DISPLAY_TZ = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover - extremely old python fallback
    DISPLAY_TZ = timezone(timedelta(hours=9))

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

LOOKBACK_DAYS = 7
MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # skip anything bigger than this
MAX_EVENTS_HARD_CAP = 40000              # safety valve against runaway logs
SCRIPT_DIR = Path(__file__).resolve().parent
OUT_JSON = SCRIPT_DIR / "parsed_worklog_last_7_days.json"
OUT_HTML = SCRIPT_DIR / "worklog_report_last_7_days.html"

HOME = Path.home()
CWD = Path.cwd()

CANDIDATE_ROOTS = []
for base in (HOME, CWD):
    CANDIDATE_ROOTS.append(base / ".claude")
    CANDIDATE_ROOTS.append(base / ".codex")

# Where to look for Browser Activity Logger export bundles (the JSON the Chrome
# extension writes when you click "Export JSON"). These are ordinary downloaded
# files with no fixed location, so we scan the usual spots and also accept
# explicit paths passed on the command line (see main()).
BROWSER_EXPORT_DIRS = [
    SCRIPT_DIR,
    SCRIPT_DIR / "browser-exports",
    CWD,
    HOME / "Downloads",
]

# Directories we never walk into: they hold binaries, caches, plugin source
# code, or content most likely to contain credentials/tokens rather than
# user work. This list is intentionally conservative (skip more, not less).
DENY_DIR_NAMES = {
    "node_modules", ".git", "cache", "plugins", "chrome", "session-env",
    "shell-snapshots", "shell_snapshots", "paste-cache", "backups",
    "telemetry", "file-history", "ide", ".tmp", ".tmp.driveupload",
    ".tmp.drivedownload", "generated_images", "computer-use", "pets",
    "ambient-suggestions", "sqlite", "vendor_imports", "process_manager",
    "log", "tmp", "commands", "skills", ".plugin-appserver",
    ".remote-plugin-install-staging", "bundled-marketplaces",
}

# File name substrings that mark a file as "secret-like" -> never read its
# contents, only note that it was skipped.
SECRET_FILENAME_SUBSTRINGS = [
    "credential", "token", "secret", "api_key", "apikey", ".env",
    ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".pem", ".p12", ".pfx", ".netrc", "cookie", "auth.json",
]
SECRET_FILENAME_WORD_RE = re.compile(r"\bkey\b", re.IGNORECASE)

ALLOWED_EXTENSIONS = {".jsonl", ".json", ".md", ".markdown", ".yaml", ".yml",
                       ".toml", ".txt"}

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"sk-proj-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"\bASIA[0-9A-Z]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.=]{10,}", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|access[_-]?key|"
        r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',]{6,}"
    ),
    # URL query params that commonly carry share tokens / signatures / auth
    # codes, e.g. ...?tk=wQmTA5 or &access_token=... . Keeps the param name
    # for readability, masks the value.
    re.compile(
        r"(?i)([?&](?:tk|token|access[_-]?token|auth|api[_-]?key|key|sig|"
        r"signature|code|state|session|sid|pwd|secret)=)[^&\s\"']{4,}"
    ),
]


def redact_text(text):
    """Mask secret-looking substrings. Returns (clean_text, was_redacted)."""
    if not text:
        return text, False
    redacted = False
    out = text
    for pat in _SECRET_PATTERNS:
        def _sub(m):
            nonlocal redacted
            redacted = True
            g = m.group(0)
            # keep "key=" / "token:" style prefixes for readability
            head_match = re.match(r"^[\w-]{2,20}\s*[:=]\s*", g)
            if head_match:
                return head_match.group(0) + "[REDACTED]"
            return "[REDACTED]"
        out = pat.sub(_sub, out)
    return out, redacted


def truncate(text, limit=600):
    if text is None:
        return text
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …[truncated]"


def safe_text(text, limit=600):
    """Redact then truncate. Use for anything destined for the report."""
    clean, was_redacted = redact_text(text or "")
    return truncate(clean, limit), was_redacted


# --------------------------------------------------------------------------
# Secret-file / binary-file filtering
# --------------------------------------------------------------------------

def is_secret_path(path: Path) -> bool:
    parts_lower = [p.lower() for p in path.parts]
    name_lower = path.name.lower()
    if ".ssh" in parts_lower or ".aws" in parts_lower or ".gnupg" in parts_lower:
        return True
    for sub in SECRET_FILENAME_SUBSTRINGS:
        if sub in name_lower:
            return True
    if SECRET_FILENAME_WORD_RE.search(name_lower):
        return True
    return False


def looks_binary(path: Path, sniff_bytes=2048) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(sniff_bytes)
        if b"\x00" in chunk:
            return True
    except OSError:
        return True
    return False


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def iter_candidate_files(root: Path, cutoff_ts: float):
    """Walk a root dir, yielding files that are plausibly parseable, not
    secret-like, not binary, not oversized, and modified within the lookback
    window (mtime is used as a cheap pre-filter; parsers may still look at
    older lines inside a file if the file itself was touched recently)."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DENY_DIR_NAMES and not d.startswith(".git")]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if is_secret_path(fpath):
                continue
            if fpath.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            if st.st_size == 0 or st.st_size > MAX_FILE_SIZE_BYTES:
                continue
            if st.st_mtime < cutoff_ts:
                continue
            if looks_binary(fpath):
                continue
            yield fpath


def discover_files(cutoff_dt: datetime):
    cutoff_ts = cutoff_dt.timestamp()
    found = {"claude_transcripts": [], "claude_history": [], "claude_memory": [],
              "codex_rollouts": [], "codex_memory": [], "instruction_docs": [],
              "skipped_secret": 0}

    for root in CANDIDATE_ROOTS:
        if not root.exists():
            continue
        is_claude = root.name == ".claude"
        for fpath in iter_candidate_files(root, cutoff_ts):
            rel = fpath.relative_to(root)
            parts = rel.parts
            if is_claude:
                if "projects" in parts and fpath.suffix == ".jsonl":
                    found["claude_transcripts"].append(fpath)
                elif fpath.name == "history.jsonl":
                    found["claude_history"].append(fpath)
                elif "memory" in parts and fpath.suffix in (".md", ".markdown"):
                    found["claude_memory"].append(fpath)
                elif fpath.name.upper() in ("CLAUDE.MD", "AGENTS.MD"):
                    found["instruction_docs"].append(fpath)
            else:
                if "sessions" in parts and fpath.suffix == ".jsonl":
                    found["codex_rollouts"].append(fpath)
                elif "archived_sessions" in parts and fpath.suffix == ".jsonl":
                    found["codex_rollouts"].append(fpath)
                elif "memories" in parts and fpath.suffix in (".md", ".markdown"):
                    found["codex_memory"].append(fpath)
                elif fpath.name.upper() in ("CLAUDE.MD", "AGENTS.MD"):
                    found["instruction_docs"].append(fpath)

    # Also look for CLAUDE.md / AGENTS.md directly at the root of any project
    # directory we can infer once transcripts are parsed (done later, see
    # enrich_instruction_docs_from_projects()).
    return found


def enrich_instruction_docs_from_project_paths(project_paths, cutoff_ts, found):
    seen = {str(p) for p in found["instruction_docs"]}
    for proj in project_paths:
        if not proj:
            continue
        base = Path(proj)
        if not base.exists() or not base.is_dir():
            continue
        for candidate in ("CLAUDE.md", "AGENTS.md"):
            fpath = base / candidate
            if fpath.exists() and str(fpath) not in seen:
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if st.st_size <= MAX_FILE_SIZE_BYTES:
                    found["instruction_docs"].append(fpath)
                    seen.add(str(fpath))


# --------------------------------------------------------------------------
# Common event schema
# --------------------------------------------------------------------------

EVENT_FIELDS = [
    "event_id", "timestamp", "source", "source_session_id", "project_path",
    "repository", "event_type", "prompt_or_text", "tool_name", "command",
    "file_path", "summary", "redacted", "evidence",
]


def make_event(timestamp, source, source_session_id, project_path, event_type,
               prompt_or_text=None, tool_name=None, command=None, file_path=None,
               summary=None, evidence=None):
    text_redacted = False
    if prompt_or_text:
        prompt_or_text, r1 = safe_text(prompt_or_text, 4000)
        text_redacted = text_redacted or r1
    if command:
        command, r2 = safe_text(command, 1500)
        text_redacted = text_redacted or r2
    if summary:
        summary, r3 = safe_text(summary, 300)
        text_redacted = text_redacted or r3

    repository = None
    if project_path:
        repository = Path(str(project_path).rstrip("/")).name or str(project_path)

    return {
        "event_id": uuid.uuid4().hex[:12],
        "timestamp": timestamp,  # ISO8601 UTC string
        "source": source,        # "claude_code" | "codex"
        "source_session_id": source_session_id,
        "project_path": project_path,
        "repository": repository,
        "event_type": event_type,
        "prompt_or_text": prompt_or_text,
        "tool_name": tool_name,
        "command": command,
        "file_path": file_path,
        "summary": summary,
        "redacted": text_redacted,
        "evidence": evidence or {},
    }


def parse_iso(ts):
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def dt_to_iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def flatten_text_content(content):
    """Claude message content can be a plain string or a list of blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    for b2 in inner:
                        if isinstance(b2, dict) and b2.get("type") == "text":
                            parts.append(b2.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


# --------------------------------------------------------------------------
# Parser: Claude Code project transcripts (~/.claude/projects/**/*.jsonl)
# --------------------------------------------------------------------------

CLAUDE_FILE_TOOLS_READ = {"Read", "Glob", "Grep", "NotebookRead"}
CLAUDE_FILE_TOOLS_WRITE = {"Write", "Edit", "NotebookEdit", "MultiEdit"}
CLAUDE_COMMAND_TOOLS = {"Bash", "BashOutput", "KillShell"}

# Synthetic placeholder strings the harness injects into "user" records that
# are not actually authored by the user (interruption markers, local-command
# wrapper notices). These would otherwise show up as bogus, highly-repeated
# "atomic activities" and pollute routine detection.
_NOISE_PROMPT_EXACT = {
    "[request interrupted by user]",
    "[request interrupted by user for tool use]",
}
_NOISE_PROMPT_PREFIXES = ("<local-command-caveat>", "<command-name>", "<command-message>",
                           "<local-command-stdout>", "<local-command-stderr>")


def is_noise_prompt(text):
    if not text:
        return True
    t = text.strip()
    if t.lower() in _NOISE_PROMPT_EXACT:
        return True
    if t.startswith(_NOISE_PROMPT_PREFIXES):
        return True
    return False


def parse_claude_transcript(path: Path, cutoff_dt: datetime, parsed_files_log, events_out):
    session_id = path.stem
    project_path_guess = None
    line_errors = 0
    n_lines = 0
    tool_use_names = {}  # tool_use_id -> name, for matching tool_result -> tool_name

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    for line_no, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        n_lines += 1
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            line_errors += 1
            continue

        rtype = rec.get("type")
        ts = parse_iso(rec.get("timestamp"))
        cwd = rec.get("cwd")
        if cwd:
            project_path_guess = cwd

        evidence = {"file": str(path), "line": line_no, "session_id": session_id}

        if rtype == "user":
            if rec.get("isMeta"):
                continue  # harness-injected meta message, not user-authored content
            msg = rec.get("message", {})
            content = msg.get("content")
            uuid_ = rec.get("uuid")
            ts2 = parse_iso(rec.get("timestamp")) or ts
            if ts2 is None or ts2 < cutoff_dt:
                continue
            # tool_result blocks embedded in a "user" record -> tool_result events
            if isinstance(content, list):
                had_tool_result = False
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        had_tool_result = True
                        tool_name = tool_use_names.get(block.get("tool_use_id"), "unknown_tool")
                        text = flatten_text_content(block.get("content"))
                        is_error = bool(block.get("is_error"))
                        events_out.append(make_event(
                            dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                            "error" if is_error else "tool_result",
                            prompt_or_text=text, tool_name=tool_name,
                            summary=("tool error" if is_error else "tool result"),
                            evidence=evidence,
                        ))
                if had_tool_result:
                    continue
            text = flatten_text_content(content)
            if text.strip() and not is_noise_prompt(text):
                events_out.append(make_event(
                    dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                    "agent_prompt", prompt_or_text=text,
                    evidence=evidence,
                ))
            continue

        if rtype == "assistant":
            msg = rec.get("message", {})
            content = msg.get("content", [])
            ts2 = ts
            if ts2 is None or ts2 < cutoff_dt:
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text.strip():
                        events_out.append(make_event(
                            dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                            "agent_response", prompt_or_text=text,
                            evidence=evidence,
                        ))
                elif btype == "tool_use":
                    name = block.get("name", "unknown_tool")
                    tool_use_names[block.get("id")] = name
                    tool_input = block.get("input", {}) or {}
                    if name in CLAUDE_COMMAND_TOOLS:
                        events_out.append(make_event(
                            dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                            "command_run", tool_name=name,
                            command=tool_input.get("command", ""),
                            summary=tool_input.get("description"),
                            evidence=evidence,
                        ))
                    elif name in CLAUDE_FILE_TOOLS_WRITE:
                        events_out.append(make_event(
                            dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                            "file_write", tool_name=name,
                            file_path=tool_input.get("file_path") or tool_input.get("notebook_path"),
                            prompt_or_text=json.dumps(tool_input, ensure_ascii=False)[:400],
                            evidence=evidence,
                        ))
                    elif name in CLAUDE_FILE_TOOLS_READ:
                        events_out.append(make_event(
                            dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                            "file_read", tool_name=name,
                            file_path=tool_input.get("file_path") or tool_input.get("pattern"),
                            evidence=evidence,
                        ))
                    else:
                        summary_bits = []
                        for k in ("description", "prompt", "query", "subagent_type"):
                            if tool_input.get(k):
                                summary_bits.append(f"{k}={tool_input.get(k)}")
                        events_out.append(make_event(
                            dt_to_iso(ts2), "claude_code", session_id, project_path_guess,
                            "tool_call", tool_name=name,
                            prompt_or_text="; ".join(summary_bits) if summary_bits else None,
                            evidence=evidence,
                        ))
            continue

        # queue-operation with "content" holds the raw prompt text too; skip -
        # it duplicates the "user" record and adds no new information.
        # system/mode/title/attachment records are metadata, not analyzable
        # work content -> skipped for this MVP.

    parsed_files_log.append({"file": str(path), "source": "claude_code",
                              "lines": n_lines, "parse_errors": line_errors})


# --------------------------------------------------------------------------
# Parser: Claude Code global prompt history (~/.claude/history.jsonl)
# --------------------------------------------------------------------------

def parse_claude_history(path: Path, cutoff_dt: datetime, known_session_ids,
                          parsed_files_log, events_out):
    n_lines = 0
    line_errors = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    for line_no, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        n_lines += 1
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            line_errors += 1
            continue
        session_id = rec.get("sessionId")
        if session_id in known_session_ids:
            continue  # already captured with full fidelity from the transcript
        ts_ms = rec.get("timestamp")
        if not ts_ms:
            continue
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        if dt < cutoff_dt:
            continue
        display = rec.get("display", "")
        if not display.strip() or is_noise_prompt(display):
            continue
        events_out.append(make_event(
            dt_to_iso(dt), "claude_code", session_id or "unknown",
            rec.get("project"), "agent_prompt", prompt_or_text=display,
            evidence={"file": str(path), "line": line_no, "session_id": session_id},
        ))
    parsed_files_log.append({"file": str(path), "source": "claude_code",
                              "lines": n_lines, "parse_errors": line_errors})


# --------------------------------------------------------------------------
# Parser: Codex rollout sessions (~/.codex/sessions/**, archived_sessions/**)
# --------------------------------------------------------------------------

CODEX_COMMAND_FN_NAMES = {"exec_command", "shell", "local_shell_call", "write_stdin"}
CODEX_PATCH_FN_NAMES = {"apply_patch"}

_PATCH_FILE_RE = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete) File:\s*(.+)$", re.MULTILINE)


def extract_patch_files(patch_text):
    if not patch_text:
        return []
    return [m.strip() for m in _PATCH_FILE_RE.findall(patch_text)][:10]


def parse_codex_rollout(path: Path, cutoff_dt: datetime, parsed_files_log, events_out):
    session_id = None
    project_path_guess = None
    n_lines = 0
    line_errors = 0
    call_id_to_name = {}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return

    for line_no, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        n_lines += 1
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            line_errors += 1
            continue

        rtype = rec.get("type")
        ts = parse_iso(rec.get("timestamp"))
        payload = rec.get("payload", {}) if isinstance(rec.get("payload"), dict) else {}

        if rtype == "session_meta":
            session_id = payload.get("session_id") or payload.get("id") or path.stem
            project_path_guess = payload.get("cwd") or project_path_guess
            continue
        if rtype == "turn_context":
            project_path_guess = payload.get("cwd") or project_path_guess
            continue

        if ts is None or ts < cutoff_dt:
            continue
        if session_id is None:
            session_id = path.stem

        evidence = {"file": str(path), "line": line_no, "session_id": session_id}

        if rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "user_message":
                text = payload.get("message", "")
                if text and text.strip() and not is_noise_prompt(text):
                    events_out.append(make_event(
                        dt_to_iso(ts), "codex", session_id, project_path_guess,
                        "agent_prompt", prompt_or_text=text, evidence=evidence,
                    ))
            elif ptype == "agent_message":
                text = payload.get("message", "")
                if text and text.strip():
                    events_out.append(make_event(
                        dt_to_iso(ts), "codex", session_id, project_path_guess,
                        "agent_response", prompt_or_text=text, evidence=evidence,
                    ))
            elif ptype == "patch_apply_end":
                ok = payload.get("success", payload.get("ok", True))
                events_out.append(make_event(
                    dt_to_iso(ts), "codex", session_id, project_path_guess,
                    "file_write" if ok else "error", tool_name="apply_patch",
                    summary="patch applied" if ok else "patch failed",
                    evidence=evidence,
                ))
            elif ptype == "mcp_tool_call_end":
                invocation = payload.get("invocation", {}) or {}
                tool = invocation.get("tool", "mcp_tool")
                result = payload.get("result", {}) or {}
                is_error = "Err" in result or "error" in result
                args = invocation.get("arguments", {}) or {}
                events_out.append(make_event(
                    dt_to_iso(ts), "codex", session_id, project_path_guess,
                    "error" if is_error else "tool_call", tool_name=tool,
                    prompt_or_text=json.dumps(args, ensure_ascii=False)[:400],
                    evidence=evidence,
                ))
            elif ptype in ("task_started", "task_complete", "turn_aborted",
                            "context_compacted", "thread_rolled_back", "token_count",
                            "web_search_end"):
                pass  # bookkeeping only, not analyzable work content
            continue

        if rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "function_call":
                name = payload.get("name", "unknown_tool")
                call_id = payload.get("call_id")
                if call_id:
                    call_id_to_name[call_id] = name
                try:
                    args = json.loads(payload.get("arguments", "{}"))
                except Exception:
                    args = {}
                if name in CODEX_COMMAND_FN_NAMES:
                    cmd = args.get("cmd") or args.get("command")
                    if isinstance(cmd, list):
                        cmd = " ".join(str(c) for c in cmd)
                    events_out.append(make_event(
                        dt_to_iso(ts), "codex", session_id, project_path_guess,
                        "command_run", tool_name=name, command=cmd,
                        evidence=evidence,
                    ))
                elif name in CODEX_PATCH_FN_NAMES:
                    patch_text = args.get("patch") or args.get("input") or ""
                    files = extract_patch_files(patch_text)
                    events_out.append(make_event(
                        dt_to_iso(ts), "codex", session_id, project_path_guess,
                        "file_write", tool_name=name,
                        file_path=files[0] if files else None,
                        summary=f"{len(files)} file(s) patched" if files else None,
                        evidence={**evidence, "files": files},
                    ))
                else:
                    summary_bits = []
                    for k in ("query", "description", "prompt"):
                        if isinstance(args.get(k), str):
                            summary_bits.append(f"{k}={args.get(k)}")
                    events_out.append(make_event(
                        dt_to_iso(ts), "codex", session_id, project_path_guess,
                        "tool_call", tool_name=name,
                        prompt_or_text="; ".join(summary_bits) if summary_bits else None,
                        evidence=evidence,
                    ))
            elif ptype == "function_call_output":
                out_text = payload.get("output", "")
                if isinstance(out_text, str) and re.search(r"error|exception|traceback", out_text, re.IGNORECASE):
                    events_out.append(make_event(
                        dt_to_iso(ts), "codex", session_id, project_path_guess,
                        "error", prompt_or_text=out_text, evidence=evidence,
                    ))
            # message/reasoning/tool_search_* items are either duplicated by
            # event_msg (message) or not analyzable work content (reasoning,
            # tool search) -> skipped for this MVP.
            continue

    parsed_files_log.append({"file": str(path), "source": "codex",
                              "lines": n_lines, "parse_errors": line_errors})


# --------------------------------------------------------------------------
# Parser: Browser Activity Logger export bundles (Chrome extension JSON)
# --------------------------------------------------------------------------
#
# The companion Chrome extension (browser-activity-logger/) records the user's
# browsing as a stream of ActivityEvents and exports them as an ExportBundle:
#   { exportedAt, schemaVersion, sessionId, settings, events: [ ... ] }
# We normalize those into the SAME common event schema as Claude/Codex so the
# rest of the pipeline (categorize -> segment -> stitch -> routines ->
# report) treats browsing as just another work source.
#
# Each browser event carries its own `domain`; we put that in `project_path`
# so the domain acts like a "repository" for grouping/segmentation, and feed
# it to the category classifier as a strong hint (see _DOMAIN_CATEGORY_HINTS).

# Event types treated as intent-bearing "turn heads" (each becomes its own
# activity with a readable title). Everything else is attached as evidence.
_BROWSER_HEAD_TYPES = {"search_query", "page_view"}
# Low-level replay/navigation noise: dropped entirely (no analytical value,
# would flood the timeline).
_BROWSER_SKIP_TYPES = {"scroll", "key_down", "tab_activated"}


def _clean_url(url):
    """Return scheme://host/path, dropping the query string and fragment.
    URL query params routinely carry tokens / share keys / PII (e.g. the
    real export contained luma.com/...?tk=wQmTA5), so we never surface them."""
    if not url:
        return url
    url = url.split("#", 1)[0]
    url = url.split("?", 1)[0]
    return url


def _looks_like_browser_export(path: Path) -> bool:
    """Cheap pre-check: peek at the head of the file for the two marker keys
    before committing to a full json.load (avoids loading unrelated JSON)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return False
    return '"schemaVersion"' in head and '"events"' in head


def find_browser_exports(cutoff_dt: datetime, extra_paths):
    cutoff_ts = cutoff_dt.timestamp()
    found = []
    seen = set()

    def consider(fpath: Path):
        try:
            rp = str(fpath.resolve())
        except OSError:
            return
        if rp in seen:
            return
        if is_secret_path(fpath) or fpath.suffix.lower() != ".json":
            return
        try:
            st = fpath.stat()
        except OSError:
            return
        if st.st_size == 0 or st.st_size > MAX_FILE_SIZE_BYTES:
            return
        # export file mtime is a coarse pre-filter; individual events are still
        # filtered by their own timestamp inside the parser.
        if st.st_mtime < cutoff_ts:
            return
        if not _looks_like_browser_export(fpath):
            return
        seen.add(rp)
        found.append(fpath)

    for p in extra_paths or []:
        pth = Path(p).expanduser()
        if pth.is_dir():
            for f in sorted(pth.glob("*.json")):
                consider(f)
        elif pth.exists():
            # explicit file paths bypass the mtime window on purpose
            try:
                rp = str(pth.resolve())
            except OSError:
                rp = None
            if rp and rp not in seen and _looks_like_browser_export(pth):
                seen.add(rp)
                found.append(pth)

    for d in BROWSER_EXPORT_DIRS:
        if not d.exists() or not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            consider(f)
    return found


def parse_browser_export(path: Path, cutoff_dt: datetime, parsed_files_log, events_out):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            bundle = json.load(f)
    except (OSError, json.JSONDecodeError):
        parsed_files_log.append({"file": str(path), "source": "browser",
                                  "lines": 0, "parse_errors": 1})
        return

    events = bundle.get("events") if isinstance(bundle, dict) else None
    if not isinstance(events, list):
        parsed_files_log.append({"file": str(path), "source": "browser",
                                  "lines": 0, "parse_errors": 1})
        return

    n = 0
    kept = 0
    for idx, ev in enumerate(events):
        n += 1
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype in _BROWSER_SKIP_TYPES:
            continue
        ts = parse_iso(ev.get("timestamp"))
        if ts is None or ts < cutoff_dt:
            continue

        session_id = ev.get("sessionId") or bundle.get("sessionId") or path.stem
        domain = ev.get("domain") or ""
        title = ev.get("title") or ""
        url = _clean_url(ev.get("url") or "")
        evidence = {"file": str(path), "index": idx, "session_id": session_id,
                     "event_type": etype}

        if etype == "search_query":
            q = ev.get("searchQuery") or ""
            engine = ev.get("searchEngine") or domain
            events_out.append(make_event(
                dt_to_iso(ts), "browser", session_id, domain,
                "agent_prompt", prompt_or_text=f"[検索] {q}（{engine}）",
                summary=f"search: {truncate(q, 80)}", file_path=url,
                evidence=evidence,
            ))
            kept += 1
        elif etype == "page_view":
            events_out.append(make_event(
                dt_to_iso(ts), "browser", session_id, domain,
                "agent_prompt", prompt_or_text=f"[閲覧] {title}",
                summary=domain, file_path=url, evidence=evidence,
            ))
            kept += 1
        elif etype == "tab_updated":
            events_out.append(make_event(
                dt_to_iso(ts), "browser", session_id, domain,
                "tool_call", tool_name="browser_navigate",
                prompt_or_text=title or url, file_path=url,
                summary=f"navigate: {domain}", evidence=evidence,
            ))
            kept += 1
        elif etype == "click":
            el = ev.get("click") or {}
            what = el.get("text") or el.get("ariaLabel") or el.get("nearbyText") or el.get("href") or el.get("selector") or ""
            events_out.append(make_event(
                dt_to_iso(ts), "browser", session_id, domain,
                "tool_call", tool_name="browser_click",
                prompt_or_text=f"クリック: {truncate(what, 80)}（{domain}）",
                summary=truncate(what, 80), file_path=url, evidence=evidence,
            ))
            kept += 1
        elif etype in ("input", "change"):
            el = ev.get("input") or {}
            field = el.get("label") or el.get("ariaLabel") or el.get("placeholder") or el.get("name") or el.get("type") or "入力欄"
            # inputValue is only present when the extension deemed it safe to
            # persist; make_event still runs it through redaction as a backstop.
            val = ev.get("inputValue")
            body = f"入力[{field}]: {val}" if val else f"入力[{field}]（内容は非保存）"
            events_out.append(make_event(
                dt_to_iso(ts), "browser", session_id, domain,
                "tool_call", tool_name="browser_input",
                prompt_or_text=body, summary=f"input: {field}",
                file_path=url, evidence=evidence,
            ))
            kept += 1
        elif etype == "form_submit":
            fields = ev.get("formFields") or []
            names = [f.get("label") or f.get("name") or f.get("type") for f in fields if isinstance(f, dict)]
            names = [x for x in names if x][:6]
            events_out.append(make_event(
                dt_to_iso(ts), "browser", session_id, domain,
                "tool_call", tool_name="browser_form_submit",
                prompt_or_text=f"フォーム送信（{domain}）: " + ", ".join(names),
                summary=f"submit: {domain}", file_path=url, evidence=evidence,
            ))
            kept += 1

    parsed_files_log.append({"file": str(path), "source": "browser",
                              "lines": n, "parse_errors": 0, "kept_events": kept})


# --------------------------------------------------------------------------
# Parser: markdown instruction / memory docs (CLAUDE.md, AGENTS.md, MEMORY.md)
# --------------------------------------------------------------------------

def parse_markdown_doc(path: Path, cutoff_dt: datetime, parsed_files_log, events_out, source):
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    mtime_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    if mtime_dt < cutoff_dt:
        return
    project_path = str(path.parent)
    events_out.append(make_event(
        dt_to_iso(mtime_dt), source, f"doc:{path.name}", project_path,
        "config_or_instruction", prompt_or_text=text,
        summary=f"{path.name} (timestamp = file mtime, not an authored event time)",
        evidence={"file": str(path), "timestamp_source": "file_mtime"},
    ))
    parsed_files_log.append({"file": str(path), "source": source, "lines": 1, "parse_errors": 0})


# --------------------------------------------------------------------------
# Category classification (rule-based; swap-in point for an LLM classifier)
# --------------------------------------------------------------------------
#
# `classify_category()` is intentionally a pure function of plain-text
# signals (prompt text, file paths, commands, repo name) with no state and
# no I/O, so it can be replaced later by a call to a local or remote LLM
# without touching any caller. Callers only depend on the (category,
# confidence) return shape.

CATEGORY_KEYWORDS = {
    "coding": [
        "実装", "コード", "コーディング", "修正して", "リファクタ", "機能追加", "関数", "クラス",
        "component", "implement", "refactor", "function", "endpoint", "frontend",
        "backend", "プラグイン", "スクリプト", "コミット", "commit", "pull request", "pr作成",
        "ビルド", "build", "テスト実装", "unit test",
    ],
    "debugging": [
        "エラー", "デバッグ", "直して", "落ちる", "動かない", "直す", "バグ", "例外", "fix",
        "bug", "crash", "stack trace", "traceback", "failed", "失敗", "原因調査",
        "warning", "落ちた",
    ],
    "research": [
        "調査", "調べて", "リサーチ", "research", "検索", "情報収集", "比較して",
        "サーベイ", "survey", "競合", "事例", "ドキュメントを読んで", "仕様確認",
        "閲覧",
    ],
    "documentation": [
        "ドキュメント", "readme", "議事録", "まとめて", "報告書", "レポート", "マニュアル",
        "仕様書", "doc", "spec", "手順書", "議事", "要約して",
    ],
    "accounting": [
        "請求書", "経費", "見積", "会計", "invoice", "支払い", "領収書", "予算", "精算",
        "仕訳", "税",
    ],
    "customer_support": [
        "問い合わせ", "サポート", "返信して", "カスタマー", "クレーム", "customer",
        "ticket", "対応して", "顧客対応",
    ],
    "sales": [
        "営業", "提案書", "商談", "見込み客", "sales", "lead", "顧客獲得", "契約獲得",
        "受注",
    ],
    "project_management": [
        "タスク管理", "スケジュール", "進捗", "プロジェクト管理", "ガントチャート",
        "backlog", "スプリント", "マイルストーン", "優先順位",
    ],
    "design": [
        "デザイン", " ui ", "ux", "画面設計", "レイアウト", "figma", "デザイン案",
        "モックアップ", "配色",
    ],
    "data_entry": [
        "データ入力", "転記", "スプレッドシート入力", "フォーム記入", "csv入力", "台帳",
    ],
    "communication": [
        "メール", "連絡して", "slack", "返信して", "送信して", "mail", "message",
        "ドラフト作成", "下書き", "問い合わせ対応",
    ],
    "planning": [
        "計画", "企画", "立案", "方針", "戦略", "ロードマップ", "設計方針",
    ],
    "admin": [
        "事務", "申請", "手続き", "総務", "契約書", "書類作成", "様式", "提出",
    ],
}

# Order matters only as a tie-break; keep debugging before coding since an
# error-fix prompt often also contains generic coding words.
_CATEGORY_ORDER = ["debugging", "accounting", "customer_support", "sales",
                    "data_entry", "communication", "admin", "documentation",
                    "research", "design", "project_management", "planning",
                    "coding"]

_FILE_EXT_CODING_HINT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                          ".java", ".c", ".cpp", ".rb", ".swift", ".kt",
                          ".css", ".html", ".json", ".yaml", ".yml", ".sh"}
_FILE_EXT_DOC_HINT = {".md", ".markdown", ".docx", ".pdf", ".txt"}

# Domain -> business category. For browser activity the domain is by far the
# strongest signal for "what kind of work is this", so a known domain gets a
# large boost (bigger than a single keyword hit). The domain arrives via the
# classifier's `repo` argument (browser events set project_path = domain).
_DOMAIN_CATEGORY_HINTS = {
    "github.com": "coding", "gitlab.com": "coding", "bitbucket.org": "coding",
    "vercel.com": "coding", "dash.cloudflare.com": "coding",
    "developer.mozilla.org": "coding", "npmjs.com": "coding",
    "stackoverflow.com": "debugging", "stackexchange.com": "debugging",
    "docs.google.com": "documentation", "notion.so": "documentation",
    "www.notion.so": "documentation", "drive.google.com": "documentation",
    "script.google.com": "coding",
    "mail.google.com": "communication", "gmail.com": "communication",
    "slack.com": "communication", "outlook.office.com": "communication",
    "calendar.google.com": "project_management",
    "freee.co.jp": "accounting", "moneyforward.com": "accounting",
    "figma.com": "design",
    "google.com": "research", "www.google.com": "research", "bing.com": "research",
    "duckduckgo.com": "research", "chatgpt.com": "research", "claude.ai": "research",
    "youtube.com": "research", "www.youtube.com": "research",
    "luma.com": "planning", "lu.ma": "planning",
}


def classify_category(text=None, file_paths=None, commands=None, repo=None):
    """Rule-based category classifier.

    Returns (category:str, confidence:float 0..1). `confidence` reflects how
    many independent signals (keyword matches, file extensions, command
    verbs) agreed, not a calibrated probability.
    """
    text = (text or "").lower()
    file_paths = file_paths or []
    commands = commands or []
    repo = (repo or "").lower()

    scores = Counter()
    haystack = " ".join([text, repo] + [c.lower() for c in commands])

    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in haystack:
                scores[category] += 1

    # Domain hint (browser events pass their domain in `repo`). A dot in the
    # value distinguishes a domain from an ordinary repo name.
    if "." in repo and repo in _DOMAIN_CATEGORY_HINTS:
        scores[_DOMAIN_CATEGORY_HINTS[repo]] += 2.5

    for fp in file_paths:
        ext = Path(fp).suffix.lower()
        if ext in _FILE_EXT_CODING_HINT:
            scores["coding"] += 0.5
        elif ext in _FILE_EXT_DOC_HINT:
            scores["documentation"] += 0.3

    for cmd in commands:
        cmd_l = cmd.lower()
        if any(v in cmd_l for v in ("git commit", "git push", "npm run", "pytest",
                                     "go test", "yarn ", "pip install", "make ")):
            scores["coding"] += 0.5
        if any(v in cmd_l for v in ("git log", "grep", "find ", "cat ", "ls ")):
            scores["research"] += 0.2

    if not scores:
        return "unknown", 0.0

    best = max(scores.items(), key=lambda kv: (kv[1], -_CATEGORY_ORDER.index(kv[0])
               if kv[0] in _CATEGORY_ORDER else 0))
    top_score = best[1]
    total = sum(scores.values())
    confidence = min(1.0, top_score / max(1.0, total) * (0.5 + 0.1 * top_score))
    return best[0], round(confidence, 2)


# --------------------------------------------------------------------------
# Atomic activity decomposition
# --------------------------------------------------------------------------
#
# A single prompt like "請求書を作って、そのあと競合調査して、報告書にまとめて"
# should become 3 atomic activities. We split on explicit Japanese/English
# topic-transition connectors and on list-like structures (numbered /
# bulleted lines), then discard fragments too short to be a real activity.

_TRANSITION_SPLIT_RE = re.compile(
    r"(?:(?<=[。.!?\n])|^)\s*"
    r"(?:そのあと|その後|次に|それから|あと[、,]|それとは別に|別件で|別件だけど|"
    r"ついでに|あわせて|また、|なお、)\s*"
)

_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*・]|\d+[\.\)]|\(\d+\))\s+(.*)$", re.MULTILINE
)

_MIN_ACTIVITY_LEN = 6


def split_prompt_into_fragments(prompt_text):
    if not prompt_text:
        return []
    text = prompt_text.strip()

    list_items = [m.group(1).strip() for m in _LIST_ITEM_RE.finditer(text)]
    list_items = [li for li in list_items if len(li) >= _MIN_ACTIVITY_LEN]
    if len(list_items) >= 2:
        return list_items

    fragments = _TRANSITION_SPLIT_RE.split(text)
    fragments = [f.strip(" 、,\n") for f in fragments if f and len(f.strip()) >= _MIN_ACTIVITY_LEN]
    if fragments:
        return fragments
    return [text] if len(text) >= 1 else []


_FILLER_PREFIX_RE = re.compile(
    r"^(?:えっと[、,]?\s*|えーと[、,]?\s*|あの[、,ー]?\s*|その[、,]?\s*|まあ[、,]?\s*|"
    r"なんか[、,]?\s*|ちょっと[、,]?\s*|えー+[、,]?\s*|うーん[、,]?\s*|あー[、,]?\s*|"
    r"well[,]?\s+|so[,]?\s+|um+[,]?\s+|uh+[,]?\s+|like[,]?\s+)+",
    re.IGNORECASE,
)
# Japanese sentence enders always count; a Latin "." only ends a sentence when
# it's followed by whitespace or end-of-string, so "github.com" / "config.ts"
# are not mistaken for a sentence boundary.
_SENTENCE_END_RE = re.compile(r"[。!?！？]|\.(?=\s|$)")


def make_label(fragment, max_len=40):
    label = re.sub(r"\s+", " ", fragment).strip()
    # Strip conversational filler ("えっと、", "なんか", "well,") from the
    # front — it survives the atomic-activity split verbatim and otherwise
    # becomes the first thing a reader sees in every title/step/flow label.
    prev = None
    while label != prev:
        prev = label
        label = _FILLER_PREFIX_RE.sub("", label).strip()
    if len(label) <= max_len:
        return label
    # Prefer cutting at the first sentence boundary if it's not much further
    # out than max_len, so the label reads as a complete clause rather than
    # an arbitrary mid-word slice.
    m = _SENTENCE_END_RE.search(label)
    if m and m.start() <= max_len + 20:
        return label[:m.start() + 1].strip()
    cut = label[:max_len]
    return cut.rstrip("、,.， ") + "…"


def evidence_hint(files_touched, commands_run, outputs):
    """A short, concrete "what actually happened" hint — the file/command/
    output evidence a segment accumulated — meant to sit alongside a title
    built from the user's own (often vague, mid-thought) phrasing, e.g. "ssh
    config のファイルvs codeで開いて欲しい。場所がわからん" reads much clearer
    as "...場所がわからん 《対象: config》" once the actual file involved is
    visible in the title itself, not just buried in a details sub-line."""
    if outputs:
        names = [Path(o).name if ("/" in o or "\\" in o) else o for o in outputs[:2]]
        return "成果物: " + ", ".join(names)
    if files_touched:
        return "対象: " + ", ".join(Path(f).name for f in files_touched[:2])
    if commands_run:
        cmd = re.sub(r"\s+", " ", commands_run[0]).strip()
        cmd = cmd if len(cmd) <= 36 else cmd[:36].rstrip() + "…"
        return "実行: " + cmd
    return None


def build_display_title(base_label, files_touched, commands_run, outputs, max_len=64):
    hint = evidence_hint(files_touched, commands_run, outputs)
    if not hint:
        return base_label
    combined = f"{base_label} 《{hint}》"
    if len(combined) <= max_len:
        return combined
    # keep the evidence hint intact (it's the part answering "what
    # happened?") and shorten the label portion instead of truncating blind.
    budget = max(10, max_len - len(hint) - 5)
    short_label = base_label[:budget].rstrip("、,.， ") + "…" if len(base_label) > budget else base_label
    return f"{short_label} 《{hint}》"


# --------------------------------------------------------------------------
# Small text-similarity helpers (CJK-aware; no external NLP deps)
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def char_bigrams(s):
    s = re.sub(r"\s+", "", s or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def word_set(s):
    return {w.lower() for w in _WORD_RE.findall(s or "") if len(w) > 2}


def jaccard(a, b):
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def text_similarity(a, b):
    """CJK-aware similarity: max of char-bigram jaccard, word jaccard, and
    Ratcliff/Obershelp sequence-matching ratio (stdlib `difflib`).

    Three independent signals combined via max() rather than one metric:
    char-bigrams are the standard approach for un-tokenized CJK text (used by
    Lucene/Elasticsearch's CJK analyzers, since Japanese has no whitespace
    word boundaries and average word length is ~2 chars); word-jaccard
    covers Latin/English tokens (commands, file names, identifiers) that
    bigram overlap under-weights; SequenceMatcher additionally rewards
    order-preserving near-duplicates (e.g. "タスク管理" vs "タスク管理機能")
    that a bag-of-bigrams/words comparison can miss. Taking the max of
    several ratio signals is itself an established pattern (e.g. RapidFuzz's
    WRatio blends multiple ratio functions the same way).
    """
    a, b = a or "", b or ""
    return max(jaccard(char_bigrams(a), char_bigrams(b)),
               jaccard(word_set(a), word_set(b)),
               SequenceMatcher(None, a, b).ratio())


def similarity_meets_guard(a, b, threshold):
    """True if `a`/`b` clear the similarity threshold AND pass two guards
    against a documented failure mode of fixed-threshold short-string
    similarity: length bias, where two short strings can hit a high Jaccard
    ratio from just one or two shared bigrams out of a tiny union.

    - minimum absolute bigram-overlap count (not just a ratio)
    - bounded length ratio between the two strings (cf. Jiang & Li-style
      length filters used in string-similarity join literature)
    """
    a, b = a or "", b or ""
    if text_similarity(a, b) < threshold:
        return False
    bg_a, bg_b = char_bigrams(a), char_bigrams(b)
    if min(len(bg_a), len(bg_b)) > 2 and len(bg_a & bg_b) < 2:
        return False
    la, lb = len(a), len(b)
    if la and lb and min(la, lb) / max(la, lb) < 0.25:
        return False
    return True


def local_date_key(iso_ts):
    dt = parse_iso(iso_ts)
    if dt is None:
        return "unknown"
    return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d")


def local_time_str(iso_ts):
    dt = parse_iso(iso_ts)
    if dt is None:
        return "?"
    return dt.astimezone(DISPLAY_TZ).strftime("%H:%M")


def local_dt(iso_ts):
    dt = parse_iso(iso_ts)
    return dt.astimezone(DISPLAY_TZ) if dt else None


_EXPLICIT_NEW_TOPIC_RE = re.compile(
    "|".join([
        "ところで", "話は変わりますが", "全然関係ないけど", "別の話ですが",
        "それとは別に", "別件ですが", "別件で", "話変わって",
    ])
)


# --------------------------------------------------------------------------
# Step 1: build atomic activities from agent_prompt "turns"
# --------------------------------------------------------------------------

def build_turns(session_events):
    """session_events: chronologically sorted events for one (source, session).
    Returns list of turns: {prompt_event, evidence_events:[...]} where a turn
    starts at an agent_prompt and absorbs everything up to the next one."""
    turns = []
    current = None
    for ev in session_events:
        if ev["event_type"] == "agent_prompt":
            if current is not None:
                turns.append(current)
            current = {"prompt_event": ev, "evidence_events": []}
        else:
            if current is None:
                # evidence with no preceding captured prompt in this window;
                # still worth keeping as its own minimal turn so work isn't lost
                current = {"prompt_event": None, "evidence_events": []}
            current["evidence_events"].append(ev)
    if current is not None:
        turns.append(current)
    return turns


def build_activities_from_turn(turn, project_path, repository, source, session_id):
    prompt_event = turn["prompt_event"]
    evidence_events = turn["evidence_events"]

    files_touched = []
    commands_run = []
    errors = []
    outputs = []
    for ev in evidence_events:
        if ev["event_type"] in ("file_write", "file_read") and ev.get("file_path"):
            if ev["file_path"] not in files_touched:
                files_touched.append(ev["file_path"])
        if ev["event_type"] == "command_run" and ev.get("command"):
            commands_run.append(ev["command"])
        if ev["event_type"] == "error":
            errors.append({"event_id": ev["event_id"], "summary": ev.get("summary") or truncate(ev.get("prompt_or_text"), 200)})
        if ev["event_type"] == "file_write":
            outputs.append(ev.get("file_path") or ev.get("summary") or "file written")

    if prompt_event is None:
        start_time = evidence_events[0]["timestamp"] if evidence_events else None
        end_time = evidence_events[-1]["timestamp"] if evidence_events else start_time
        if start_time is None:
            return []
        # Prefer a human-readable label from the first evidence event's own
        # text/summary (e.g. a browser navigate carries the page title) before
        # falling back to the bare tool/event-type name.
        first = evidence_events[0]
        readable = first.get("prompt_or_text") or first.get("summary")
        text_for_class = " ".join(e.get("prompt_or_text") or e.get("summary") or "" for e in evidence_events[:8])
        if readable and readable.strip():
            fake_label = readable.strip()
        else:
            fake_label = "(内容未取得) " + (first.get("tool_name") or first["event_type"])
        category, conf = classify_category(text_for_class, files_touched, commands_run, repository)
        activity = {
            "activity_id": uuid.uuid4().hex[:12],
            "label": make_label(fake_label),
            "category": category,
            "intent": fake_label,
            "related_files": files_touched,
            "related_commands": commands_run,
            "related_project": project_path,
            "start_time": start_time,
            "end_time": end_time,
            "evidence_event_ids": [ev["event_id"] for ev in evidence_events],
            "confidence": round(conf * 0.5, 2),
            "source": source,
            "source_session_id": session_id,
            "errors": errors,
            "outputs": outputs,
        }
        return [activity]

    prompt_text = prompt_event.get("prompt_or_text") or ""
    fragments = split_prompt_into_fragments(prompt_text)
    if not fragments:
        fragments = [prompt_text or "(empty prompt)"]
    # Guard against the splitter emitting the same fragment twice (e.g. a
    # numbered-list regex matching overlapping spans).
    seen_fragments = set()
    deduped = []
    for frag in fragments:
        key = re.sub(r"\s+", "", frag).lower()
        if key in seen_fragments:
            continue
        seen_fragments.add(key)
        deduped.append(frag)
    fragments = deduped

    start_time = prompt_event["timestamp"]
    end_time = evidence_events[-1]["timestamp"] if evidence_events else start_time
    multi = len(fragments) > 1
    all_evidence_ids = [prompt_event["event_id"]] + [ev["event_id"] for ev in evidence_events]

    activities = []
    for frag in fragments:
        category, conf = classify_category(frag, files_touched, commands_run, repository)
        base_conf = 0.55 if multi else 0.85
        activities.append({
            "activity_id": uuid.uuid4().hex[:12],
            "label": make_label(frag),
            "category": category,
            "intent": truncate(frag, 300),
            "related_files": files_touched,
            "related_commands": commands_run,
            "related_project": project_path,
            "start_time": start_time,
            "end_time": end_time,
            "evidence_event_ids": all_evidence_ids,
            "confidence": round(min(1.0, base_conf * (0.5 + conf)), 2),
            "source": source,
            "source_session_id": session_id,
            "errors": errors,
            "outputs": outputs,
        })
    return activities


# --------------------------------------------------------------------------
# Step 2: task segment building (merge consecutive same-task activities)
# --------------------------------------------------------------------------

SEGMENT_MERGE_GAP_MINUTES = 90


def build_segments_for_session(activities):
    """activities already sorted by start_time for one session."""
    segments = []
    current = None
    for act in activities:
        if current is None:
            current = _new_segment(act)
            continue

        gap_minutes = None
        prev_end = parse_iso(current["end_time"])
        this_start = parse_iso(act["start_time"])
        if prev_end and this_start:
            gap_minutes = (this_start - prev_end).total_seconds() / 60.0

        explicit_new_topic = bool(_EXPLICIT_NEW_TOPIC_RE.search(act.get("intent") or ""))
        same_category = act["category"] == current["category"]
        file_overlap = bool(set(act["related_files"]) & set(current["files_touched"]))
        sim = text_similarity(act.get("intent", ""), current["title"])

        continues = (
            not explicit_new_topic and
            gap_minutes is not None and gap_minutes <= SEGMENT_MERGE_GAP_MINUTES and
            (same_category or file_overlap or sim > 0.15)
        )

        if continues:
            _extend_segment(current, act)
        else:
            segments.append(current)
            current = _new_segment(act)

    if current is not None:
        segments.append(current)
    return segments


def _new_segment(act):
    return {
        "segment_id": uuid.uuid4().hex[:12],
        "title": act["label"],
        "category": act["category"],
        "start_time": act["start_time"],
        "end_time": act["end_time"],
        "source": act["source"],
        "source_session_id": act["source_session_id"],
        "project_path": act["related_project"],
        "activities": [act["activity_id"]],
        "activity_objs": [act],
        "files_touched": list(act["related_files"]),
        "commands_run": list(act["related_commands"]),
        "errors": list(act["errors"]),
        "outputs": list(act["outputs"]),
        "confidences": [act["confidence"]],
        "evidence_event_ids": list(act["evidence_event_ids"]),
    }


def _extend_segment(seg, act):
    seg["end_time"] = act["end_time"]
    seg["activities"].append(act["activity_id"])
    seg["activity_objs"].append(act)
    for f in act["related_files"]:
        if f not in seg["files_touched"]:
            seg["files_touched"].append(f)
    seg["commands_run"].extend(act["related_commands"])
    seg["errors"].extend(act["errors"])
    seg["outputs"].extend(act["outputs"])
    seg["confidences"].append(act["confidence"])
    seg["evidence_event_ids"].extend(act["evidence_event_ids"])
    # category of a segment = majority vote across its activities so far
    cats = Counter(a["category"] for a in seg["activity_objs"])
    seg["category"] = cats.most_common(1)[0][0]


def finalize_segment(seg):
    start = parse_iso(seg["start_time"])
    end = parse_iso(seg["end_time"])
    duration = max(1.0, (end - start).total_seconds() / 60.0) if start and end else 1.0
    repository = Path(str(seg["project_path"]).rstrip("/")).name if seg["project_path"] else None
    outputs_dedup = list(dict.fromkeys(seg["outputs"]))[:10]
    display_title = build_display_title(seg["title"], seg["files_touched"], seg["commands_run"], outputs_dedup)
    return {
        "segment_id": seg["segment_id"],
        "title": display_title,
        "category": seg["category"],
        "start_time": seg["start_time"],
        "end_time": seg["end_time"],
        "duration_minutes": round(duration, 1),
        "source": seg["source"],
        "source_session_id": seg["source_session_id"],
        "project_path": seg["project_path"],
        "repository": repository,
        "activities": seg["activities"],
        "files_touched": seg["files_touched"],
        "commands_run": seg["commands_run"][:20],
        "errors": seg["errors"][:10],
        "outputs": outputs_dedup,
        "confidence": round(sum(seg["confidences"]) / len(seg["confidences"]), 2) if seg["confidences"] else 0.0,
        "evidence_event_ids": list(dict.fromkeys(seg["evidence_event_ids"]))[:40],
    }


# --------------------------------------------------------------------------
# Step 3: task stitching — re-merge segments split across time/sessions that
# belong to the same underlying task (e.g. morning fix + afternoon PR)
# --------------------------------------------------------------------------

STITCH_TITLE_SIM_THRESHOLD = 0.30


def _segments_should_stitch(a, b):
    # Union-find over pairwise matches is single-linkage clustering, whose
    # documented weakness is "chaining" (A~B~C transitively merges dissimilar
    # A and C through an intermediary). Requiring the same category AND the
    # same repo/project as a hard gate before considering title/file overlap
    # keeps any chain confined to one project's own history, which is the
    # scope this feature is meant to cover (re-uniting split work on the same
    # task), so unrelated projects can never chain together.
    if a["segment_id"] == b["segment_id"]:
        return False
    if a["category"] != b["category"]:
        return False
    same_repo = bool(a["repository"]) and a["repository"] == b["repository"]
    same_project = bool(a["project_path"]) and a["project_path"] == b["project_path"]
    if not (same_repo or same_project):
        return False
    file_overlap = bool(set(a["files_touched"]) & set(b["files_touched"]))
    return file_overlap or similarity_meets_guard(a["title"], b["title"], STITCH_TITLE_SIM_THRESHOLD)


class UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx


def stitch_segments_into_tasks(segments):
    uf = UnionFind([s["segment_id"] for s in segments])
    # group by repository first to keep this roughly O(n * bucket_size)
    # instead of O(n^2) across the whole week.
    by_repo = defaultdict(list)
    for s in segments:
        key = s["repository"] or s["project_path"] or "unknown"
        by_repo[key].append(s)

    for bucket in by_repo.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                if _segments_should_stitch(bucket[i], bucket[j]):
                    uf.union(bucket[i]["segment_id"], bucket[j]["segment_id"])

    groups = defaultdict(list)
    for s in segments:
        groups[uf.find(s["segment_id"])].append(s)

    merged_tasks = []
    for group_segments in groups.values():
        group_segments.sort(key=lambda s: s["start_time"] or "")
        titles = Counter(s["title"] for s in group_segments)
        representative_title = max(group_segments, key=lambda s: s["duration_minutes"])["title"]
        categories = Counter(s["category"] for s in group_segments)
        files = []
        commands = []
        errors = []
        outputs = []
        activities = []
        evidence_ids = []
        for s in group_segments:
            for f in s["files_touched"]:
                if f not in files:
                    files.append(f)
            commands.extend(s["commands_run"])
            errors.extend(s["errors"])
            outputs.extend(s["outputs"])
            activities.extend(s["activities"])
            evidence_ids.extend(s.get("evidence_event_ids", []))
        days = sorted({local_date_key(s["start_time"]) for s in group_segments if s["start_time"]})
        total_minutes = sum(s["duration_minutes"] for s in group_segments)
        merged_tasks.append({
            "task_id": uuid.uuid4().hex[:12],
            "title": representative_title,
            "category": categories.most_common(1)[0][0],
            "repository": group_segments[0]["repository"],
            "project_path": group_segments[0]["project_path"],
            "segment_ids": [s["segment_id"] for s in group_segments],
            "days": days,
            "start_time": group_segments[0]["start_time"],
            "end_time": group_segments[-1]["end_time"],
            "total_duration_minutes": round(total_minutes, 1),
            "files_touched": files,
            "commands_run": commands[:30],
            "errors": errors[:15],
            "outputs": list(dict.fromkeys(outputs))[:15],
            "activity_ids": activities,
            "evidence_event_ids": list(dict.fromkeys(evidence_ids))[:60],
            "is_stitched": len(group_segments) > 1,
            "confidence": round(sum(s["confidence"] for s in group_segments) / len(group_segments), 2),
        })
    return merged_tasks


# --------------------------------------------------------------------------
# Step 4: routine detection across the merged tasks of the whole week
# --------------------------------------------------------------------------
#
# Grouping instances of "the same routine" by title-text similarity alone is
# a lexical proxy for what is really a *behavioral* question: did these task
# instances follow the same steps? Process-mining practice (trace/variant
# clustering, e.g. Bose & van der Aalst's "Context Aware Trace Clustering",
# and the Alpha/Heuristics-Miner idea of deriving structure from
# directly-follows relations between activities) groups traces by their
# ordered step sequence instead, which is invariant to wording and sensitive
# to order — two differently-worded tasks doing the same steps cluster
# together; two similarly-worded tasks doing different steps don't. We
# approximate that here with a lightweight, dependency-free stand-in: each
# task's activities are collapsed into a run-length-encoded category
# sequence (its "shape"), and clustering uses token-level edit distance
# between shapes as a second signal alongside (guarded) title similarity.

ROUTINE_TITLE_SIM_THRESHOLD = 0.28
ROUTINE_SEQUENCE_MAX_NORM_DISTANCE = 0.45  # allow ~45% of steps to differ


def levenshtein(seq_a, seq_b):
    """Token-level edit distance (Wagner-Fischer DP) between two sequences
    of hashable tokens (here: run-length-encoded activity categories)."""
    if seq_a == seq_b:
        return 0
    la, lb = len(seq_a), len(seq_b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if seq_a[i - 1] == seq_b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def task_step_sequence(task, activity_category_by_id):
    """Run-length-encoded category sequence for a task's activities, e.g.
    [research, research, coding, coding, coding] -> [research, coding]. The
    collapse focuses on workflow *shape* (topic transitions), not raw step
    counts, which would otherwise dominate the edit-distance signal."""
    cats = [activity_category_by_id.get(aid) for aid in task.get("activity_ids", [])]
    cats = [c for c in cats if c]
    shape = []
    for c in cats:
        if not shape or shape[-1] != c:
            shape.append(c)
    return shape


def sequence_similarity(seq_a, seq_b):
    if not seq_a and not seq_b:
        return 1.0
    max_len = max(len(seq_a), len(seq_b), 1)
    return 1.0 - (levenshtein(seq_a, seq_b) / max_len)


def cluster_tasks(tasks, activity_category_by_id):
    """Nearest-fit clustering (not first-fit): compare a candidate against
    every existing cluster's centroid and join the closest one clearing the
    threshold, re-estimating the centroid periodically. First-fit-into-
    first-match is a known weakness of naive leader-style clustering
    (order-dependent, picks an arbitrary match instead of the closest one)."""
    clusters = []  # {"tasks": [...], "centroid_title": str, "centroid_seq": [...]}
    for t in tasks:
        seq = task_step_sequence(t, activity_category_by_id)
        best_idx, best_score = None, 0.0
        for i, cluster in enumerate(clusters):
            title_ok = similarity_meets_guard(t["title"], cluster["centroid_title"], ROUTINE_TITLE_SIM_THRESHOLD)
            seq_sim = sequence_similarity(seq, cluster["centroid_seq"])
            # A single-step task's "shape" is just [category] — trivially
            # identical to every other same-category single-step task, which
            # would make the sequence signal meaningless noise (any two
            # unrelated one-off questions in the same category would match).
            # Only let sequence-shape drive clustering once there's an actual
            # multi-step shape to compare.
            seq_informative = min(len(seq), len(cluster["centroid_seq"])) >= 2
            seq_ok = seq_informative and seq_sim >= (1 - ROUTINE_SEQUENCE_MAX_NORM_DISTANCE)
            if not (title_ok or seq_ok):
                continue
            score = max(text_similarity(t["title"], cluster["centroid_title"]) if title_ok else 0.0,
                        seq_sim if seq_informative else 0.0)
            if score > best_score:
                best_score, best_idx = score, i
        if best_idx is not None:
            cluster = clusters[best_idx]
            cluster["tasks"].append(t)
            if len(cluster["tasks"]) % 3 == 0:  # periodic centroid re-estimation
                titles = [x["title"] for x in cluster["tasks"]]
                cluster["centroid_title"] = Counter(titles).most_common(1)[0][0]
                seqs = [tuple(task_step_sequence(x, activity_category_by_id)) for x in cluster["tasks"]]
                cluster["centroid_seq"] = list(Counter(seqs).most_common(1)[0][0])
        else:
            clusters.append({"tasks": [t], "centroid_title": t["title"], "centroid_seq": seq})
    return [c["tasks"] for c in clusters]


def detect_routines(merged_tasks, activity_category_by_id, segment_title_by_id):
    by_category = defaultdict(list)
    for t in merged_tasks:
        by_category[t["category"]].append(t)

    routines = []
    for category, tasks in by_category.items():
        for ctasks in cluster_tasks(tasks, activity_category_by_id):
            if len(ctasks) < 2:
                continue
            routines.append(build_routine_record(category, ctasks, activity_category_by_id, segment_title_by_id))

    routines.sort(key=lambda r: r["occurrences"], reverse=True)
    return routines


def build_routine_record(category, tasks, activity_category_by_id, segment_title_by_id):
    title_counts = Counter(t["title"] for t in tasks)
    title = title_counts.most_common(1)[0][0]
    days = sorted({d for t in tasks for d in t["days"]})
    projects = sorted({t["repository"] for t in tasks if t["repository"]})

    # medoid: the task instance whose step-shape is closest to all others in
    # the cluster (real observed sequence, robust to outliers) — its actual
    # segment titles become common_steps, instead of a frequency count of
    # whole-task titles that can't represent a multi-step routine.
    seqs = [task_step_sequence(t, activity_category_by_id) for t in tasks]
    if len(tasks) == 1:
        medoid_idx = 0
        cohesion = 0.5
    else:
        total_dists = []
        pair_sims = []
        for i in range(len(tasks)):
            dist_sum = 0.0
            for j in range(len(tasks)):
                if i == j:
                    continue
                sim = sequence_similarity(seqs[i], seqs[j])
                dist_sum += (1 - sim)
                if j > i:
                    pair_sims.append(sim)
            total_dists.append(dist_sum)
        medoid_idx = min(range(len(tasks)), key=lambda i: total_dists[i])
        cohesion = sum(pair_sims) / len(pair_sims) if pair_sims else 0.5

    medoid_task = tasks[medoid_idx]
    common_steps = []
    for sid in medoid_task.get("segment_ids", []):
        step_title = segment_title_by_id.get(sid)
        if step_title and (not common_steps or common_steps[-1] != step_title):
            common_steps.append(step_title)
    if not common_steps:
        common_steps = [medoid_task["title"]]

    file_basename_counter = Counter()
    for t in tasks:
        for f in t["files_touched"]:
            file_basename_counter[Path(f).name] += 1
    common_files = [name for name, cnt in file_basename_counter.most_common(8) if cnt >= 2]
    if not common_files:
        common_files = [name for name, _ in file_basename_counter.most_common(5)]

    durations = [t["total_duration_minutes"] for t in tasks]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    automation_potential, reason = score_automation(category, tasks, durations, cohesion)

    return {
        "routine_id": uuid.uuid4().hex[:12],
        "title": title,
        "category": category,
        "occurrences": len(tasks),
        "involved_days": days,
        "involved_projects": projects,
        "common_steps": common_steps,
        "common_files_or_artifacts": common_files,
        "average_duration_minutes": round(avg_duration, 1),
        "automation_potential": automation_potential,
        "reason": reason,
        "task_ids": [t["task_id"] for t in tasks],
    }


# Roughly how "clear input -> output" this category's work tends to be, and
# how much human judgement it typically still requires. These are coarse
# priors used only to explain the automation verdict, not hard rules.
_CATEGORY_IO_CLARITY = {
    "accounting": 0.9, "data_entry": 0.9, "documentation": 0.7,
    "customer_support": 0.6, "admin": 0.7, "communication": 0.6,
    "coding": 0.55, "debugging": 0.4, "research": 0.35,
    "sales": 0.4, "design": 0.35, "project_management": 0.5,
    "planning": 0.3, "unknown": 0.3,
}
_CATEGORY_JUDGMENT_PENALTY = {
    "accounting": 0.1, "data_entry": 0.1, "documentation": 0.2,
    "customer_support": 0.35, "admin": 0.2, "communication": 0.3,
    "coding": 0.35, "debugging": 0.45, "research": 0.4,
    "sales": 0.5, "design": 0.45, "project_management": 0.35,
    "planning": 0.45, "unknown": 0.4,
}


def _modal_match_rate(sets):
    """Fraction of occurrences matching the cluster's most typical (medoid)
    set. An *average* pairwise Jaccard can't tell "3 identical + 1 wild
    outlier" apart from "4 moderately-similar occurrences" — RPA feasibility
    scorecards call this the standardization / happy-path rate (% of cases
    following the standard variant), a criterion this fills in cheaply from
    data we already have (files/commands touched per occurrence)."""
    sets = [s for s in sets if s]
    if len(sets) < 2:
        return 1.0
    # medoid = the set with the highest average similarity to all others
    best_idx, best_avg = 0, -1.0
    for i in range(len(sets)):
        sims = [jaccard(sets[i], sets[j]) for j in range(len(sets)) if j != i]
        avg = sum(sims) / len(sims) if sims else 0.0
        if avg > best_avg:
            best_avg, best_idx = avg, i
    modal = sets[best_idx]
    matches = sum(1 for s in sets if jaccard(s, modal) >= 0.7)
    return matches / len(sets)


def score_automation(category, tasks, durations, cohesion=0.5):
    occurrences = len(tasks)

    # A routine seen 4 times in one afternoon is weaker evidence of a real,
    # ongoing habit than one seen across several distinct days (real
    # recurrence vs. one exploratory burst) — dampen frequency accordingly.
    distinct_days = {d for t in tasks for d in t.get("days", [])}
    freq_score = min(1.0, occurrences / 4.0)
    if len(distinct_days) < 2:
        freq_score *= 0.6

    # Stability blends three things instead of one: average pairwise
    # file/command overlap (as before), the modal-match-rate "happy path"
    # signal (see _modal_match_rate), and step-shape cohesion from the
    # sequence clustering step (see cluster_tasks/task_step_sequence) — a
    # tighter step-sequence match across occurrences is itself evidence of a
    # standardized procedure, independent of which files/commands were used.
    file_sets = [set(t["files_touched"]) for t in tasks]
    cmd_sets = [set(t["commands_run"]) for t in tasks]
    pair_sims = []
    for i in range(len(tasks)):
        for j in range(i + 1, len(tasks)):
            pair_sims.append(jaccard(file_sets[i], file_sets[j]))
            pair_sims.append(jaccard(cmd_sets[i], cmd_sets[j]))
    avg_pairwise = (sum(pair_sims) / len(pair_sims)) if pair_sims else 0.3
    modal_match_rate = max(_modal_match_rate(file_sets), _modal_match_rate(cmd_sets))
    structural_stability = 0.4 * avg_pairwise + 0.3 * modal_match_rate + 0.3 * cohesion

    if len(durations) >= 2 and sum(durations) > 0:
        mean_d = sum(durations) / len(durations)
        variance = sum((d - mean_d) ** 2 for d in durations) / len(durations)
        cv = (variance ** 0.5) / mean_d if mean_d else 1.0
        duration_stability = max(0.0, 1 - min(1.0, cv))
    else:
        duration_stability = 0.5
    stability_score = 0.6 * structural_stability + 0.4 * duration_stability

    io_clarity = _CATEGORY_IO_CLARITY.get(category, 0.4)
    judgment_penalty = _CATEGORY_JUDGMENT_PENALTY.get(category, 0.4)

    raw = (freq_score * 0.30 + stability_score * 0.30 + io_clarity * 0.25
           - judgment_penalty * 0.15)

    reasons = []
    if freq_score >= 0.5:
        reasons.append(f"直近1週間で{occurrences}回の繰り返しを検出")
    else:
        reasons.append(f"繰り返しは{occurrences}回のみ、または特定の1日に偏っている")
    if structural_stability >= 0.4:
        reasons.append("使用ファイル/コマンド・作業手順の型が毎回似ている")
    else:
        reasons.append("毎回のやり方に差がある")
    if modal_match_rate >= 0.7:
        reasons.append("大半の回が同じ「型」に沿っている")
    if io_clarity >= 0.6:
        reasons.append("入力と出力の形が明確")
    if judgment_penalty >= 0.4:
        reasons.append("人間の判断が多く関わる領域")

    # Base tier from the weighted sum (for ranking/ordering)...
    if raw >= 0.55 and structural_stability >= 0.35 and occurrences >= 3:
        potential = "high"
    elif raw >= 0.35:
        potential = "medium"
    else:
        potential = "low"

    # ...then RPA-style gates layered on top. Published feasibility scoring
    # (vendor scorecards, Bédard et al. 2024 AHP+TOPSIS RPA-candidate study)
    # treats rule-based-ness/exception-rate as a near-veto criterion rather
    # than just one more additive term: a highly-judgment-heavy or
    # unstructured process should never be rated "high" no matter how
    # frequent/stable it looks on paper, because a bot literally cannot
    # execute an undefined judgment call.
    if potential == "high" and judgment_penalty >= 0.7:
        potential = "medium"
        reasons.append("頻度・安定性は高いが判断業務の比重が大きいため上限を「中」に抑制")
    if potential == "high" and io_clarity <= 0.35 and structural_stability < 0.5:
        potential = "medium"
        reasons.append("入出力が構造化されておらず手順も不安定なため上限を「中」に抑制")

    return potential, "。".join(reasons) + "。"


# --------------------------------------------------------------------------
# Step 5: automation / skill candidates
# --------------------------------------------------------------------------

def build_automation_candidates(routines, merged_tasks_by_id):
    candidates = []
    for r in routines:
        if r["automation_potential"] == "low":
            continue
        example_task = None
        for tid in r["task_ids"]:
            if tid in merged_tasks_by_id:
                example_task = merged_tasks_by_id[tid]
                break
        expected_input = "同じ種類の依頼文（例:「" + r["title"] + "」に類似した指示）"
        if r["common_files_or_artifacts"]:
            expected_input += " と対象ファイル: " + ", ".join(r["common_files_or_artifacts"][:4])
        expected_output = ", ".join(r["common_files_or_artifacts"][:4]) or "（成果物ファイルは検出できず、応答メッセージのみ）"
        steps = r["common_steps"] or [r["title"]]
        risks = []
        if r["category"] in ("customer_support", "communication", "sales"):
            risks.append("宛先や文面の最終確認は人間が行うべき")
        if r["category"] in ("accounting", "admin"):
            risks.append("金額・法的内容の最終チェックは人間が行うべき")
        if example_task and example_task.get("errors"):
            risks.append("過去にエラーが発生した実績があるため例外処理を用意すること")
        if not risks:
            risks.append("入力パターンが今後変化した場合は自動化フローの見直しが必要")

        candidates.append({
            "routine_id": r["routine_id"],
            "title": r["title"],
            "category": r["category"],
            "why": r["reason"],
            "automation_potential": r["automation_potential"],
            "expected_input": expected_input,
            "expected_output": expected_output,
            "expected_steps": steps,
            "risks": risks,
            "occurrences": r["occurrences"],
            "involved_projects": r["involved_projects"],
        })

    order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (order.get(c["automation_potential"], 3), -c["occurrences"]))
    return candidates


# --------------------------------------------------------------------------
# Orchestration: turn raw events into segments / merged tasks / routines
# --------------------------------------------------------------------------

def analyze(events):
    events = [e for e in events if e.get("timestamp")]
    events.sort(key=lambda e: e["timestamp"])

    sessions = defaultdict(list)
    for e in events:
        sessions[(e["source"], e["source_session_id"])].append(e)

    all_activities = []
    all_segments = []
    for (source, session_id), sess_events in sessions.items():
        sess_events.sort(key=lambda e: e["timestamp"])
        project_path = next((e["project_path"] for e in sess_events if e.get("project_path")), None)
        repository = Path(str(project_path).rstrip("/")).name if project_path else None

        # config_or_instruction docs aren't part of a conversational turn;
        # keep them out of turn-building but still expose as their own
        # low-confidence activity/segment so they show up in the timeline.
        conv_events = [e for e in sess_events if e["event_type"] != "config_or_instruction"]
        doc_events = [e for e in sess_events if e["event_type"] == "config_or_instruction"]

        turns = build_turns(conv_events)
        session_activities = []
        for turn in turns:
            session_activities.extend(build_activities_from_turn(turn, project_path, repository, source, session_id))

        # Instruction/memory docs (CLAUDE.md, AGENTS.md, MEMORY.md, ...) are
        # recorded as their own low-confidence activities for evidence/
        # traceability, but are deliberately kept out of task-segment
        # building: they are background configuration, not a unit of work
        # the user asked for, and would otherwise dilute the "top projects"
        # and task counts with one-line, near-zero-duration entries.
        doc_activities = []
        for doc_ev in doc_events:
            category, conf = classify_category(doc_ev.get("prompt_or_text", ""), [], [], repository)
            doc_activities.append({
                "activity_id": uuid.uuid4().hex[:12],
                "label": make_label(Path(doc_ev["evidence"]["file"]).name + " の設定/指示内容"),
                "category": "documentation" if category == "unknown" else category,
                "intent": doc_ev.get("summary") or "instruction/memory document",
                "related_files": [doc_ev["evidence"]["file"]],
                "related_commands": [],
                "related_project": project_path,
                "start_time": doc_ev["timestamp"],
                "end_time": doc_ev["timestamp"],
                "evidence_event_ids": [doc_ev["event_id"]],
                "confidence": 0.4,
                "source": source,
                "source_session_id": session_id,
                "errors": [],
                "outputs": [],
            })

        session_activities.sort(key=lambda a: a["start_time"] or "")
        all_activities.extend(session_activities)
        all_activities.extend(doc_activities)

        raw_segments = build_segments_for_session(session_activities)
        for seg in raw_segments:
            all_segments.append(finalize_segment(seg))

    merged_tasks = stitch_segments_into_tasks(all_segments)
    merged_tasks_by_id = {t["task_id"]: t for t in merged_tasks}
    activity_category_by_id = {a["activity_id"]: a["category"] for a in all_activities}
    segment_title_by_id = {s["segment_id"]: s["title"] for s in all_segments}
    routines = detect_routines(merged_tasks, activity_category_by_id, segment_title_by_id)
    automation_candidates = build_automation_candidates(routines, merged_tasks_by_id)

    return {
        "activities": all_activities,
        "segments": all_segments,
        "merged_tasks": merged_tasks,
        "routines": routines,
        "automation_candidates": automation_candidates,
    }


def build_summary(events, segments, merged_tasks, automation_candidates, parsed_files_log, period_start, period_end):
    category_minutes = Counter()
    category_counts = Counter()
    for seg in segments:
        category_minutes[seg["category"]] += seg["duration_minutes"]
        category_counts[seg["category"]] += 1

    project_counts = Counter()
    for seg in segments:
        if seg["repository"]:
            project_counts[seg["repository"]] += 1

    source_event_counts = Counter(e["source"] for e in events)

    return {
        "period_start": dt_to_iso(period_start),
        "period_end": dt_to_iso(period_end),
        "log_files_read": len(parsed_files_log),
        "events_detected": len(events),
        "tasks_detected": len(merged_tasks),
        "segments_detected": len(segments),
        "category_minutes": dict(category_minutes.most_common()),
        "category_counts": dict(category_counts.most_common()),
        "top_projects": project_counts.most_common(8),
        "source_event_counts": dict(source_event_counts.most_common()),
        "automation_candidate_count": len(automation_candidates),
    }


# --------------------------------------------------------------------------
# HTML report rendering (single self-contained file, no external CDN/JS libs)
# --------------------------------------------------------------------------

import html as _html


def esc(s):
    return _html.escape(str(s if s is not None else ""))


CATEGORY_LABELS_JA = {
    "coding": "コーディング", "debugging": "デバッグ", "research": "リサーチ",
    "documentation": "ドキュメント作成", "accounting": "経理", "customer_support": "顧客対応",
    "sales": "営業", "project_management": "プロジェクト管理", "design": "デザイン",
    "data_entry": "データ入力", "communication": "連絡・メール", "planning": "企画",
    "admin": "事務", "unknown": "分類不明",
}

CATEGORY_COLORS = {
    "coding": "#3b82f6", "debugging": "#ef4444", "research": "#8b5cf6",
    "documentation": "#06b6d4", "accounting": "#f59e0b", "customer_support": "#ec4899",
    "sales": "#f97316", "project_management": "#10b981", "design": "#d946ef",
    "data_entry": "#84cc16", "communication": "#14b8a6", "planning": "#6366f1",
    "admin": "#78716c", "unknown": "#9ca3af",
}

AUTOMATION_COLORS = {"high": "#16a34a", "medium": "#d97706", "low": "#9ca3af"}
AUTOMATION_LABELS_JA = {"high": "自動化候補: 高", "medium": "自動化候補: 中", "low": "自動化候補: 低"}

SOURCE_LABELS_JA = {"claude_code": "Claude Code", "codex": "Codex", "browser": "ブラウザ"}


def source_label(src):
    return SOURCE_LABELS_JA.get(src, src)


def cat_badge(cat):
    color = CATEGORY_COLORS.get(cat, "#9ca3af")
    label = CATEGORY_LABELS_JA.get(cat, cat)
    return f'<span class="badge" style="background:{color}22;color:{color};border:1px solid {color}55">{esc(label)}</span>'


def auto_badge(level):
    color = AUTOMATION_COLORS.get(level, "#9ca3af")
    label = AUTOMATION_LABELS_JA.get(level, level)
    return f'<span class="badge auto-badge" style="background:{color}22;color:{color};border:1px solid {color}55">{esc(label)}</span>'


def render_summary(summary):
    top_projects = "".join(
        f"<li>{esc(name)} <span class='muted'>({cnt}件)</span></li>"
        for name, cnt in summary["top_projects"]
    ) or "<li class='muted'>検出なし</li>"

    cards = f"""
    <div class="cards">
      <div class="card"><div class="card-num">{summary['log_files_read']}</div><div class="card-label">読み込んだログファイル数</div></div>
      <div class="card"><div class="card-num">{summary['events_detected']}</div><div class="card-label">検出イベント数</div></div>
      <div class="card"><div class="card-num">{summary['segments_detected']}</div><div class="card-label">検出タスクセグメント数</div></div>
      <div class="card"><div class="card-num">{summary['tasks_detected']}</div><div class="card-label">再統合後タスク数</div></div>
      <div class="card"><div class="card-num">{summary['automation_candidate_count']}</div><div class="card-label">自動化候補数</div></div>
    </div>
    """
    period_start_local = local_dt(summary["period_start"])
    period_end_local = local_dt(summary["period_end"])
    period_str = ""
    if period_start_local and period_end_local:
        period_str = f"{period_start_local.strftime('%Y-%m-%d %H:%M')} 〜 {period_end_local.strftime('%Y-%m-%d %H:%M')} (JST)"

    source_counts = summary.get("source_event_counts", {})
    source_line = " ・ ".join(f"{source_label(s)}: {c}件" for s, c in source_counts.items()) or "—"

    return f"""
    <section id="summary">
      <h2>1. サマリー</h2>
      <p class="muted">解析対象期間: {esc(period_str)}</p>
      {cards}
      <h3>データソース別イベント数</h3>
      <p class="muted small">{esc(source_line)}</p>
      <h3>主なプロジェクト・ドメイン</h3>
      <ul class="plain-list">{top_projects}</ul>
    </section>
    """


def render_category_chart(summary):
    minutes = summary["category_minutes"]
    counts = summary["category_counts"]
    if not minutes:
        return '<section id="categories"><h2>2. カテゴリ別作業量</h2><p class="muted">データがありません。</p></section>'
    max_minutes = max(minutes.values()) or 1
    rows = []
    for cat, mins in sorted(minutes.items(), key=lambda kv: kv[1], reverse=True):
        pct = round(mins / max_minutes * 100, 1)
        color = CATEGORY_COLORS.get(cat, "#9ca3af")
        hours = mins / 60.0
        rows.append(f"""
        <div class="bar-row">
          <div class="bar-label">{cat_badge(cat)} <span class="muted">{counts.get(cat, 0)}件</span></div>
          <div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>
          <div class="bar-value">{hours:.1f} h</div>
        </div>
        """)
    return f"""
    <section id="categories">
      <h2>2. 1週間のカテゴリ別作業量</h2>
      <div class="bar-chart">{''.join(rows)}</div>
    </section>
    """


def _day_flow_chain_html(segs):
    """Compact "at a glance" chain of category badges for the day, collapsing
    consecutive repeats of the same category (e.g. coding,coding,coding,
    research -> coding -> research) so the day's overall shape reads in one
    line before drilling into the step-by-step flow below it."""
    chain = []
    for seg in segs:
        if not chain or chain[-1] != seg["category"]:
            chain.append(seg["category"])
    return '<span class="flow-arrow">→</span>'.join(cat_badge(c) for c in chain)


def render_daily_timeline(segments):
    by_day = defaultdict(list)
    for seg in segments:
        by_day[local_date_key(seg["start_time"])].append(seg)

    days_sorted = sorted(by_day.keys(), reverse=True)
    day_blocks = []
    for day in days_sorted:
        segs = sorted(by_day[day], key=lambda s: s["start_time"] or "")
        steps = []
        for i, seg in enumerate(segs, 1):
            start_t = local_time_str(seg["start_time"])
            end_t = local_time_str(seg["end_time"])
            files = ", ".join(Path(f).name for f in seg["files_touched"][:5]) or "—"
            outputs = ", ".join(seg["outputs"][:3])
            summary_line = outputs if outputs else (seg["commands_run"][0] if seg["commands_run"] else "")
            err_flag = f'<span class="badge err-badge">エラー{len(seg["errors"])}件</span>' if seg["errors"] else ""
            color = CATEGORY_COLORS.get(seg["category"], "#9ca3af")
            steps.append(f"""
            <div class="flow-step">
              <div class="flow-dot" style="background:{color}">{i}</div>
              <div class="flow-content">
                <div class="flow-title">{esc(seg['title'])}</div>
                <div class="muted small">{esc(start_t)}–{esc(end_t)} ({seg['duration_minutes']:.0f}分) ・ {cat_badge(seg['category'])} {err_flag}</div>
                <div class="muted small">{esc(seg['repository'] or seg['project_path'] or '-')} ・ {esc(source_label(seg['source']))}</div>
                <div class="muted small">ファイル: {esc(files)}</div>
                {f'<div class="muted small">概要: {esc(truncate(summary_line, 160))}</div>' if summary_line else ''}
              </div>
            </div>
            """)
        total_minutes = sum(s["duration_minutes"] for s in segs)
        day_blocks.append(f"""
        <details class="day-block" open>
          <summary>{esc(day)} <span class="muted">（{len(segs)}ステップ, 推定 {total_minutes/60:.1f}h）</span></summary>
          <div class="day-flow-chain small">{_day_flow_chain_html(segs)}</div>
          <div class="flow day-flow">{''.join(steps)}</div>
        </details>
        """)
    return f"""
    <section id="timeline">
      <h2>3. 日別タイムライン（1日の作業の流れ）</h2>
      <p class="muted small">各日の作業を、時系列順に発生したステップとしてフロー図で表示しています。上の色帯はその日の作業カテゴリの推移（連続する同カテゴリはまとめています）、下の番号付きステップが詳細です。</p>
      {''.join(day_blocks) if day_blocks else '<p class="muted">データがありません。</p>'}
    </section>
    """


def render_task_flows(merged_tasks, segments_by_id):
    stitched = [t for t in merged_tasks if t["is_stitched"]]
    pool = stitched if stitched else merged_tasks
    top = sorted(pool, key=lambda t: t["total_duration_minutes"], reverse=True)[:5]

    if not top:
        return '<section id="flows"><h2>4. 作業フロー表示</h2><p class="muted">データがありません。</p></section>'

    note = "" if stitched else '<p class="muted">複数セグメントにまたがるタスクは検出されませんでした。所要時間の長い単発タスクを表示しています。</p>'

    blocks = []
    for t in top:
        segs = sorted([segments_by_id[sid] for sid in t["segment_ids"] if sid in segments_by_id],
                       key=lambda s: s["start_time"] or "")
        steps = []
        for i, seg in enumerate(segs, 1):
            day = local_date_key(seg["start_time"])
            tstr = local_time_str(seg["start_time"])
            steps.append(f"""
            <div class="flow-step">
              <div class="flow-dot">{i}</div>
              <div class="flow-content">
                <div class="flow-title">{esc(seg['title'])}</div>
                <div class="muted small">{esc(day)} {esc(tstr)} ・ {cat_badge(seg['category'])}</div>
              </div>
            </div>
            """)
        blocks.append(f"""
        <div class="flow-card">
          <h4>{esc(t['title'])} <span class="muted small">({esc(t['repository'] or '-')}, {t['total_duration_minutes']:.0f}分, {len(segs)}ステップ)</span></h4>
          <div class="flow">{''.join(steps)}</div>
        </div>
        """)
    return f"""
    <section id="flows">
      <h2>4. 作業フロー表示（主要タスク）</h2>
      {note}
      <div class="flow-grid">{''.join(blocks)}</div>
    </section>
    """


def render_routines(routines):
    if not routines:
        return '<section id="routines"><h2>5. 繰り返し作業パターン</h2><p class="muted">直近1週間では2回以上繰り返された作業パターンは検出されませんでした。</p></section>'
    cards = []
    for r in routines:
        steps = "".join(f"<li>{esc(s)}</li>" for s in r["common_steps"])
        files = ", ".join(r["common_files_or_artifacts"]) or "—"
        projects = ", ".join(r["involved_projects"]) or "—"
        cards.append(f"""
        <div class="routine-card">
          <div class="routine-header">
            <h4>{esc(r['title'])}</h4>
            {cat_badge(r['category'])} {auto_badge(r['automation_potential'])}
          </div>
          <div class="muted small">出現 {r['occurrences']}回 ・ {len(r['involved_days'])}日にまたがる ・ 平均所要 {r['average_duration_minutes']:.0f}分</div>
          <div class="muted small">関連プロジェクト: {esc(projects)}</div>
          <div class="muted small">関連ファイル/成果物: {esc(files)}</div>
          <div class="small">共通ステップ: <ol>{steps}</ol></div>
          <div class="small">判定理由: {esc(r['reason'])}</div>
        </div>
        """)
    return f"""
    <section id="routines">
      <h2>5. 繰り返し作業パターン</h2>
      <div class="routine-grid">{''.join(cards)}</div>
    </section>
    """


def render_automation(candidates):
    if not candidates:
        return '<section id="automation"><h2>6. スキル化・自動化候補</h2><p class="muted">現時点で自動化候補として提案できる繰り返しパターンは検出されませんでした。</p></section>'
    cards = []
    for c in candidates:
        steps = "".join(f"<li>{esc(s)}</li>" for s in c["expected_steps"])
        risks = "".join(f"<li>{esc(r)}</li>" for r in c["risks"])
        cards.append(f"""
        <div class="auto-card">
          <div class="routine-header">
            <h4>{esc(c['title'])}</h4>
            {cat_badge(c['category'])} {auto_badge(c['automation_potential'])}
          </div>
          <p class="small"><strong>なぜ候補か:</strong> {esc(c['why'])}（直近1週間で{c['occurrences']}回検出、対象: {esc(', '.join(c['involved_projects']) or '複数')}）</p>
          <p class="small"><strong>想定される入力:</strong> {esc(c['expected_input'])}</p>
          <p class="small"><strong>想定される出力:</strong> {esc(c['expected_output'])}</p>
          <p class="small"><strong>想定される手順:</strong></p>
          <ol class="small">{steps}</ol>
          <p class="small"><strong>リスク・注意点:</strong></p>
          <ul class="small">{risks}</ul>
        </div>
        """)
    return f"""
    <section id="automation">
      <h2>6. スキル化・自動化候補</h2>
      <div class="auto-grid">{''.join(cards)}</div>
    </section>
    """


def render_evidence(merged_tasks, event_index):
    rows = []
    for t in merged_tasks[:200]:
        ev_lines = []
        for eid in t.get("evidence_event_ids", [])[:8]:
            info = event_index.get(eid)
            if not info:
                continue
            ev_lines.append(f"{esc(eid)} → {esc(Path(info['file']).name)}:{info.get('line', '?')}")
        if not ev_lines:
            continue
        rows.append(f"""
        <details class="evidence-block">
          <summary>{esc(t['title'])} <span class="muted small">({esc(t['task_id'])})</span></summary>
          <ul class="small mono">{''.join(f'<li>{l}</li>' for l in ev_lines)}</ul>
        </details>
        """)
    return f"""
    <section id="evidence">
      <h2>7. 生ログへの根拠</h2>
      <p class="muted">各タスクを判定する根拠となったイベントIDと、それが記録されているログファイル名・行番号です（内容は本文中で既にマスク済み）。</p>
      {''.join(rows) if rows else '<p class="muted">データがありません。</p>'}
    </section>
    """


def render_limitations(parsed_files_log, missing_ts_count):
    file_list = "".join(f"<li>{esc(Path(f['file']).name)} ({f['source']}, {f['lines']}行, parse_errors={f['parse_errors']})</li>"
                         for f in parsed_files_log[:60])
    more = f"<li class='muted'>...他 {len(parsed_files_log) - 60} ファイル</li>" if len(parsed_files_log) > 60 else ""
    return f"""
    <section id="limitations">
      <h2>解析の限界について</h2>
      <ul>
        <li>ログ形式は非公開の内部フォーマットであり、将来のアップデートで構造が変わると本ツールのパーサーが対応できなくなる可能性があります。</li>
        <li>タイムスタンプが取得できないドキュメント系ファイル（CLAUDE.md / AGENTS.md / MEMORY.md など）は、ファイルの更新時刻（mtime）を代用の作業時刻として扱っています。実際の編集時刻とは異なる場合があります。</li>
        <li>業務カテゴリ分類・atomic activity への分解・タスクの再統合・繰り返しパターン検出は、いずれもキーワード/ファイルパス/時間差などに基づくルールベースの推定です。人間の意図を完全に正確に読み取れているわけではありません。</li>
        <li>1つのプロンプト内に複数作業が含まれる場合の証拠（ツール呼び出し・ファイル編集など）は、サブタスクへ厳密に按分できないため、そのターン内の証拠をまとめて全サブタスクに割り当てています。</li>
        <li>自動化候補の判定（high/medium/low）はヒューリスティックなスコアリングであり、実際に自動化して良いかどうかの最終判断は人間が行ってください。</li>
        <li>本ツールが認証情報・秘密情報らしきファイル/文字列と判断したものは内容を一切レポートに含めていません（ファイル名パターンおよび正規表現によるマスキング）。</li>
        <li>読み込み対象は直近{LOOKBACK_DAYS}日間に更新されたファイルに限定しているため、それより前から続く長期タスクの全体像は捉えきれない場合があります。</li>
        <li>タスクの並行実行（インターリーブ）には対応していません。1セッション内のイベントは時系列の単純な列として扱っており、検索セッション分割の研究（Jones &amp; Klinkner, CIKM 2008）では実ログの17〜20%が複数タスクの並行/入れ子構造を持つと報告されています。</li>
        <li>タスクの区切り・類似度のしきい値（時間差90分、テキスト類似度0.15〜0.30など）は経験則であり、大規模な人手ラベル付きデータでの検証は行っていません。時間差だけに頼らずカテゴリ・ファイル・テキスト類似度・話題転換表現を組み合わせる設計自体は先行研究と整合していますが、しきい値の妥当性は未検証です。</li>
        <li>繰り返しパターンの検出には、テキスト類似度に加えて各タスクの作業カテゴリ列（例: research→coding）を簡易的な「手順の型」とみなした編集距離を併用しています（プロセスマイニングのtrace/variant clustering の簡易版）。ただし本格的なワークフローグラフの復元は行っていません。</li>
      </ul>
      <h3>読み込んだログファイル一覧</h3>
      <ul class="small mono">{file_list}{more}</ul>
    </section>
    """


PAGE_CSS = """
:root {
  --bg: #0b1120; --panel: #131b2e; --panel2: #0f1526; --text: #e5e9f0;
  --muted: #8b93a7; --border: #24304a; --accent: #60a5fa;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0; padding: 0 0 60px 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, "Hiragino Kaku Gothic ProN", "Yu Gothic", sans-serif;
  line-height: 1.6; overflow-x: hidden; font-size: 16px;
}
header.top {
  background: linear-gradient(135deg, #1e293b, #0b1120); padding: 32px 24px;
  border-bottom: 1px solid var(--border);
}
header.top h1 { margin: 0 0 6px 0; font-size: 1.6rem; overflow-wrap: break-word; }
header.top p { margin: 0; color: var(--muted); }
nav.toc {
  position: sticky; top: 0; background: rgba(11,17,32,0.95); backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--border); padding: 10px 24px; z-index: 10;
  display: flex; gap: 14px; flex-wrap: wrap; font-size: 0.85rem;
}
nav.toc a { color: var(--accent); text-decoration: none; white-space: nowrap; }
nav.toc a:hover { text-decoration: underline; }
main { max-width: 1100px; margin: 0 auto; padding: 0 24px; width: 100%; }
section { margin-top: 42px; }
h2 { font-size: 1.25rem; border-left: 4px solid var(--accent); padding-left: 10px; }
h3 { font-size: 1.05rem; color: var(--text); }
h4 { margin: 0; font-size: 1rem; overflow-wrap: break-word; }
p, li, td, summary { overflow-wrap: break-word; word-break: break-word; }
.muted { color: var(--muted); }
.small { font-size: 0.85rem; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; word-break: break-all; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 20px; min-width: 130px; flex: 1 1 130px;
}
.card-num { font-size: 1.8rem; font-weight: 700; }
.card-label { color: var(--muted); font-size: 0.8rem; margin-top: 4px; }
.plain-list { list-style: none; padding: 0; }
.plain-list li { padding: 4px 0; border-bottom: 1px dashed var(--border); overflow-wrap: break-word; }
.bar-chart { display: flex; flex-direction: column; gap: 14px; }
.bar-row { display: grid; grid-template-columns: 200px 1fr 70px; align-items: center; gap: 10px; }
.bar-track { background: var(--panel2); border-radius: 6px; height: 16px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 6px; }
.bar-value { text-align: right; color: var(--muted); font-size: 0.85rem; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.72rem;
  font-weight: 600; white-space: nowrap;
}
.err-badge { background: #ef444422; color: #ef4444; border: 1px solid #ef444455; margin-left: 6px; }
.day-block {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  margin-bottom: 14px; padding: 10px 16px;
}
.day-block > summary { cursor: pointer; font-weight: 600; padding: 6px 0; }
.day-flow-chain { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; margin: 4px 0 14px 0; }
.flow-arrow { color: var(--muted); }
.day-flow { margin-top: 0; }
.timeline-table { width: 100%; border-collapse: collapse; margin-top: 8px; table-layout: fixed; }
.timeline-table th { text-align: left; color: var(--muted); font-size: 0.75rem; padding: 6px 8px; border-bottom: 1px solid var(--border); }
.timeline-table td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: top; overflow-wrap: break-word; }
.time-col { white-space: nowrap; color: var(--muted); font-size: 0.85rem; width: 92px; }
.seg-title { font-weight: 600; overflow-wrap: break-word; }
.flow-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.flow-card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; min-width: 0; }
.flow { margin-top: 10px; border-left: 2px solid var(--border); padding-left: 16px; display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.flow-step { position: relative; min-width: 0; }
.flow-dot {
  position: absolute; left: -25px; top: 0; width: 20px; height: 20px; border-radius: 50%;
  background: var(--accent); color: #0b1120; font-size: 0.7rem; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.flow-title { font-weight: 600; overflow-wrap: break-word; }
.routine-grid, .auto-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.routine-card, .auto-card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px;
  min-width: 0;
}
.routine-header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.evidence-block {
  background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 12px; margin-bottom: 8px;
}
.evidence-block > summary { cursor: pointer; overflow-wrap: break-word; }
footer { text-align: center; color: var(--muted); margin-top: 60px; font-size: 0.8rem; }

/* ---- Mobile (phone-width viewports) ---- */
@media (max-width: 680px) {
  header.top { padding: 20px 16px; }
  header.top h1 { font-size: 1.25rem; }
  header.top p { font-size: 0.85rem; }
  nav.toc {
    padding: 8px 12px; gap: 10px; font-size: 0.78rem;
    flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
    scrollbar-width: thin;
  }
  main { padding: 0 12px; }
  section { margin-top: 28px; }
  h2 { font-size: 1.05rem; }
  .cards { gap: 8px; }
  .card { padding: 10px 12px; min-width: 100px; flex: 1 1 100px; }
  .card-num { font-size: 1.35rem; }
  .card-label { font-size: 0.72rem; }
  /* stack the category bar chart: label above track above value */
  .bar-row { grid-template-columns: 1fr; gap: 4px; }
  .bar-value { text-align: left; }
  .day-block { padding: 8px 10px; }
  /* collapse the timeline table into a stack of per-row cards */
  .timeline-table, .timeline-table thead, .timeline-table tbody,
  .timeline-table tr, .timeline-table td { display: block; width: 100%; }
  .timeline-table thead { display: none; }
  .timeline-table tr {
    margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid var(--border);
  }
  .timeline-table tr:last-child { border-bottom: none; margin-bottom: 0; }
  .timeline-table td { border-bottom: none; padding: 2px 0; }
  .time-col { width: auto; margin-bottom: 2px; }
  .flow-grid, .routine-grid, .auto-grid { grid-template-columns: 1fr; }
  .flow-card, .routine-card, .auto-card { padding: 12px; }
  .flow { padding-left: 14px; }
  .flow-dot { left: -22px; width: 18px; height: 18px; }
}
"""

PAGE_JS = """
document.addEventListener('click', function(e){
  if (e.target.matches('nav.toc a')) {
    // let default anchor scrolling happen; nothing else needed (no CDN, no tracking)
  }
});
"""


def render_html(data):
    summary = data["summary"]
    segments_by_id = {s["segment_id"]: s for s in data["segments"]}
    generated_at = local_dt(dt_to_iso(datetime.now(timezone.utc))).strftime("%Y-%m-%d %H:%M JST")

    body = "".join([
        render_summary(summary),
        render_category_chart(summary),
        render_daily_timeline(data["segments"]),
        render_task_flows(data["merged_tasks"], segments_by_id),
        render_routines(data["routines"]),
        render_automation(data["automation_candidates"]),
        render_evidence(data["merged_tasks"], data["event_index"]),
        render_limitations(data["parsed_files_log"], 0),
    ])

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code / Codex / ブラウザ 作業ログレポート（直近7日間）</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header class="top">
  <h1>Claude Code / Codex / ブラウザ 作業ログ 直近7日間レポート</h1>
  <p>すべてローカルで生成・完結。外部送信なし。生成日時: {esc(generated_at)}</p>
</header>
<nav class="toc">
  <a href="#summary">サマリー</a>
  <a href="#categories">カテゴリ別作業量</a>
  <a href="#timeline">日別タイムライン</a>
  <a href="#flows">作業フロー</a>
  <a href="#routines">繰り返しパターン</a>
  <a href="#automation">自動化候補</a>
  <a href="#evidence">根拠ログ</a>
  <a href="#limitations">解析の限界</a>
</nav>
<main>
{body}
<footer>Generated locally by analyze_worklogs.py — no data leaves this machine.</footer>
</main>
<script>{PAGE_JS}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    now_utc = datetime.now(timezone.utc)
    cutoff_dt = now_utc - timedelta(days=LOOKBACK_DAYS)

    # Any command-line arguments are treated as extra Browser Activity Logger
    # export files or directories to ingest (in addition to the default scan
    # of ~/Downloads etc.).
    browser_export_args = [a for a in sys.argv[1:] if not a.startswith("-")]

    print(f"[1/6] Discovering log files under {', '.join(str(r) for r in CANDIDATE_ROOTS)} ...")
    found = discover_files(cutoff_dt)
    browser_exports = find_browser_exports(cutoff_dt, browser_export_args)

    events = []
    parsed_files_log = []

    print(f"[2/6] Parsing {len(found['claude_transcripts'])} Claude transcript file(s), "
          f"{len(found['codex_rollouts'])} Codex session file(s), "
          f"{len(browser_exports)} browser export(s) ...")
    for fpath in found["claude_transcripts"]:
        parse_claude_transcript(fpath, cutoff_dt, parsed_files_log, events)
        if len(events) > MAX_EVENTS_HARD_CAP:
            break
    for fpath in found["codex_rollouts"]:
        parse_codex_rollout(fpath, cutoff_dt, parsed_files_log, events)
        if len(events) > MAX_EVENTS_HARD_CAP:
            break
    for fpath in browser_exports:
        parse_browser_export(fpath, cutoff_dt, parsed_files_log, events)
        if len(events) > MAX_EVENTS_HARD_CAP:
            break

    known_session_ids = {e["source_session_id"] for e in events if e["source"] == "claude_code"}
    for fpath in found["claude_history"]:
        parse_claude_history(fpath, cutoff_dt, known_session_ids, parsed_files_log, events)

    print("[3/6] Parsing memory / instruction documents ...")
    for fpath in found["claude_memory"]:
        parse_markdown_doc(fpath, cutoff_dt, parsed_files_log, events, "claude_code")
    for fpath in found["codex_memory"]:
        parse_markdown_doc(fpath, cutoff_dt, parsed_files_log, events, "codex")

    project_paths = {e["project_path"] for e in events if e.get("project_path")}
    enrich_instruction_docs_from_project_paths(project_paths, cutoff_dt.timestamp(), found)
    for fpath in found["instruction_docs"]:
        source_guess = "codex" if ".codex" in fpath.parts else "claude_code"
        parse_markdown_doc(fpath, cutoff_dt, parsed_files_log, events, source_guess)

    events = [e for e in events if parse_iso(e["timestamp"]) and parse_iso(e["timestamp"]) >= cutoff_dt]
    print(f"[4/6] Normalized {len(events)} events. Building activities / segments / tasks ...")

    result = analyze(events)

    print(f"[5/6] Detected {len(result['segments'])} task segments, "
          f"{len(result['merged_tasks'])} merged tasks, "
          f"{len(result['routines'])} recurring routines, "
          f"{len(result['automation_candidates'])} automation candidates.")

    summary = build_summary(events, result["segments"], result["merged_tasks"],
                             result["automation_candidates"], parsed_files_log,
                             cutoff_dt, now_utc)

    event_index = {e["event_id"]: {"file": e["evidence"].get("file"), "line": e["evidence"].get("line")}
                   for e in events if e.get("evidence")}

    output = {
        "generated_at": dt_to_iso(now_utc),
        "lookback_days": LOOKBACK_DAYS,
        "summary": summary,
        "events": events,
        "activities": result["activities"],
        "segments": result["segments"],
        "merged_tasks": result["merged_tasks"],
        "routines": result["routines"],
        "automation_candidates": result["automation_candidates"],
        "parsed_files_log": parsed_files_log,
    }

    print(f"[6/6] Writing {OUT_JSON.name} and {OUT_HTML.name} ...")
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    html_data = dict(output)
    html_data["event_index"] = event_index
    html_doc = render_html(html_data)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print("Done.")
    print(f"  {OUT_JSON}")
    print(f"  {OUT_HTML}")


if __name__ == "__main__":
    sys.exit(main() or 0)
