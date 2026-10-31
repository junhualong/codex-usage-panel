"""Independent theme/icon choices in isolated Tk fixtures; no real user settings."""
import json
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from gui_panel_test import make_panel, scenario
from test_usage import panel
from panel_visuals import THEMES, ICONS
from panel_icons import frame_for, render_icon


class AppearanceGuiTests(unittest.TestCase):
    def setUp(self):
        self.root, self.app = make_panel("appearance-" + self._testMethodName)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args))

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.assertFalse(self.errors)

    def click(self, x, y):
        c = self.app.canvas
        for event in ("<Motion>", "<ButtonPress-1>", "<ButtonRelease-1>"):
            c.event_generate(event, x=self.app.px(x), y=self.app.px(y))
            self.root.update()

    def test_footer_and_context_menus_select_independently(self):
        app = self.app
        app.set_expanded(True)
        for zoom in (.8, 1.0, 1.5):
            app.place_window(20, 20, scale=zoom)
            app.render()
            self.root.update_idletasks()
            for kind, x in (("theme", 300), ("icon", 530)):
                with patch.object(panel.ThemedMenu, "show") as popup:
                    self.click(x, 349)
                    popup.assert_called_once()
                menu = app.appearance_menus[kind]
                self.assertEqual(menu.index("end") + 1, len(THEMES if kind == "theme" else ICONS))
                self.assertEqual(menu.type(0), "radiobutton")
        app.appearance_menus["icon"].invoke(list(ICONS).index("gear"))
        app.appearance_menus["theme"].invoke(list(THEMES).index("frost"))
        self.assertEqual((app.theme_id, app.icon), ("frost", "gear"))
        with patch.object(panel.ThemedMenu, "show"):
            app.context_menu(SimpleNamespace(x_root=40, y_root=80))
        menu = app.context_popup
        theme_menu = menu.nametowidget(menu.entrycget(2, "menu"))
        icon_menu = menu.nametowidget(menu.entrycget(3, "menu"))
        self.assertEqual(str(theme_menu.entrycget(0, "variable")), str(app.theme_variable))
        self.assertEqual(str(icon_menu.entrycget(0, "variable")), str(app.icon_variable))
        icon_menu.invoke(list(ICONS).index("orbit"))
        theme_menu.invoke(list(THEMES).index("sand"))
        self.assertEqual((app.theme_variable.get(), app.icon_variable.get()), ("sand", "orbit"))

    def test_all_combinations_keep_statistics_scope_and_window_state(self):
        app = self.app
        app.set_mode("last")
        app.set_expanded(True)
        before = (app.stats_text(), app.mode, app.path, app.follow_enabled, app.topmost,
                  app.zoom, app.compact_position, self.root.geometry())
        for icon in ICONS:
            app.set_icon(icon)
            for key, theme in THEMES.items():
                app.set_theme(key)
                self.assertEqual((app.theme_id, app.icon), (key, icon))
                self.assertEqual(app.canvas.itemcget("speed_value", "fill"), theme.accent)
                self.assertEqual(app.canvas.itemcget("theme_label", "text"), "主题：" + theme.name)
                self.assertEqual(app.canvas.itemcget("icon_label", "text"), "图标：" + ICONS[icon])
                self.assertEqual((app.stats_text(), app.mode, app.path, app.follow_enabled, app.topmost,
                                  app.zoom, app.compact_position, self.root.geometry()), before)
                self.assertEqual(app.icon_frame.ink, theme.accent)
                self.assertEqual(app.icon_frame.surface, theme.surface)
                self.assertEqual(app.canvas.type("status_icon"), "image")
                self.assertEqual(app.canvas.itemcget("status_icon", "image"), str(app.icon_image))
                self.assertEqual((app.icon_image.width(), app.icon_image.height()), app.icon_frame.size)
        app.copy_stats()
        self.assertEqual(self.root.clipboard_get(), before[0])

    def test_reopen_restores_both_choices_and_existing_preferences(self):
        app = self.app
        app.set_theme("teal")
        app.set_icon("terminal")
        app.set_mode("last")
        app.topmost = False
        app.zoom = .8
        app.place_window(120, 100, scale=app.dpi_scale * app.zoom)
        app.save_settings()
        saved = json.loads((app.state_dir / "settings.json").read_text(encoding="utf-8"))
        config, folder = app.config, app.state_dir
        app.close()
        self.root = tk.Tk()
        with patch.object(panel.UsagePanel, "start_reader"), patch.object(panel, "read_titles", return_value={}):
            self.app = panel.UsagePanel(self.root, config, folder)
        self.root.after_cancel(self.app.poll_job)
        self.app.poll_job = None
        self.root.update_idletasks()
        self.assertEqual((self.app.theme_id, self.app.icon, self.app.mode, self.app.topmost, self.app.zoom),
                         ("teal", "terminal", "last", False, .8))
        self.assertEqual(self.app.compact_position, (saved["x"], saved["y"]))

    def test_all_combinations_fit_minimum_and_scaled_layouts(self):
        app = self.app
        with patch.object(panel, "visual_preferences", return_value=(False, False)):
            for theme in THEMES:
                app.set_theme(theme)
                for icon in ICONS:
                    app.set_icon(icon)
                    for scale in (.8, 1.25, 2.0):
                        app.scale = scale
                        app.fonts.clear()
                        for expanded in (False, True):
                            app.expanded = expanded
                            height = app.EXPANDED_HEIGHT if expanded else app.BAR_HEIGHT
                            self.root.geometry(f"{app.px(app.WIDTH)}x{app.px(height)}+20+50")
                            scenario(app, "大数值")
                            boxes = []
                            for item in app.canvas.find_all():
                                if app.canvas.type(item) != "text":
                                    continue
                                box = app.canvas.bbox(item)
                                label = app.canvas.itemcget(item, "text")
                                self.assertTrue(0 <= box[0] <= box[2] <= app.px(app.WIDTH), (theme, icon, label, box))
                                self.assertTrue(0 <= box[1] <= box[3] <= app.px(height), (theme, icon, label, box))
                                for other in boxes:
                                    self.assertFalse(min(box[2], other[2]) > max(box[0], other[0]) and
                                                     min(box[3], other[3]) > max(box[1], other[1]), label)
                                boxes.append(box)
                            icon_box = app.canvas.bbox("status_icon")
                            self.assertLess(icon_box[2], app.px(62))
                            self.assertGreater(icon_box[0], 0)

    def test_animation_reuses_items_and_error_has_shape_cue(self):
        app = self.app
        for icon in ICONS:
            app.set_icon(icon)
            for theme in ("frost", "sand"):
                app.set_theme(theme)
                scenario(app, "生成中")
                app.animation_enabled = True
                app.hover(SimpleNamespace(x_root=40, y_root=80), "scope", True)
                ids, stats = app.canvas.find_all(), app.stats_text()
                for elapsed in (0, .55, 2.1, 5.1):
                    app.animation_start = time.monotonic() - elapsed
                    app.update_icon()
                    self.assertEqual(app.canvas.find_all(), ids)
                    self.assertEqual(app.stats_text(), stats)
                    self.assertEqual(app.hovered_tag, "scope")
                for state in ("等待会话", "读取中", "等待统计", "空闲", "读取失败"):
                    scenario(app, state)
                    self.assertEqual(app.icon_frame.error, state == "读取失败")
                    self.assertEqual(app.icon_frame.ink, app.theme.warning if state == "读取失败" else
                                     app.theme.accent if state == "空闲" else app.theme.muted)
                scenario(app, "生成中")
                app.animation_enabled = False
                app.update_icon()
                frame, image = app.icon_frame, app.icon_image
                app.animation_start -= 10
                app.update_icon()
                self.assertEqual(app.icon_frame, frame)
                self.assertIs(app.icon_image, image)
                self.assertEqual(float(self.root.attributes("-alpha")), 1)

    def test_tk_image_cache_is_reused_bounded_and_cleared_on_appearance_changes(self):
        app = self.app
        cache = app.icon_images
        self.assertIs(cache.get(app.icon_frame), app.icon_image)
        for tick in range(400):
            frame = frame_for("pulse", .8, "生成中", tick*.065, app.theme)
            cache.get(frame)
            self.assertLessEqual(len(cache.images), cache.LIMIT)
        frame = frame_for("gear", 1.25, "等待新的调用", 0, THEMES["nebula"])
        image = cache.get(frame)
        self.assertEqual(len(cache.images), 1)
        self.assertIs(cache.get(frame), image)
        # Inspect the actual Tk image: transparent padding and opaque colored ink survive upload.
        raster = render_icon(frame)
        x, y = next((x, y) for y in range(raster.height) for x in range(raster.width)
                    if raster.getpixel((x, y))[3] == 255)
        self.assertEqual(self.root.tk.call(str(image), "get", x, y), raster.getpixel((x, y))[:3])
        self.assertEqual(self.root.tk.call(str(image), "transparency", "get", 0, 0), 1)
        app.close()
        self.assertFalse(cache.images)
        self.assertIsNone(app.icon_image)

    def test_theme_fallback_and_bottom_edge_anchor_survive_switching(self):
        app = self.app
        left, top, right, bottom = panel.work_area(self.root)
        app.place_window(right-app.px(app.WIDTH), bottom-app.px(app.BAR_HEIGHT))
        self.root.update_idletasks()
        anchor = app.compact_position
        app.set_expanded(True)
        with patch.object(panel, "visual_preferences", return_value=(False, False)):
            for theme in THEMES:
                app.set_theme(theme)
                app.refresh_visual_settings(force=True)
                self.assertEqual(app.background, THEMES[theme].background)
                self.assertFalse(app.material.enabled)
                self.assertFalse(app.animation_enabled)
                for icon in ICONS:
                    app.set_icon(icon)
                    self.assertEqual(app.compact_position, anchor)
                    self.assertLessEqual(self.root.winfo_y() + self.root.winfo_height(), bottom)
        app.set_expanded(False)
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), anchor)
        with patch.object(panel, "visual_preferences", return_value=(True, True)):
            app.set_theme("frost")
            self.assertFalse(app.material.enabled)
            self.assertEqual(app.background, THEMES["frost"].background)
            self.assertIn("确保深色文字清晰", app.material.reason)
            self.assertEqual(app.canvas.itemcget("speed_value", "fill"), THEMES["frost"].accent)


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)
