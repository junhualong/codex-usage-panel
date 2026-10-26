"""Isolated Tk interaction/layout checks; --preview opens synthetic data for visual QA."""
from dataclasses import replace
import json
import sys
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_usage import panel, WORK
from test_panel_view import sample
from active_session import Selection

BASE = WORK / "panel-ui-fixtures"
LONG_TITLE = "一个包含中文与 English 的很长聊天名称：界面排版和缓存用量验证 / " * 8


def make_panel(name):
    state_dir = BASE / name
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "settings.json").write_text("{}", encoding="utf-8")
    config = {"default_session": str(BASE / "synthetic.jsonl"),
              "sessions_root": str(BASE / "sessions"), "refresh_ms": 1000, "auto_follow": False}
    root = tk.Tk()
    with patch.object(panel.UsagePanel, "start_reader"), patch.object(panel, "read_titles", return_value={}):
        app = panel.UsagePanel(root, config, state_dir)
    root.after_cancel(app.poll_job)
    app.poll_job = None
    app.snapshot = sample()
    app.titles = {app.snapshot.session_id: LONG_TITLE}
    root.geometry(f"{app.px(app.WIDTH)}x{app.px(app.BAR_HEIGHT)}+40+80")
    app.render()
    root.update()
    return root, app


def scenario(app, name):
    app.follow_enabled = True
    app.follow_selection = Selection(session_id=sample().session_id, path="synthetic.jsonl", message="")
    app.snapshot = sample()
    if name == "等待会话":
        app.apply_follow_selection(Selection(message="等待当前聊天的导航记录"))
    elif name == "读取中":
        app.apply_follow_selection(Selection(loading=True, message="正在读取界面导航记录"))
    elif name == "等待统计":
        app.snapshot = panel.Snapshot(session_id=sample().session_id, loading=False)
    elif name == "生成中":
        app.snapshot = replace(sample(), active=True)
    elif name == "读取失败":
        app.snapshot = panel.Snapshot(session_id=sample().session_id, loading=False,
                                      error="日志读取失败：拒绝访问，请检查文件访问权限")
    elif name == "大数值":
        app.snapshot = replace(sample(), total=panel.Usage.parse({
            "input_tokens": 987654321098765432, "cached_input_tokens": 98765432109876543,
            "output_tokens": 123456789012345678, "reasoning_output_tokens": 12345678901234567}))
    app.render()
    app.root.update_idletasks()


