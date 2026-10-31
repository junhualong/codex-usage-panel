"""Appearance preferences and state motion contracts without desktop or real chats."""
import unittest
from test_usage import panel  # Establish the same project import path as other tests.
from panel_visuals import THEMES, ICONS, appearance_ids, icon_pose, acrylic_color


def luminance(color):
    rgb = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
    return sum(weight * (v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4)
               for weight, v in zip((.2126, .7152, .0722), rgb))


class AppearanceTests(unittest.TestCase):
    def test_all_icons_accept_every_theme_without_pairing(self):
        self.assertEqual(len(ICONS), 5)
        self.assertEqual(len(THEMES), 6)  # Existing blue-gray plus five new themes.
        for theme in THEMES:
            for icon in ICONS:
                self.assertEqual(appearance_ids(theme, icon), (theme, icon))

    def test_missing_and_invalid_preferences_fall_back_independently(self):
        for invalid in (None, "", "unknown", 123, [], {}):
            self.assertEqual(appearance_ids(invalid, "gear"), ("classic", "gear"))
            self.assertEqual(appearance_ids("frost", invalid), ("frost", "robot"))
        self.assertEqual(appearance_ids(None, None), ("classic", "robot"))

    def test_motion_requires_task_activity_and_respects_reduced_motion(self):
        for icon in ICONS:
            self.assertNotEqual(icon_pose(icon, "生成中", 0), icon_pose(icon, "生成中", .55), icon)
            for state in ("等待会话", "读取中", "等待统计", "读取失败"):
                self.assertEqual(icon_pose(icon, state, 0), icon_pose(icon, state, 5.1), (icon, state))
            for state in ("生成中", "等待新的调用"):
                self.assertEqual(icon_pose(icon, state, 0, False), icon_pose(icon, state, 5.1, False))
            if icon != "robot":
                self.assertEqual(icon_pose(icon, "等待新的调用", 0), icon_pose(icon, "等待新的调用", 5.1))

    def test_acrylic_channel_order_and_foreground_opacity_contract(self):
        self.assertEqual(acrylic_color(THEMES["classic"].background), 0xD92E2217)
        self.assertEqual(acrylic_color(THEMES["frost"].background), 0xD9FAF6F3)
        for theme in THEMES.values():
            self.assertEqual(acrylic_color(theme.background) >> 24, 0xD9)

    def test_text_colors_are_readable_on_all_theme_surfaces(self):
        for key, theme in THEMES.items():
            for bg in (theme.background, theme.surface, theme.hover, theme.selected):
                for fg in (theme.text, theme.muted, theme.accent, theme.success, theme.warning):
                    bright, dark = sorted((luminance(fg), luminance(bg)), reverse=True)
                    self.assertGreaterEqual((bright+.05)/(dark+.05), 4.5, (key, fg, bg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
