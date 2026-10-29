"""B-theme checks on isolated Tk windows; no real session or OS preference changes."""
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from gui_panel_test import make_panel, scenario
from test_usage import panel
from panel_visuals import robot_pose


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_panel("visual-" + self._testMethodName)

    def tearDown(self):
        if not self.app.closed:
            self.app.close()

    def test_minimum_type_and_core_values_fit_at_multiple_dpi_levels(self):
        app = self.app
        for dpi in (1, 1.25, 1.5, 2):
            app.dpi_scale, app.scale = dpi, dpi * app.MIN_ZOOM
            app.fonts.clear()
            for state in ("空闲", "大数值", "等待会话", "读取失败"):
                scenario(app, state)
                for item in app.canvas.find_all():
                    if app.canvas.type(item) == "text":
                        actual = int(app.root.tk.call("font", "configure", app.canvas.itemcget(item, "font"), "-size"))
                        self.assertGreaterEqual(abs(actual), round(12 * dpi))
                for tag in ("speed_value", "compact_total", "cache_rate"):
                    item = app.canvas.find_withtag(tag)[0]
                    actual = int(app.root.tk.call("font", "configure", app.canvas.itemcget(item, "font"), "-size"))
                    self.assertGreaterEqual(abs(actual), round(16 * dpi))
                if state == "大数值":
                    self.assertIn("e+", app.canvas.itemcget("compact_total", "text"))
                    self.assertIn("987,654,321,098,765,432", app.stats_text())

    def test_full_cache_and_extreme_speed_do_not_collide_at_minimum(self):
        app = self.app
        app.scale, app.dpi_scale = .8, 1
        app.fonts.clear()
        app.snapshot = replace(app.snapshot, total=panel.Usage(10000, 10000, 0, 0, 10000),
                               speed=panel.SpeedSample(100000000, 1))
        app.render()
        boxes = [app.canvas.bbox(i) for i in app.canvas.find_all() if app.canvas.type(i) == "text"]
        for index, box in enumerate(boxes):
            for other in boxes[index + 1:]:
                self.assertFalse(min(box[2], other[2]) > max(box[0], other[0]) and
                                 min(box[3], other[3]) > max(box[1], other[1]))

    def test_pose_uses_actual_state_and_reduced_motion_is_static(self):
        self.assertNotEqual(robot_pose("生成中", 0), robot_pose("生成中", .55))
        self.assertNotEqual(robot_pose("等待新的调用", 0), robot_pose("等待新的调用", 5.1))
        for state in ("等待会话", "等待统计", "读取中", "读取失败"):
            self.assertEqual(robot_pose(state, 0), robot_pose(state, 5.1))
        self.assertEqual(robot_pose("读取失败", 0)[2], "#f0c68b")
        for state in ("生成中", "等待新的调用"):
            self.assertEqual(robot_pose(state, 0, False), robot_pose(state, 5.1, False))

    def test_robot_reuses_items_and_does_not_clear_hover_or_change_stats(self):
        app = self.app
        scenario(app, "生成中")
        app.hover(SimpleNamespace(x_root=40, y_root=80), "scope", True)
        before_ids, before_stats = app.canvas.find_all(), app.stats_text()
        for elapsed in (0, .55, 5.1):
            app.animation_start = time.monotonic() - elapsed
            app.update_robot()
        self.assertEqual(app.canvas.find_all(), before_ids)
        self.assertEqual(app.stats_text(), before_stats)
        self.assertEqual(app.hovered_tag, "scope")
        self.assertEqual(app.canvas.itemcget("scope_bg", "fill"), panel.HOVER)
        self.assertEqual(float(self.root.attributes("-alpha")), 1)

    def test_preferences_fallback_restores_controls_and_keeps_anchor(self):
        app = self.app
        anchor = (self.root.winfo_x(), self.root.winfo_y())
        with patch.object(panel, "visual_preferences", return_value=(False, False)):
            app.preference_check = 0
            app.refresh_visual_settings()
        self.assertFalse(app.material.enabled)
        self.assertFalse(app.animation_enabled)
        self.assertEqual(app.background, panel.BACKGROUND)
        app.set_expanded(True)
        app.set_expanded(False)
        self.assertEqual(app.compact_position, anchor)
        app.copy_stats()
        self.assertEqual(self.root.clipboard_get(), app.stats_text())

    def test_unsupported_native_api_falls_back_without_losing_ui(self):
        app = self.app
        if not app.material.hwnd:
            self.skipTest("Native Windows API unavailable on this platform")
        with patch.object(app.material.user, "SetWindowCompositionAttribute", return_value=0):
            app.material.apply(True)
        self.assertFalse(app.material.enabled)
        self.assertIn("实色背景", app.material.reason)

    def test_close_cancels_animation_and_frees_tk_window(self):
        app = self.app
        self.assertIsNotNone(app.animation_job)
        app.close()
        self.assertIsNone(app.animation_job)
        self.assertTrue(app.closed)


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)
