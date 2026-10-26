import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".local" / "tests"
WORK.mkdir(parents=True, exist_ok=True)
SOURCE = ROOT / "usage_panel.py"
sys.path.insert(0, str(SOURCE.parent))
spec = importlib.util.spec_from_file_location("usage_panel", SOURCE)
panel = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = panel
spec.loader.exec_module(panel)


def event(total=None, last=None, kind="token_count", info=True):
    payload = {"type": kind}
    if kind == "token_count":
        payload["info"] = {"total_token_usage": total, "last_token_usage": last} if info else None
    return {"type": "event_msg", "timestamp": "2026-10-23T07:00:00Z", "payload": payload}


def usage(inputs=1000, cached=600, output=100, reasoning=30):
    return dict(input_tokens=inputs, cached_input_tokens=cached, output_tokens=output,
                reasoning_output_tokens=reasoning, total_tokens=inputs+output)


def line(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.folder = WORK / "test-fixtures" / self._testMethodName
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "test.jsonl"
        self.path.write_bytes(b"")

    def test_cumulative_snapshots_are_not_added(self):
        record = line(event(usage(), usage(300, 150, 30, 10)))
        self.path.write_bytes(record + record)
        tailer = panel.LogTailer(self.path)
        result = tailer.poll()
        self.assertEqual(result.total.total_tokens, 1100)
        self.assertEqual(result.total.cache_rate, 60)
        self.assertEqual(result.total.uncached_input, 400)
        self.assertEqual(result.last.total_tokens, 330)
        self.assertEqual(result.last.cache_rate, 50)
        self.assertEqual(tailer.poll().total.total_tokens, 1100)
        with self.path.open("ab") as stream:
            stream.write(line(event(usage(2000, 1400, 200, 70))))
        self.assertEqual(tailer.poll().total.total_tokens, 2200)

    def test_partial_record_is_committed_only_after_newline(self):
        record = line(event(usage()))
        split = len(record) // 2
        self.path.write_bytes(record[:split])
        tailer = panel.LogTailer(self.path)
        self.assertFalse(tailer.poll().has_usage)
        with self.path.open("ab") as stream:
            stream.write(record[split:-1])
        self.assertFalse(tailer.poll().has_usage)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(tailer.poll().total.cache_rate, 60)

    def test_truncated_log_resets_totals_and_pending_record(self):
        self.path.write_bytes(line(event(usage())) * 3 + b'{"incomplete":')
        tailer = panel.LogTailer(self.path)
        self.assertEqual(tailer.poll().total.total_tokens, 1100)
        self.path.write_bytes(line(event(usage(10, 4, 3, 1))))
        result = tailer.poll()
        self.assertEqual(result.total.total_tokens, 13)
        self.assertEqual(result.total.cache_rate, 40)

    def test_file_replacement_resets_cursor(self):
        self.path.write_bytes(line(event(usage())))
        tailer = panel.LogTailer(self.path)
        tailer.poll()
        replacement = self.folder / "replacement.jsonl"
        replacement.write_bytes(line(event(usage(2000, 1800, 300, 0))) * 4)
        replacement.replace(self.path)
        self.assertEqual(tailer.poll().total.total_tokens, 2300)

    def test_missing_file_recovers(self):
        absent = self.folder / "recovered.jsonl"
        if absent.exists():
            absent.unlink()
        tailer = panel.LogTailer(absent)
        self.assertTrue(tailer.poll().error)
        absent.write_bytes(line(event(usage())))
        result = tailer.poll()
        self.assertFalse(result.error)
        self.assertEqual(result.total.total_tokens, 1100)

    def test_null_usage_does_not_erase_existing_counters(self):
        self.path.write_bytes(line(event(usage())) + line(event(info=False)))
        result = panel.read_snapshot(self.path)
        self.assertEqual(result.total.total_tokens, 1100)
        self.assertEqual(result.total.cache_rate, 60)

    def test_unknown_or_invalid_cache_count_is_not_zero_percent(self):
        self.assertIsNone(panel.Usage.parse({"input_tokens": 100, "output_tokens": 10}).cache_rate)
        self.assertIsNone(panel.Usage.parse(usage(0, 0, 0, 0)).cache_rate)
        self.assertIsNone(panel.Usage.parse(usage(100, 120, 10, 0)).cache_rate)
        self.assertIsNone(panel.Usage.parse(usage(100, -1, 10, 0)).cache_rate)
        self.assertEqual(panel.Usage.parse(usage(100, 0, 10, 0)).cache_rate, 0)

    def test_cached_and_reasoning_tokens_are_subsets(self):
        result = panel.Usage.parse(usage(1000, 900, 100, 80))
        self.assertEqual(result.total_tokens, 1100)
        self.assertEqual(result.cache_rate, 90)

    def test_malformed_lines_do_not_stop_later_usage(self):
        self.path.write_bytes(b"garbage\n{broken\n" + line(event(usage())))
        result = panel.read_snapshot(self.path)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.total.total_tokens, 1100)

    def test_large_line_is_bounded_and_next_event_is_read(self):
        tailer = panel.LogTailer(self.path)
        tailer.consume(b"x" * (panel.MAX_LINE + 1))
        self.assertEqual(tailer.pending, b"")
        self.assertTrue(tailer.discarding)
        tailer.consume(b"more\n" + line(event(usage())))
        self.assertEqual(tailer.snapshot.total.total_tokens, 1100)

    def test_status_events_and_utf8_boundaries(self):
        meta = line({"type": "session_meta", "payload": {"id": "测试会话"}})
        tailer = panel.LogTailer(self.path)
        for part in meta:
            tailer.consume(bytes([part]))
        self.assertEqual(tailer.snapshot.session_id, "测试会话")
        tailer.consume(line(event(kind="task_started")))
        self.assertTrue(tailer.snapshot.active)
        tailer.consume(line(event(kind="task_complete")))
        self.assertFalse(tailer.snapshot.active)

    def test_non_event_text_cannot_change_usage(self):
        fake = event(usage())
        fake["type"] = "response_item"
        self.path.write_bytes(line(fake))
        self.assertFalse(panel.read_snapshot(self.path).has_usage)

    def test_protected_log_is_not_parsed(self):
        self.path.write_bytes(b"%TSD-Header-001%encrypted-test-data")
        result = panel.read_snapshot(self.path)
        self.assertIn("TSD", result.error)
        self.assertFalse(result.has_usage)

    def test_recent_list_excludes_subagents(self):
        root = self.folder / "sessions"
        day = root / "2026" / "10" / "23"
        day.mkdir(parents=True, exist_ok=True)
        parent = day / "rollout-2026-10-23T01-00-00-parent.jsonl"
        child = day / "rollout-2026-10-23T02-00-00-child.jsonl"
        parent.write_bytes(line({"type": "session_meta", "payload": {"id": "parent", "source": "vscode", "cwd": "C:/project"}}))
        child.write_bytes(line({"type": "session_meta", "payload": {"id": "child", "source": {"subagent": {}}, "cwd": "C:/project"}}))
        rows = panel.recent_sessions(root, parent)
        self.assertEqual([row[3] for row in rows], ["parent"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
