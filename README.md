# worklog-analyzer

A fully local analysis tool that reads your local logs from Claude Code
(`~/.claude`), Codex (`~/.codex`), and browser activity (the export JSON
written by the bundled `browser-activity-logger` Chrome extension), organizes
the last 7 days of work chronologically, and visualizes it as a single HTML
report.

**Nothing is ever sent anywhere.** It makes no network calls, skips
credential/secret-looking files without reading them, and masks
secret-looking strings found in any other text (API keys, tokens, Bearer/JWT,
private-key blocks, URL token parameters, etc.) with a regex before writing
output.

## Repository layout

This repository has two components: one that **collects** AI/browser work
logs, and one that **analyzes and visualizes** them.

```
worklog-analyzer/               <- repo root = the analysis engine
├─ analyze_worklogs.py          the analyzer (Python standard library only)
├─ README.md                    this file
└─ browser-activity-logger/     Chrome extension that collects browser activity (the collector)
   ├─ src/                       extension source (TypeScript)
   └─ README.md                  extension details
```

> This repo also contains a `company-brain/` folder, which is a separate
> project (concept memos, an RFS proposal, and a work-intelligence backend
> prototype) unrelated to the analyzer described below.

### Data flow

```
[ Claude Code ~/.claude ] ─┐
[ Codex       ~/.codex   ] ─┼─▶  analyze_worklogs.py  ─▶  worklog_report_last_7_days.html
[ Browser extension Export JSON ]─┘  (normalize → classify → segment →   parsed_worklog_last_7_days.json
                                      stitch → routine detection → visualize)
```

- **Collector** `browser-activity-logger/`: a Chrome extension that locally
  records browsing, searches, clicks, etc. and exports them via "Export JSON".
  It does for the browser what Claude Code / Codex automatically do by leaving
  session logs. See
  [browser-activity-logger/README.md](browser-activity-logger/README.md).
- **Analyzer** `analyze_worklogs.py`: auto-discovers `~/.claude` and
  `~/.codex`, also ingests the extension's export JSON, and analyzes and
  visualizes all three sources together. The rest of this README describes the
  analyzer.

## Input

| Source | Concrete input | Format |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl`, `~/.claude/history.jsonl` | JSON Lines (one record per line) |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/**` | JSON Lines |
| Browser | `browser-activity-*.json` exported by `browser-activity-logger` | JSON (`ExportBundle`: `{exportedAt, schemaVersion, sessionId, settings, events[]}`) |
| Instructions / memory | `CLAUDE.md`, `AGENTS.md`, `MEMORY.md` | Markdown |

- Claude / Codex logs are **auto-discovered** (`~/.claude`, `~/.codex`, and
  any `.claude` / `.codex` under the current directory).
- Browser export JSON is auto-scanned in `~/Downloads`, the current directory,
  the script's own directory, and a `browser-exports/` subfolder. You can also
  pass explicit paths as arguments:
  `python3 analyze_worklogs.py /path/to/browser-activity-20260705.json`
- In all cases, only files updated within the **last 7 days**, and only events
  within that window, are considered.

## Output

| File | Format | Contents |
|---|---|---|
| `worklog_report_last_7_days.html` | Self-contained HTML (no external CDN/fonts, CSS inlined) | The main deliverable: a visualization report you can open directly in a browser |
| `parsed_worklog_last_7_days.json` | JSON | The structured data below (a machine-readable intermediate artifact) |

Top-level structure of `parsed_worklog_last_7_days.json`:

```jsonc
{
  "generated_at": "…Z",
  "lookback_days": 7,
  "summary": { "events_detected": …, "category_minutes": {…},
               "source_event_counts": { "claude_code": …, "codex": …, "browser": … }, … },
  "events":     [ /* common events: event_id,timestamp,source,event_type,prompt_or_text,… */ ],
  "activities": [ /* atomic activities: label,category,intent,related_files,… */ ],
  "segments":   [ /* task segments: title,category,duration_minutes,files_touched,… */ ],
  "merged_tasks":[ /* stitched tasks: fragments of the same work re-joined */ ],
  "routines":   [ /* recurring patterns: occurrences,common_steps,automation_potential,… */ ],
  "automation_candidates": [ /* skill/automation candidates */ ],
  "parsed_files_log": [ /* the list of files that were read */ ]
}
```

