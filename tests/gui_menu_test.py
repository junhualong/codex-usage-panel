"""Exercise the actual themed popup loop and Tk input, using synthetic panel data."""
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

from gui_panel_test import make_panel
from panel_menu import ThemedMenu, menu_border, menu_position
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

    def post(self, menu, action, area=None, position=None):
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
        self.app.active_popup, self.app.menu_open = popup, True
        try:
            popup.show(*(position or (area[2]-5, area[3]-5)))
        finally:
            self.app.active_popup, self.app.menu_open = None, False
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

    def native_windows(self):
        import ctypes
        from ctypes import wintypes
        import os
        if os.name != "nt":
            self.skipTest("Windows stacking regression")
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindow.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        return user

    @staticmethod
    def hwnd(window):
        return int(str(window.tk.call("wm", "frame", window)), 0)

    def test_native_submenu_never_exposes_panel_at_overlapping_menu_row(self):
        from ctypes import wintypes
        user = self.native_windows()
        root_handle = self.hwnd(self.root)
        for pinned in (True, False):
            self.app.topmost = pinned
            self.root.attributes("-topmost", pinned)
            self.root.geometry(f"{self.app.px(self.app.WIDTH)}x{self.app.px(self.app.BAR_HEIGHT)}+320+400")
            self.root.update()
            before = self.app.stats_text()
            model = self.app.new_menu()
            model.add_cascade(label="主题", menu=self.app.appearance_menu("theme", model))
            for i in range(8):
                model.add_command(label=f"测试选项 {i}")
            # No room above/below: force a real overlap to test stacking, not placement.
            area = (300, 386, 1500, 400+self.root.winfo_height()+14)
            def check(popup, node):
                node.activate(0, cascade=True)
                self.root.update()
                child = popup.windows[1]
                main_handle, child_handle = self.hwnd(node.window), self.hwnd(child.window)
                point = wintypes.POINT(node.window.winfo_x()+node.gutter+5, 420)
                self.assertEqual(user.GetAncestor(user.WindowFromPoint(point), 2), main_handle)
                self.assertEqual(user.GetWindow(main_handle, 4), root_handle)
                self.assertEqual(user.GetWindow(child_handle, 4), main_handle)
                for handle in (main_handle, child_handle):
                    style = user.GetWindowLongPtrW(handle, -16)
                    self.assertTrue(style & 0x80000000)  # Keep WS_POPUP; no child-window clipping.
                    self.assertFalse(style & 0x40000000)
                for phase, refresh in (("raise", self.root.lift), ("render", self.app.render),
                                       ("material", lambda: self.app.refresh_visual_settings(force=True)),
                                       ("icon", self.app.update_icon), ("refresh", self.app.refresh_state)):
                    refresh()
                    self.root.update()
                    self.assertEqual(user.GetAncestor(user.WindowFromPoint(point), 2), main_handle,
                                     (pinned, phase))
                    self.assertEqual(bool(self.root.attributes("-topmost")), pinned)
                child.key(SimpleNamespace(keysym="Left"))
                self.root.update()
                self.assertEqual(user.GetAncestor(user.WindowFromPoint(point), 2), main_handle)
            self.post(model, check, area, (380, 390))
            self.assertEqual(self.app.stats_text(), before)
            self.assertEqual(self.app.topmost, pinned)

    def test_main_and_child_menus_avoid_panel_at_top_and_bottom_at_all_scales(self):
        area = panel.work_area(self.root)
        left, top, right, bottom = area
        for scale in (.8, 1.25, 2):
            for at_bottom in (False, True):
                self.app.place_window(left+20, bottom if at_bottom else top+20, scale=scale, area=area)
                self.app.render()
                self.root.update()
                ox, oy = self.root.winfo_x(), self.root.winfo_y()
                owner = (ox, oy, ox+self.root.winfo_width(), oy+self.root.winfo_height())
                gap = max(4, round(8*self.app.scale))
                def check(popup, node):
                    node.activate(2, cascade=True)
                    self.root.update()
                    self.assertEqual(len(popup.windows), 2)
                    for entry in popup.windows:
                        x, y = entry.window.winfo_x(), entry.window.winfo_y()
                        self.assertGreaterEqual(x, left)
                        self.assertGreaterEqual(y, top)
                        self.assertLessEqual(x+entry.width, right)
                        self.assertLessEqual(y+entry.height, bottom)
                        if at_bottom:
                            self.assertLessEqual(y+entry.height+gap, owner[1])
                        else:
                            self.assertGreaterEqual(y, owner[3]+gap)
                    self.assertEqual(bool(self.root.attributes("-topmost")), self.app.topmost)
                self.post(self.context_model(), check, area, (owner[2]-30, oy+10))

    def test_negative_coordinates_and_short_work_area_placement(self):
        area = (-1600, -900, 0, -40)
        for owner, y in (((-1500, -500, -770, -455), -480), ((-1500, -100, -770, -55), -80)):
            x, placed_y = menu_position(-1480, y, 150, 240, area, owner, 8)
            self.assertGreaterEqual(x, area[0])
            self.assertLessEqual(x+150, area[2])
            self.assertGreaterEqual(placed_y, area[1])
            self.assertLessEqual(placed_y+240, area[3])
            self.assertTrue(placed_y+240 <= owner[1]-8 or placed_y >= owner[3]+8)
        parent = (-1400, -350, -1250, -108)
        x, y = menu_position(parent[2]-1, -150, 180, 300, area, (-1500, -100, -770, -55), 8, parent)
        self.assertEqual(x, parent[2]-1)
        self.assertLessEqual(y+300, -108)
        self.assertLess(y, parent[3])  # Parent and child remain vertically connected.
        self.assertGreater(y+300, parent[1])
        self.assertEqual(menu_position(100, 40, 150, 100, (0, 0, 600, 100), (20, 30, 580, 80), 8), (100, 0))

    def test_usage_updates_continue_while_popup_keeps_native_hit_target(self):
        from ctypes import wintypes
        from dataclasses import replace
        user = self.native_windows()
        def check(popup, node):
            before_mode, before_pin = self.app.mode, self.app.topmost
            updated = replace(self.app.snapshot, total=panel.Usage.parse({"input_tokens": 9000, "output_tokens": 1000}))
            self.app.queue.put_nowait((self.app.generation, updated))
            self.app.refresh_state()
            self.root.update()
            self.assertEqual(self.app.snapshot.total.total_tokens, 10000)
            self.assertEqual((self.app.mode, self.app.topmost), (before_mode, before_pin))
            point = wintypes.POINT(node.window.winfo_x()+node.gutter, node.window.winfo_y()+node.row_height)
            self.assertEqual(user.GetAncestor(user.WindowFromPoint(point), 2), self.hwnd(node.window))
            self.assertEqual(len(popup.windows), 1)
        self.post(self.context_model(), check)

    def test_menu_placement_preserves_size_position_pin_and_settings(self):
        before = (self.root.winfo_geometry(), self.app.zoom, self.app.topmost,
                  (self.app.state_dir / "settings.json").read_bytes())
        for model in (self.context_model(), self.app.appearance_menu("theme"), self.app.appearance_menu("icon")):
            self.post(model, lambda popup, node: node.key(SimpleNamespace(keysym="Escape")),
                      position=(self.root.winfo_x()+100, self.root.winfo_y()+20))
        after = (self.root.winfo_geometry(), self.app.zoom, self.app.topmost,
                 (self.app.state_dir / "settings.json").read_bytes())
        self.assertEqual(after, before)

    def test_native_overlap_stays_clickable_during_sustained_refresh(self):
        from ctypes import wintypes
        from dataclasses import replace
        user = self.native_windows()
        self.root.geometry(f"{self.app.px(self.app.WIDTH)}x{self.app.px(self.app.BAR_HEIGHT)}+320+400")
        self.app.snapshot = replace(self.app.snapshot, active=True)
        self.root.update()
        area = (300, 386, 1500, 400+self.root.winfo_height()+14)
        def check(popup, node):
            node.activate(2, cascade=True)
            self.root.update()
            point = wintypes.POINT(node.window.winfo_x()+node.gutter+5, 420)
            expected = self.hwnd(node.window)
            finished, errors, samples = tk.BooleanVar(self.root, False), [], []
            def sample():
                try:
                    self.assertEqual(user.GetAncestor(user.WindowFromPoint(point), 2), expected)
                    samples.append(len(samples))
                    if len(samples) == 20:
                        finished.set(True)
                        return
                    if len(samples) % 3 == 0:
                        self.app.render()
                    self.app.refresh_state()
                    self.root.after(40, sample)
                except Exception as exc:
                    errors.append(exc)
                    finished.set(True)
            timeout = self.root.after(4000, lambda: finished.set(True))
            try:
                sample()
                if not finished.get():
                    self.root.wait_variable(finished)
            finally:
                self.root.after_cancel(timeout)
            if errors:
                raise errors[0]
            self.assertEqual(len(samples), 20)
        self.post(self.context_model(), check, area, (380, 390))

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
        user = self.native_windows()
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
            index = next(i for i in range(node.model.index("end") + 1)
                         if node.model.type(i) == "command" and node.model.entrycget(i, "label") == "退出")
            self.click_row(node, index)
        self.post(self.context_model(), choose)
        self.assertTrue(self.app.closed)


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)
