"""Presentation contracts using real usage/speed records, without a desktop."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from test_usage import panel, event, usage
from active_session import Selection


def sample():
    return panel.Snapshot(
        total=panel.Usage.parse(usage(20000, 16000, 3000, 1000)),
        last=panel.Usage.parse(usage(2000, 1000, 1413, 400)),
        speed=panel.SpeedSample(1413, 38770, "2026-10-23T08:00:00Z"),
        session_id="00000000-0000-4000-8000-000000000001",
        timestamp="2026-10-23T08:00:01Z", loading=False, has_usage=True)


def view(snapshot=None):
    app = panel.UsagePanel.__new__(panel.UsagePanel)
    app.mode = "total"
    app.snapshot = snapshot or sample()
    app.follow_enabled = False
    app.follow_selection = Selection()
    app.path = Path("sample.jsonl")
    app.titles = {app.snapshot.session_id: "用于验证完整名称的聊天"}
    app.config = {"refresh_ms": 1000}
    return app


class PanelViewTests(unittest.TestCase):
    def test_event_evidence_controls_generation_and_idle(self):
        reader = panel.LogTailer("unused.jsonl")
        reader.snapshot.loading = False
        self.assertEqual(panel.display_state(reader.snapshot)[0], "等待统计")
        reader.accept(event(kind="task_started"))
        self.assertEqual(panel.display_state(reader.snapshot)[0], "生成中")
        reader.accept(event(usage(), usage(400, 200, 30, 10)))
        reader.accept(event(kind="task_complete"))
        self.assertEqual(panel.display_state(reader.snapshot)[0], "等待新的调用")
        reader.snapshot.timestamp = "2000-01-01T00:00:00Z"
        self.assertEqual(panel.display_state(reader.snapshot)[0], "等待新的调用")
        self.assertEqual(reader.snapshot.total.total_tokens, 1100)

    def test_wait_read_error_priority_and_empty_counts(self):
        snapshot = panel.Snapshot(loading=False)
        self.assertEqual(panel.display_state(snapshot, True, Selection())[0], "等待会话")
        selected = Selection(session_id="known", message="等待本地用量日志")
        self.assertEqual(panel.display_state(snapshot, True, selected)[0], "等待统计")
        snapshot.loading = True
        self.assertEqual(panel.display_state(snapshot, True, selected)[0], "读取中")
        snapshot.error = "日志读取失败：拒绝访问"
        self.assertEqual(panel.display_state(snapshot)[:2], ("读取失败", snapshot.error))
        self.assertEqual(panel.display_state(panel.Snapshot(loading=False, has_usage=True))[0], "等待统计")

    def test_current_scope_drives_usage_cache_hints_and_copy_not_speed(self):
        app = view()
        total_copy, speed = app.stats_text(), app.speed_hint()
        self.assertIn("统计范围：会话累计", total_copy)
        self.assertIn("输入总量：20,000", total_copy)
        self.assertIn("总用量（输入 + 输出）：23,000", total_copy)
        self.assertIn("缓存命中率：80.00%", total_copy)
        app.mode = "last"
        last_copy = app.stats_text()
        self.assertIn("统计范围：最近调用", last_copy)
        self.assertIn("输入总量：2,000", last_copy)
        self.assertIn("已缓存：1,000", last_copy)
        self.assertIn("未缓存：1,000", last_copy)
        self.assertIn("其中推理：400", last_copy)
        self.assertIn("总用量（输入 + 输出）：3,413", last_copy)
        self.assertIn("缓存命中率：50.00%", last_copy)
        self.assertIn("最近调用", app.usage_hint())
        self.assertIn("最近调用", app.cache_hint())
        self.assertEqual(app.speed_hint(), speed)
        self.assertIn("≈36.4 tok/s · 输出 1,413 · 耗时 38.77 秒", app.call_summary())
        for text in (last_copy, total_copy):
            self.assertIn("不计工具执行等待", text)
            self.assertIn("包含推理和工具参数输出", text)
            self.assertIn("数据时间（用量统计记录，本地时区）：2026-10-23", text)
            self.assertIn("不代表当前上下文占用", text)

    def test_unknown_counts_and_timing_are_explained_not_zero(self):
        app = view(panel.Snapshot(loading=False))
        self.assertIn("输入总量：—", app.stats_text())
        self.assertIn("缓存命中率：—", app.stats_text())
        self.assertIn("耗时 —", app.call_summary())
        self.assertIn("缺少计时记录", app.speed_hint())
        self.assertIn("计数缺失", app.cache_hint())
        self.assertIn("字段缺失", app.usage_hint())

    def test_data_age_and_full_timestamp_have_distinct_meanings(self):
        stamp = "2026-10-23T08:00:00Z"
        now = datetime(2026, 10, 23, 8, tzinfo=timezone.utc)
        for delta, expected in ((0, "刚刚"), (9, "9 秒前"), (90, "1 分钟前"),
                                (7200, "2 小时前"), (172800, "2 天前")):
            self.assertEqual(panel.data_age(stamp, now + timedelta(seconds=delta)), expected)
        self.assertEqual(panel.data_age(""), "—")
        self.assertEqual(panel.data_age("invalid"), "—")
        self.assertIn("2026-10-23", panel.timestamp_label(stamp))

    def test_follow_hint_includes_complete_title_and_real_wait_reason(self):
        app = view()
        self.assertIn("固定会话", app.follow_hint())
        self.assertIn(app.titles[app.snapshot.session_id], app.follow_hint())
        app.follow_enabled = True
        app.follow_selection = Selection(message="当前为远程会话，无法读取本地用量")
        app.snapshot = panel.Snapshot(loading=False)
        self.assertIn("等待会话", app.follow_hint())
        self.assertIn("远程会话", app.follow_hint())


if __name__ == "__main__":
    unittest.main(verbosity=2)