class PanelGuiTests(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_panel(self._testMethodName)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args))

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.assertFalse(self.errors)

    def text(self, tag):
        return [self.app.canvas.itemcget(i, "text") for i in self.app.canvas.find_withtag(tag)
                if self.app.canvas.type(i) == "text"]

    def click(self, x, y):
        canvas = self.app.canvas
        canvas.event_generate("<Motion>", x=self.app.px(x), y=self.app.px(y))
        canvas.event_generate("<ButtonPress-1>", x=self.app.px(x), y=self.app.px(y))
        canvas.event_generate("<ButtonRelease-1>", x=self.app.px(x), y=self.app.px(y))
        self.root.update()

    def test_scope_text_and_arrow_open_marked_menu_and_sync_every_view(self):
        app = self.app
        app.set_expanded(True)
        with patch.object(tk.Menu, "tk_popup") as popup:
            for x in (268, 337):
                self.click(x, 24)
                self.assertIsNone(app.drag_origin)
                menu = app.scope_menu
                self.assertEqual(menu.entrycget(0, "label"), "会话累计")
                self.assertEqual(menu.entrycget(1, "label"), "最近调用")
                self.assertEqual(menu.type(0), "radiobutton")
                self.assertEqual(app.scope_variable.get(), app.mode)
            self.assertEqual(popup.call_count, 2)
        speed = self.text("speed_value")
        app.scope_menu.invoke(1)
        self.assertEqual(app.mode, "last")
        self.assertEqual(self.text("compact_total"), ["3.4K"])
        self.assertEqual(self.text("input_value"), ["2,000"])
        self.assertEqual(self.text("cache_rate"), ["50.0%"])
        self.assertEqual(self.text("usage_scope"), ["最近调用用量 · tokens"])
        self.assertIn("最近调用", app.usage_hint())
        self.assertIn("最近调用", app.cache_hint())
        self.assertEqual(self.text("speed_value"), speed)
        saved = json.loads((app.state_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "last")
        app.scope_menu.invoke(0)
        self.assertEqual(self.text("input_value"), ["20,000"])
        self.assertEqual(self.text("cache_rate"), ["80.0%"])

    def test_clipboard_and_temporary_feedback_match_selected_scope(self):
        app = self.app
        app.set_expanded(True)
        app.set_mode("last")
        self.click(773, 341)
        copied = self.root.clipboard_get()
        self.assertEqual(copied, app.stats_text())
        self.assertIn("统计范围：最近调用", copied)
        self.assertIn("输入总量：2,000", copied)
        self.assertIn("缓存命中率：50.00%", copied)
        self.assertEqual(self.text("copy_label"), ["已复制"])
        app.copy_until = time.monotonic() - 1
        app.refresh_transient()
        self.assertEqual(self.text("copy_label"), ["复制统计"])

    def test_follow_badge_does_not_expand_and_buttons_have_hover_feedback(self):
        app = self.app
        scenario(app, "空闲")
        self.assertEqual(self.text("follow"), ["跟随中"])
        self.click(682, 24)
        self.assertFalse(app.expanded)
        self.assertFalse(app.canvas.tag_bind("follow", "<Button-1>"))
        for tag in ("scope", "details", "pin"):
            original = app.canvas.itemcget(tag + "_bg", "fill")
            app.hover(SimpleNamespace(x_root=50, y_root=80), tag, True)
            self.assertEqual(app.canvas.itemcget(tag + "_bg", "fill"), panel.HOVER)
            app.hide_tooltip()
            self.assertEqual(app.canvas.itemcget(tag + "_bg", "fill"), original)
        self.assertEqual(app.canvas.itemcget("pin_bg", "fill"), panel.SELECTED)
        self.click(737, 24)
        self.assertFalse(app.topmost)
        self.assertEqual(app.button_fills["pin"], panel.BACKGROUND)
        self.click(778, 24)
        self.assertTrue(app.expanded)
        self.click(778, 24)
        self.assertFalse(app.expanded)

    def test_states_and_idle_time_refresh_keep_last_record(self):
        app = self.app
        app.set_expanded(True)
        for name in ("等待会话", "读取中", "等待统计", "生成中", "空闲", "读取失败"):
            scenario(app, name)
            expected = "等待新的调用" if name == "空闲" else name
            self.assertEqual(self.text("detail_state"), [expected])
            if name == "读取失败":
                self.assertIn("拒绝访问", self.text("reason")[0])
            if name in ("等待会话", "读取中", "等待统计", "读取失败"):
                self.assertEqual(self.text("compact_total"), ["—"])
        scenario(app, "空闲")
        stamp = app.snapshot.timestamp
        with patch.object(panel, "data_age", return_value="5 分钟前"):
            app.tick()
        self.assertEqual(self.text("data_time"), ["数据时间：5 分钟前"])
        self.assertEqual(app.snapshot.timestamp, stamp)
        self.assertEqual(self.text("detail_state"), ["等待新的调用"])

    def test_releasing_a_drag_over_buttons_does_not_activate_them(self):
        app = self.app
        canvas = app.canvas
        for start_x, end_x in ((249, 778), (778, 737)):
            canvas.event_generate("<Motion>", x=app.px(start_x), y=app.px(24))
            canvas.event_generate("<ButtonPress-1>", x=app.px(start_x), y=app.px(24))
            canvas.event_generate("<Motion>", x=app.px(end_x), y=app.px(24))
            canvas.event_generate("<ButtonRelease-1>", x=app.px(end_x), y=app.px(24))
            self.root.update()
            self.assertFalse(app.expanded)
            self.assertTrue(app.topmost)

    def test_known_chat_name_survives_unreadable_log(self):
        app = self.app
        app.follow_enabled = True
        app.path = app.state_dir / "missing-session.jsonl"
        app.follow_selection = Selection(session_id=sample().session_id, path=str(app.path))
        app.start_reader()
        _, app.snapshot = app.queue.get(timeout=2)
        app.stop.set()
        self.assertEqual(app.state()[0], "读取失败")
        self.assertEqual(app.snapshot.session_id, sample().session_id)
        self.assertIn(LONG_TITLE, app.follow_hint())

    def test_layout_bounds_large_numbers_long_names_and_scaled_hit_areas(self):
        app = self.app
        checked = []
        for scale in (1.0, 1.25, 1.5, 2.0):
            app.scale = scale
            app.fonts.clear()
            for expanded in (False, True):
                app.expanded = expanded
                height = app.EXPANDED_HEIGHT if expanded else app.BAR_HEIGHT
                self.root.geometry(f"{app.px(app.WIDTH)}x{app.px(height)}+20+50")
                for name in ("等待会话", "读取中", "等待统计", "生成中", "空闲", "读取失败", "大数值"):
                    scenario(app, name)
                    boxes = []
                    for item in app.canvas.find_all():
                        if app.canvas.type(item) != "text":
                            continue
                        box = app.canvas.bbox(item)
                        text = app.canvas.itemcget(item, "text")
                        self.assertGreaterEqual(box[0], 0, (scale, text, box))
                        self.assertLessEqual(box[2], app.px(app.WIDTH), (scale, text, box))
                        self.assertGreaterEqual(box[1], 0, (scale, text, box))
                        self.assertLessEqual(box[3], app.px(height), (scale, text, box))
                        for other, label in boxes:
                            overlap = min(box[2], other[2]) > max(box[0], other[0]) and min(box[3], other[3]) > max(box[1], other[1])
                            self.assertFalse(overlap, (scale, name, text, label, box, other))
                        boxes.append((box, text))
                    checked.append([scale, expanded, name])
                with patch.object(tk.Menu, "tk_popup") as popup:
                    self.click(337, 24)
                    popup.assert_called_once()
                if expanded:
                    self.assertIn(LONG_TITLE, app.follow_hint())
                    self.assertEqual(self.text("input_value"), ["987,654,321,098,765,432"])
        (BASE / "layout-results.json").write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_bottom_edge_repeated_expand_collapse_and_escape(self):
        app = self.app
        left, top, right, bottom = panel.work_area(self.root)
        for x in (left + 1, right - app.px(app.WIDTH) - 1):
            for y in (top + 1, bottom - app.px(app.BAR_HEIGHT) - 1):
                app.set_expanded(False)
                self.root.geometry(f"+{x}+{y}")
                self.root.update()
                for _ in range(3):
                    self.click(778, 24)
                    self.assertTrue(app.expanded)
                    self.assertGreaterEqual(self.root.winfo_y(), top)
                    self.assertLessEqual(self.root.winfo_y() + self.root.winfo_height(), bottom,
                                         (x, y, app.scale, self.root.geometry(), app.compact_position, app.drag_origin))
                    self.root.focus_force()
                    self.root.event_generate("<Escape>")
                    self.root.update()
                    self.assertFalse(app.expanded)
                    self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (x, y))


def preview():
    root, app = make_panel("preview")
    root.overrideredirect(False)
    kind = "紧凑" if "--compact" in sys.argv else "错误" if "--error" in sys.argv else "明细"
    root.title("Codex 面板 · 隔离界面验证（" + kind + "）")
    app.set_expanded("--compact" not in sys.argv)
    scenario(app, "读取失败" if "--error" in sys.argv else "空闲")
    controls = tk.Toplevel(root)
    controls.title("面板验证控制")
    controls.geometry("720x70+40+600")
    for name in ("等待会话", "读取中", "等待统计", "生成中", "空闲", "读取失败", "大数值"):
        tk.Button(controls, text=name, command=lambda n=name: scenario(app, n)).pack(side="left", padx=4, pady=10)
    def refresh():
        if not app.closed:
            app.refresh_transient()
            root.after(150, refresh)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    panel.enable_dpi()
    if "--preview" in sys.argv:
        preview()
    else:
        unittest.main(verbosity=2)