## Usage

```bash
python3 analyze_worklogs.py                 # auto-discovery only
python3 analyze_worklogs.py ~/Downloads/browser-activity-20260705.json  # add an explicit browser export
```

Runs on Python 3.9+ using only the standard library (no packages to install).
Output is written (overwriting) into this directory.

## What it does

1. **Discovery**: Scans `~/.claude`, `~/.codex`, any `.claude` / `.codex`
   under the current directory, and browser export JSON (e.g. in
   `~/Downloads`), considering only files updated in the last 7 days. Files
   whose names look like credentials/secrets (`auth.json`, `credentials`,
   `token`, `secret`, `api_key`, `key`, `.env`, `.npmrc`, `.pypirc`, SSH keys,
   etc.) are never read. Binary files and files over 25 MB are also skipped.
2. **Normalization**: Converts Claude Code session transcripts
   (`projects/**/*.jsonl`), prompt history (`history.jsonl`), Codex session
   logs (`sessions/**/*.jsonl`, `archived_sessions/**`), **browser activity**
   (`browser-activity-logger` `ExportBundle` JSON), and memory/instruction
   documents (`CLAUDE.md`, `AGENTS.md`, `MEMORY.md`, etc.) into a common event
   schema (`event_id`, `timestamp`, `source`, `event_type`, ...). Browser
   events treat the domain (e.g. `github.com`) as a "repository" equivalent,
   normalize `page_view` / `search_query` as work starting points, and clicks
   and inputs as evidence. URL query strings (which often contain tokens) are
   stripped, and the domain is used as a strong category hint (e.g.
   `github.com` → coding, `docs.google.com` → documentation, `freee.co.jp` →
   accounting).
3. **Atomic activity decomposition**: When one prompt contains several
   requests ("do A, then do B", "1. ... 2. ... 3. ...", etc.), it splits them
   on connective phrases and list structures and treats each as an independent
   unit of work.
4. **Category classification**: A single pure function `classify_category()`
   classifies work from keywords, file extensions, commands, and repository
   name. It is kept independent of the rest of the pipeline so it can later be
   swapped for an LLM-based classifier.
