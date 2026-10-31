"""Raster quality, state cues and bounded animation frames, without a display."""
from dataclasses import replace
import unittest

from PIL import ImageChops, ImageColor

from panel_icons import frame_for, render_icon
from panel_visuals import ICONS, THEMES


class IconRasterTests(unittest.TestCase):
    def test_all_palettes_and_sizes_have_smooth_transparent_edges(self):
        for theme in THEMES.values():
            for icon in ICONS:
                for scale in (.8, 1, 1.25, 1.5, 2):
                    with self.subTest(theme=theme.name, icon=icon, scale=scale):
                        frame = frame_for(icon, scale, "等待新的调用", 0, theme)
                        image = render_icon(frame)
                        alpha = image.getchannel("A")
                        histogram = alpha.histogram()
                        self.assertEqual(image.size, frame.size)
                        self.assertGreater(sum(histogram[1:255]), 50)
                        self.assertGreater(histogram[255], 15)
                        # Padding remains clear, so no opaque tile is added over acrylic.
                        self.assertEqual(alpha.getpixel((0, 0)), 0)
                        self.assertEqual(alpha.getpixel((image.width-1, image.height-1)), 0)
                        self.assertGreaterEqual(alpha.getbbox()[0], 1)
                        ink = ImageColor.getrgb(frame.ink)
                        # Lanczos mixes narrow strokes with their fill; near-opaque ink must
                        # still retain the selected hue rather than flattening to gray.
                        self.assertTrue(any(pixel[3] >= 250 and max(abs(pixel[i]-ink[i]) for i in range(3)) <= 12
                                            for pixel in image.get_flattened_data()))

    def test_each_animation_changes_pixels_and_reduced_motion_freezes_them(self):
        theme = THEMES["classic"]
        for icon in ICONS:
            first = render_icon(frame_for(icon, .8, "生成中", 0, theme))
            later = render_icon(frame_for(icon, .8, "生成中", .55, theme))
            self.assertIsNotNone(ImageChops.difference(first, later).getbbox(alpha_only=False), icon)
            for state in ("等待会话", "读取中", "等待统计", "读取失败", "生成中", "等待新的调用"):
                frames = [frame_for(icon, .8, state, time, theme, False) for time in (0, .55, 5.1, 7200)]
                self.assertTrue(all(frame == frames[0] for frame in frames), (icon, state))

    def test_failure_has_visible_badge_even_with_identical_ink(self):
        for icon in ICONS:
            frame = frame_for(icon, .8, "读取失败", 0, THEMES["sand"])
            badge = render_icon(frame)
            plain = render_icon(replace(frame, error=False))
            changed = ImageChops.difference(badge, plain).getbbox(alpha_only=False)
            self.assertIsNotNone(changed)
            self.assertGreater(changed[0], frame.size[0] / 2)
            self.assertLess(changed[3], frame.size[1] / 2)

    def test_rotation_period_reuses_frames_without_accumulating_blur(self):
        for icon, period, limit in (("gear", 2, 32), ("orbit", 6, 96)):
            frames = {frame_for(icon, .8, "生成中", i*.02, THEMES["nebula"]) for i in range(1200)}
            self.assertLessEqual(len(frames), limit)
            first = frame_for(icon, .8, "生成中", .38, THEMES["nebula"])
            repeated = frame_for(icon, .8, "生成中", .38 + period*100, THEMES["nebula"])
            self.assertEqual(first, repeated)
            self.assertEqual(render_icon(first).tobytes(), render_icon(repeated).tobytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
