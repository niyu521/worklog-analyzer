import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from collectors import claude_code_watcher as cc
from collectors import google_poller as google


class ClaudeCodeWatcherTests(unittest.TestCase):
    def test_edit_pair_restores_full_content(self):
        pending = {}
        use = {
            "sessionId": "session",
            "cwd": "/srv",
            "timestamp": "2026-07-05T00:00:00Z",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Edit",
                    "input": {
                        "file_path": "sample.txt",
                        "old_string": "old",
                        "new_string": "new",
                        "replace_all": False,
                    },
                }]
            },
        }
        result = {
            "timestamp": "2026-07-05T00:00:01Z",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "ok",
                }]
            },
            "toolUseResult": {"originalFile": "old old", "userModified": False},
        }
        cc.parse_line(use, pending, "source.jsonl", 0)
        events, resolved = cc.parse_line(result, pending, "source.jsonl", 0)
        self.assertEqual(resolved, {"tool-1"})
        self.assertEqual(events[0]["content"], "new old")
        self.assertEqual(events[0]["native_id"], "/srv/sample.txt")

    def test_incomplete_jsonl_line_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(b'{"complete": true}\n{"partial":')
            lines, offset = cc.read_complete_lines(path, 0)
            self.assertEqual(len(lines), 1)
            self.assertEqual(offset, len(b'{"complete": true}\n'))

    def test_exclusions_prevent_self_loop(self):
        self.assertTrue(cc.is_excluded("/srv/company-brain/data/blob.txt"))
        self.assertTrue(cc.is_excluded("/home/chr/.claude/projects/log.jsonl"))
        self.assertTrue(cc.is_excluded("/tmp/output.txt"))
        self.assertTrue(cc.is_excluded("/srv/work/image.png"))
        self.assertFalse(cc.is_excluded("/srv/work/notes.md"))


class GooglePollerTests(unittest.TestCase):
    def test_revision_export_uses_export_link_once(self):
        client = object.__new__(google.GoogleClient)
        client.request = Mock(return_value="document")
        revision = {"exportLinks": {"text/plain": "https://export.example/text"}}
        self.assertEqual(client.export_revision(revision), "document")
        client.request.assert_called_once_with(
            "GET", "https://export.example/text", raw=True
        )

    def test_sheet_ranges_quote_apostrophes(self):
        client = object.__new__(google.GoogleClient)
        client.request = Mock(return_value={"valueRanges": [{"values": [["ok"]]}]})
        values = client.sheet_values("sheet-id", ["Team's Plan"])
        self.assertEqual(values, {"Team's Plan": [["ok"]]})
        params = client.request.call_args.kwargs["params"]
        self.assertIn(("ranges", "'Team''s Plan'!A:Z"), params)

    def test_sheet_snapshot_is_tab_normalized(self):
        client = Mock()
        client.list_files.return_value = [{
            "id": "sheet-id",
            "name": "Budget",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "modifiedTime": "2026-07-05T01:00:00Z",
        }]
        client.sheet_titles.return_value = ["Overview", "Details"]
        client.sheet_values.return_value = {
            "Overview": [["Name", "Total"], ["A", 10]],
            "Details": [["Item", "Cost"]],
        }
        events = google.snapshot_events(client, {})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["platform"], "google_sheets")
        self.assertEqual(
            events[0]["content"],
            "## Overview\nName\tTotal\nA\t10\n\n## Details\nItem\tCost\n",
        )

if __name__ == "__main__":
    unittest.main()
