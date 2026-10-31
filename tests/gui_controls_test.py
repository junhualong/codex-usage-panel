"""Isolated GUI checks for tray commands, diagnostics and failed preference writes."""
import json
from pathlib import Path
import tkinter as tk
import unittest
from unittest.mock import patch
from uuid import uuid4

from gui_panel_test import make_panel
from test_usage import panel
from panel_control import PanelCommands, request
from panel_diagnostics import DiagnosticsWindow
import test_diagnostics


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_panel("controls-" + uuid4().hex[:8])
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(str(error))

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.assertFalse(self.errors)

    def test_ipc_hide_show_keeps_statistics_and_mode(self):
        suffix = "test-" + uuid4().hex
        commands = PanelCommands(suffix)
        self.app.commands = commands
        before = self.app.stats_text()
        try:
            self.assertTrue(request("hide", suffix))
            self.app.refresh_state()
            self.root.update()
            self.assertTrue(self.app.hidden)
            self.assertFalse(self.root.winfo_ismapped())
            self.assertTrue(request("show", suffix))
            self.app.refresh_state()
            self.root.update()
            self.assertFalse(self.app.hidden)
            self.assertTrue(self.root.winfo_ismapped())
            self.assertEqual(self.app.stats_text(), before)
            self.assertEqual(json.loads((self.app.state_dir / "status.json").read_text(encoding="utf-8"))["hidden"], False)
        finally:
            self.app.commands = None
            commands.close()

    def test_recenter_stays_in_work_area_and_preserves_size(self):
        self.app.set_expanded(True)
        zoom = self.app.zoom
        self.app.handle_command("recenter")
        self.root.update()
        left, top, right, bottom = panel.work_area(self.root)
        self.assertGreaterEqual(self.root.winfo_x(), left)
        self.assertGreaterEqual(self.root.winfo_y(), top)
        self.assertLessEqual(self.root.winfo_x() + self.root.winfo_width(), right)
        self.assertLessEqual(self.root.winfo_y() + self.root.winfo_height(), bottom)
        position = self.app.compact_position
        self.app.set_expanded(False)
        self.root.update()
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), position)
        self.assertEqual(self.app.zoom, zoom)

    def test_failed_save_keeps_old_file_and_retries_latest_preference(self):
        self.app.save_settings()
        path = self.app.state_dir / "settings.json"
        original = path.read_bytes()
        real_replace = Path.replace
        def fail_settings(source, target):
            if source.name == "settings.tmp":
                raise PermissionError("injected settings file lock")
            return real_replace(source, target)
        with patch.object(Path, "replace", fail_settings):
            self.app.set_theme("teal")
            self.assertTrue(self.app.settings_error)
            self.assertEqual(path.read_bytes(), original)
            notice = self.app.save_notice
            self.assertIsNotNone(notice)
            for _ in range(3):
                self.assertFalse(self.app.save_settings())
                self.assertIs(self.app.save_notice, notice)
            self.app.publish_status()
            self.assertIn("file lock", json.loads((self.app.state_dir / "status.json").read_text(encoding="utf-8"))["settings_error"])
            self.root.update()
            left, top, right, bottom = panel.work_area(self.root)
            self.assertGreaterEqual(notice.winfo_x(), left)
            self.assertGreaterEqual(notice.winfo_y(), top)
            self.assertLessEqual(notice.winfo_x() + notice.winfo_width(), right)
            self.assertLessEqual(notice.winfo_y() + notice.winfo_height(), bottom)
        self.app.settings_retry_at = 0
        self.app.refresh_transient()
        self.assertFalse(self.app.settings_error)
        self.assertIsNone(self.app.save_notice)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["theme"], "teal")
        log = (self.app.state_dir / "panel-error.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("[设置保存] 失败，自动重试"), 1)
        self.assertIn("[设置保存] 已恢复", log)

    def test_reload_clears_cache_without_changing_scope(self):
        original = self.app.mode
        with patch.object(self.app.read_cache, "invalidate") as invalidate, patch.object(self.app, "start_reader") as start:
            self.app.handle_command("reload")
        invalidate.assert_called_once_with(self.app.path)
        start.assert_called_once()
        self.assertEqual(self.app.mode, original)
        self.assertIn("已开始", self.app.last_action)

    def test_diagnostic_buttons_and_copy_use_safe_report(self):
        window = tk.Toplevel(self.root)
        report = test_diagnostics.DiagnosticsTests().sample()
        sent = []
        diagnostic = DiagnosticsWindow(window, provider=lambda: report, sender=lambda action: sent.append(action) or True)
        try:
            self.root.update()
            self.assertIn("secret-chat", diagnostic.body.get("1.0", "end"))
            diagnostic.buttons["reload"].invoke()
            self.assertEqual(sent, ["reload"])
            self.assertIn("请求已发送", diagnostic.feedback.get())
            diagnostic.copy()
            self.assertNotIn("secret", self.root.clipboard_get())
            window.after_cancel(diagnostic.job)
            report["panel_running"] = False
            diagnostic.refresh()
            self.assertTrue(diagnostic.buttons["reload"].instate(["disabled"]))
            self.assertNotIn("secret-chat", diagnostic.body.get("1.0", "end"))
        finally:
            diagnostic.close()


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)

