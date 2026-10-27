"""Exercise bounded dragging and proportional border resizing on real isolated Tk windows."""
import json
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from gui_panel_test import make_panel
from test_usage import panel


class WindowInteractionTests(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_panel("window-" + self._testMethodName)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args))

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.assertFalse(self.errors)

    def bounds(self):
        self.root.update_idletasks()
        return (self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())

    def assert_visible(self, area=None):
        x, y, w, h = self.bounds()
        left, top, right, bottom = area or panel.work_area(self.root)
        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(x + w, right)
        self.assertLessEqual(y + h, bottom)

    def gesture(self, x, y, dx, dy):
        app, canvas = self.app, self.app.canvas
        x, y = app.px(x), app.px(y)
        origin_x, origin_y = self.root.winfo_x(), self.root.winfo_y()
        canvas.event_generate("<Motion>", x=x, y=y)
        canvas.event_generate("<ButtonPress-1>", x=x, y=y)
        self.root.update()
        canvas.event_generate("<B1-Motion>", x=x + dx, y=y + dy,
                              rootx=origin_x + x + dx, rooty=origin_y + y + dy)
        self.root.update()
        canvas.event_generate("<ButtonRelease-1>", x=x + dx, y=y + dy)
        self.root.update()
        self.assertIsNone(app.drag_origin)
        self.assertIsNone(app.resize_origin)
        self.assertIsNone(canvas.grab_current())

    def reopen(self, settings=None):
        config, folder = self.app.config, self.app.state_dir
        self.app.close()
        if settings is not None:
            (folder / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args))
        with patch.object(panel.UsagePanel, "start_reader"), patch.object(panel, "read_titles", return_value={}):
            self.app = panel.UsagePanel(self.root, config, folder)
        self.root.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.root.update()

    def test_drag_cannot_cross_any_work_area_edge_in_either_view(self):
        for expanded in (False, True):
            self.app.set_expanded(expanded)
            for dx, dy in ((-10000, -10000), (10000, -10000), (10000, 10000), (-10000, 10000)):
                self.gesture(249, 24, dx, dy)
                self.assert_visible()

    def test_all_borders_and_corners_resize_with_matching_cursor(self):
        for expanded in (False, True):
            self.app.set_expanded(expanded)
            height = self.app.EXPANDED_HEIGHT if expanded else self.app.BAR_HEIGHT
            for edge, x, y, dx, dy in (
                ("e", 838, height / 2, 120, 0), ("w", 2, height / 2, -120, 0),
                ("n", 420, 2, 0, -12), ("s", 420, height - 2, 0, 12),
                ("nw", 6, 6, -120, -24), ("ne", 834, 6, 120, -24),
                ("sw", 6, height - 6, -120, 24), ("se", 834, height - 6, 120, 24)):
                self.app.reset_size()
                self.app.place_window(250, 180)
                self.root.update()
                before = self.app.scale
                self.app.canvas.event_generate("<Motion>", x=self.app.px(x), y=self.app.px(y))
                self.root.update()
                self.assertEqual(self.app.canvas.cget("cursor"), self.app.RESIZE_CURSORS[edge], edge)
                self.gesture(x, y, dx, dy)
                self.assertGreater(self.app.scale, before, (expanded, edge))
                self.assert_visible()
                self.assertFalse(self.app.closed)
                self.assertEqual(self.app.expanded, expanded)

    def test_minimum_and_maximum_size_keep_controls_accessible(self):
        for expanded in (False, True):
            self.app.set_expanded(expanded)
            height = self.app.EXPANDED_HEIGHT if expanded else self.app.BAR_HEIGHT
            self.gesture(838, height / 2, -10000, 0)
            self.assertAlmostEqual(self.app.zoom, self.app.MIN_ZOOM)
            self.assert_visible()
            self.gesture(838, height / 2, 10000, 0)
            self.assertLessEqual(self.app.zoom, self.app.MAX_ZOOM)
            self.assert_visible()
            self.assertLessEqual(self.app.canvas.bbox("close")[2], self.root.winfo_width())

    def test_expanded_resize_preserves_the_compact_anchor_and_escape(self):
        left, top, right, bottom = panel.work_area(self.root)
        self.app.place_window(left + 30, bottom - self.app.px(self.app.BAR_HEIGHT))
        self.root.update()
        original = self.bounds()[:2]
        self.app.set_expanded(True)
        self.gesture(838, 182, 230, 0)
        expected = panel.clamp_position(*original, self.app.px(self.app.WIDTH), self.app.px(self.app.BAR_HEIGHT),
                                        (left, top, right, bottom))
        for _ in range(5):
            self.assert_visible()
            self.root.focus_force()
            self.root.event_generate("<Escape>")
            self.root.update()
            self.assertEqual(self.bounds()[:2], expected)
            self.app.set_expanded(True)
            self.root.update()

    def test_zoom_and_position_are_restored_and_default_size_is_available(self):
        self.gesture(838, 24, 180, 0)
        before, zoom = self.bounds(), self.app.zoom
        self.reopen()
        self.assertEqual(self.bounds(), before)
        self.assertAlmostEqual(self.app.zoom, zoom, places=5)
        with patch.object(self.app, "popup_menu") as popup:
            self.app.context_menu(SimpleNamespace(x_root=50, y_root=80))
        menu = popup.call_args.args[0]
        index = next(i for i in range(menu.index("end") + 1)
                     if menu.type(i) != "separator" and menu.entrycget(i, "label") == "恢复默认大小")
        menu.invoke(index)
        menu.destroy()
        self.assertEqual(self.app.zoom, 1.0)
        self.assert_visible()

    def test_startup_and_periodic_refresh_recover_an_unreachable_window(self):
        self.reopen({"x": 50000, "y": -50000, "zoom": "nan"})
        self.assert_visible()
        self.assertEqual(self.app.zoom, 1.0)
        self.root.geometry("+50000+50000")
        self.root.update()
        self.app.tick()
        self.assert_visible()

    def test_negative_monitor_coordinates_taskbar_and_small_high_dpi_area(self):
        for area, dpi in (((-1600, -900, 0, -40), 1.0), ((80, 50, 1104, 750), 2.0)):
            with patch.object(panel, "work_area", return_value=area):
                self.app.dpi_scale = dpi
                self.app.zoom = 2.0
                self.app.last_work_area = None
                self.app.ensure_visible()
                self.assert_visible(area)
                self.app.set_expanded(True)
                self.assert_visible(area)
                self.gesture(249, 24, -20000, 20000)
                self.assert_visible(area)
                self.app.set_expanded(False)
                self.assert_visible(area)


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)
