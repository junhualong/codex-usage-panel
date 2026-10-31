"""Exercise the actual themed popup loop and Tk input, using synthetic panel data."""
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

from gui_panel_test import make_panel
from panel_menu import ThemedMenu, menu_border
from panel_visuals import THEMES
from test_usage import panel


class MenuGuiTests(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_panel("menu-" + self._testMethodName)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args))

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.assertFalse(self.errors)

    def post(self, menu, action, area=None):
        area = area or panel.work_area(self.root)
        popup = ThemedMenu(self.root, menu, self.app.theme, self.app.font(12), self.app.scale, area)
        failures = []
        def exercise():
            try:
                self.assertEqual(len(popup.windows), 1)
                action(popup, popup.windows[0])
            except Exception as exc:
                failures.append(exc)
            finally:
                popup.dismiss()
        self.root.after(40, exercise)
        popup.show(area[2]-5, area[3]-5)
        if failures:
            raise failures[0]
        self.assertFalse(popup.windows)
        if not self.app.closed:
            self.assertIsNone(self.root.grab_current())
        return popup

    def click_row(self, node, index):
        top, bottom = node.rows[index][:2]
        x, y = node.gutter+5, round((top+bottom)/2-node.canvas.canvasy(0))
        for event in ("<Motion>", "<ButtonPress-1>", "<ButtonRelease-1>"):
            node.canvas.event_generate(event, x=x, y=y)

    def context_model(self):
        with patch.object(ThemedMenu, "show"):
            self.app.context_menu(SimpleNamespace(x_root=40, y_root=80))
        return self.app.context_popup

    def test_theme_surfaces_and_all_menu_sizes_stay_inside_work_area(self):
        for theme in THEMES:
            self.app.set_theme(theme)
            for scale in (.8, 1.25, 2):
                self.app.scale = scale
                self.app.fonts.clear()
                for kind in ("theme", "icon"):
                    def check(popup, node):
                        left, top, right, bottom = popup.area
                        self.assertTrue(node.window.overrideredirect())
                        self.assertEqual(node.window.cget("bg"), menu_border(self.app.theme))
                        self.assertEqual(node.canvas.cget("bg"), self.app.theme.surface)
                        self.assertEqual(node.canvas.cget("highlightthickness"), "0")
                        self.assertGreaterEqual(node.window.winfo_x(), left)
                        self.assertGreaterEqual(node.window.winfo_y(), top)
                        self.assertLessEqual(node.window.winfo_x()+node.width, right)
                        self.assertLessEqual(node.window.winfo_y()+node.height, bottom)
                        self.assertEqual(sum(row[5] for row in node.rows), 1)
                    self.post(self.app.appearance_menu(kind), check)

    def test_mouse_selection_keeps_independent_theme_and_updates_model(self):
        self.app.set_icon("pulse")
        theme = self.app.theme_id
        def choose(popup, node):
            self.assertTrue(node.rows[2][5])
            self.click_row(node, 3)
        self.post(self.app.appearance_menu("icon"), choose)
        self.assertEqual((self.app.icon, self.app.icon_variable.get()), ("orbit", "orbit"))
        self.assertEqual(self.app.theme_id, theme)

    def test_negative_monitor_coordinates_remain_absolute(self):
        area = (-1600, -900, 0, -40)
        def check(popup, node):
            self.assertEqual(node.window.winfo_x(), area[2]-node.width)
            self.assertEqual(node.window.winfo_y(), area[3]-node.height)
        self.post(self.app.appearance_menu("theme"), check, area)

    def test_windows_popup_is_a_visible_top_level_surface(self):
        import ctypes
        from ctypes import wintypes
        import os
        if os.name != "nt":
            self.skipTest("Windows surface regression")
        user = ctypes.WinDLL("user32")
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        def check(popup, node):
            hwnd = int(str(node.window.tk.call("wm", "frame", node.window)), 0)
            style = user.GetWindowLongPtrW(hwnd, -16)
            self.assertTrue(style & 0x80000000)  # WS_POPUP can paint over other applications.
            self.assertFalse(style & 0x40000000)  # WS_CHILD is not a desktop popup.
            self.assertTrue(user.IsWindowVisible(hwnd))
        self.post(self.app.appearance_menu("icon"), check)

    def test_keyboard_scope_selection_uses_existing_statistics_callbacks(self):
        with patch.object(ThemedMenu, "show"):
            self.app.open_scope_menu()
        def choose(popup, node):
            for key in ("Down", "Down", "Return"):
                node.window.event_generate("<KeyPress>", keysym=key)
        self.post(self.app.scope_menu, choose)
        self.assertEqual(self.app.mode, "last")
        self.assertIn("统计范围：最近调用", self.app.stats_text())

    def test_context_submenu_flips_left_and_keyboard_selects_theme(self):
        icon = self.app.icon
        def choose(popup, node):
            node.activate(2)
            node.key(SimpleNamespace(keysym="Right"))
            self.assertEqual(len(popup.windows), 2)
            child = popup.windows[1]
            self.assertLess(child.window.winfo_x(), node.window.winfo_x())
            for key in ("Down", "Down", "Return"):
                child.window.event_generate("<KeyPress>", keysym=key)
        self.post(self.context_model(), choose)
        self.assertEqual((self.app.theme_id, self.app.icon), ("frost", icon))

    def test_mouse_can_return_from_focused_submenu_and_select_another_cascade(self):
        def choose(popup, node):
            node.activate(2)
            node.key(SimpleNamespace(keysym="Right"))
            popup.windows[1].key(SimpleNamespace(keysym="Left"))
            self.root.update()
            self.assertEqual(len(popup.windows), 1)
            node.key(SimpleNamespace(keysym="Right"))
            node.activate(3, cascade=True)
            self.root.update()
            self.assertEqual(len(popup.windows), 2)
            child = popup.windows[1]
            self.assertEqual(child.rows[0][3], "终端光标")
            self.click_row(child, 4)
        self.post(self.context_model(), choose)
        self.assertEqual(self.app.icon, "gear")

    def test_disabled_rows_escape_and_outside_click_do_not_invoke_commands(self):
        self.app.set_expanded(True)
        before = self.app.stats_text()
        def cancel(popup, node):
            self.click_row(node, 4)  # Follow status is disabled.
            self.assertIsNone(popup.pending)
            node.window.event_generate("<KeyPress>", keysym="Escape")
            self.assertFalse(popup.windows)
        self.post(self.context_model(), cancel)
        self.assertTrue(self.app.expanded)
        self.assertEqual(self.app.stats_text(), before)
        def outside(popup, node):
            node.window.event_generate("<ButtonPress-1>", x=-10, y=-10)
            self.assertFalse(popup.windows)
            self.assertIsNone(popup.pending)
        self.post(self.app.appearance_menu("icon"), outside)

    def test_long_menu_scrolls_to_keyboard_selection_on_short_screen(self):
        chosen = []
        menu = self.app.new_menu()
        for index in range(30):
            menu.add_command(label=f"隔离测试选项 {index}", command=lambda i=index: chosen.append(i))
        def choose(popup, node):
            node.key(SimpleNamespace(keysym="End"))
            self.assertEqual(node.active, 29)
            self.assertLessEqual(node.rows[29][1], node.canvas.canvasy(0)+node.height-2)
            node.key(SimpleNamespace(keysym="Return"))
        self.post(menu, choose, (0, 0, 600, 240))
        self.assertEqual(chosen, [29])

    def test_external_focus_mapping_and_owner_shutdown_release_popup(self):
        def focus_lost(popup, node):
            other = tk.Toplevel(self.root)
            other.withdraw()
            # A local Tk grab may redirect in-process focus requests back to the
            # popup. Inject the external-focus observation, not an impossible request.
            with patch.object(self.root, "focus_displayof", return_value=other):
                node.window.event_generate("<FocusOut>")
                self.root.update_idletasks()
            other.destroy()
            self.assertFalse(popup.windows)
        self.post(self.app.appearance_menu("theme"), focus_lost)
        self.post(self.app.appearance_menu("icon"), lambda popup, node: self.app.close())
        self.assertTrue(self.app.closed)

    def test_context_exit_command_runs_after_popup_cleanup(self):
        def choose(popup, node):
            self.click_row(node, 10)
        self.post(self.context_model(), choose)
        self.assertTrue(self.app.closed)


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)