5. **Task segmentation**: Within a session, work boundaries are decided by
   combining not just time gaps but also category match, file overlap, text
   similarity, and explicit topic-transition phrases ("by the way", "on a
   separate note", etc.).
6. **Task stitching**: Segments in the same repository/project with similar
   files or titles are re-joined into a single task even when far apart in
   time (e.g. a morning fix and an afternoon PR).
7. **Recurring-pattern detection**: Stitched tasks are clustered by category
   and title similarity; anything appearing 2+ times is surfaced as a
   "routine".
8. **Automation-candidate suggestions**: Scored from recurrence frequency,
   procedure stability (reuse of the same files/commands), input/output
   clarity per category, and the amount of human judgment required, then rated
   high/medium/low.
9. **HTML/JSON output**: Writes a single self-contained HTML report (no
   external CDN/fonts, CSS inlined) plus the evidence-backed JSON.

## Reading the report

`worklog_report_last_7_days.html` contains these sections:

1. Summary (period, files read, events/tasks detected, top projects)
2. Work volume by category (bar chart)
3. Daily timeline (per day: start/end time, title, category, related files)
4. Work flow (major tasks spanning multiple segments, shown as steps)
5. Recurring work patterns (occurrences, common steps, automation potential)
6. Skill/automation candidates (expected input/output, steps, risks)
7. Evidence trail to the raw logs (event IDs and log file/line; content masked)
8. Analysis limitations (see below)

## Limitations (important)

- Log formats are internal, undocumented app details. If a future update
  changes their structure, the parsers may no longer keep up. Parsing is
  written flexibly to read a wide range of JSON/JSONL/Markdown, but complete
  coverage is not guaranteed.
- Files without timestamps (`CLAUDE.md`, `AGENTS.md`, `MEMORY.md`, etc.) use
  the file modification time (mtime) as a stand-in for the work time.
- Category classification, atomic-activity decomposition, task stitching, and
  recurring-pattern detection are all rule-based estimates from keywords, file
  paths, and time gaps. They do not perfectly recover human intent.
- When one prompt contains multiple activities, it is not possible to
  precisely attribute which tool call belongs to which sub-activity, so all
  evidence within that turn (file operations, commands, etc.) is assigned to
  every sub-activity.
- Automation-candidate ratings are heuristic scores; the final decision about
  whether something should actually be automated is left to a human.
- Only files updated within the last 7 days are read, so the full picture of a
  long-running task that started earlier may not be captured.
- **Interleaved (concurrent) tasks are not handled.** Research on segmenting
  search-query session logs (Jones & Klinkner, CIKM 2008) reports that 17–20%
  of real logs contain concurrent/nested task structure. This tool treats
  events within a session as a simple chronological sequence, so when several
  activities alternate within one conversation the boundaries may differ from
  reality.
- **Segmentation deliberately avoids relying on time gaps alone**, but the
  thresholds (a 90-minute gap, similarity 0.15–0.30, etc.) were chosen
  empirically and not validated against a large human-labeled dataset.
  Research in the same area (Jones & Klinkner 2008) shows that time-gap-only
  segmentation plateaus in accuracy when it ignores context, that there is no
  single "correct" threshold, and that combining time gaps with lexical
  similarity improves results substantially. This tool follows that idea
  (time gap + category match + file overlap + text similarity + explicit
  topic-transition phrases), but the thresholds themselves are not validated.
- **Classic text-segmentation algorithms like TextTiling / C99 are not
  directly suited to short, dialogue/chat-like utterances.** They assume
  lexical recurrence across hundreds to thousands of words of long prose, and
  several studies (Galley et al. 2003; Riedl & Biemann 2012, among others)
  report that their error rates worsen roughly 2–4× on short, conversational
  text like meeting transcripts or chat logs. This tool therefore does not
  rely on lexical similarity alone, substituting "structural cues" (category
  match, file overlap, explicit topic-transition phrases) alongside lexical
  similarity.
- **Recurring-pattern similarity** (deciding whether two tasks are "the same
  work") combines text similarity (char bigrams / words / `difflib.
  SequenceMatcher`) with an edit distance over each task's category sequence
  (e.g. research→coding→coding), treated as a lightweight "procedure shape"
  (a simplified version of trace/variant clustering from process mining /
  sequential pattern mining). It does **not** perform full process mining
  (recovering a workflow graph from directly-follows relations, as in the
  Alpha algorithm or Heuristics Miner).

## Security / privacy

- No network calls whatsoever (`urllib`, `requests`, `socket`, etc. are not
  used).
- Files whose names match secret patterns (`credential`, `token`, `secret`,
  `api_key`, `.env`, `.npmrc`, `.pypirc`, `id_rsa`, etc.) or that live under
  `.ssh` / `.aws` / `.gnupg` are never read at all.
- For all other text, API keys, Bearer tokens, JWTs, AWS access keys,
  private-key blocks, and key-value forms like `password=` / `secret=` are
  detected with regex and replaced with `[REDACTED]` before being written to
  the JSON/HTML.
- Directories likely to contain tokens or environment variables — plugin /
  marketplace source, shell snapshots, `session-env`, `.claude.json.backup`,
  etc. — are excluded from scanning entirely.

## Re-running

`analyze_worklogs.py` performs a full scan every run and overwrites the two
output files. It has no side effects and is safe to run repeatedly.
