"""Small, antialiased status icons drawn from float geometry, independent of theme."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math

from PIL import Image, ImageColor, ImageDraw, ImageTk

from panel_visuals import icon_pose


SUPERSAMPLE = 6


@dataclass(frozen=True)
class IconFrame:
    icon: str
    size: tuple[int, int]
    pose: tuple[float, ...]
    ink: str
    surface: str
    muted: str
    error: bool


def frame_for(icon, scale, state, elapsed, theme, animate=True):
    """Quantize motion, not geometry, so frames can be reused indefinitely."""
    pose = icon_pose(icon, state, elapsed, animate)
    if icon == "robot":
        pose = (round(pose[0] * 4) / 4, round(pose[1] * 2) / 2)
    elif icon == "terminal":
        pose = (round(pose[0] * 15) / 15,)
    elif icon == "pulse":
        pose = tuple(round(value * 2) / 2 for value in pose)
    elif icon in ("gear", "orbit"):
        # The six-tooth gear repeats every 60 degrees; never rotate a small bitmap.
        period, steps = (math.tau / 6, 32) if icon == "gear" else (math.tau, 96)
        pose = ((round(pose[0] / period * steps) % steps) * period / steps,)
    ink = (theme.warning if state == "读取失败" else
           theme.muted if state in ("等待会话", "等待统计", "读取中") else theme.accent)
    return IconFrame(icon, (max(1, round(48 * scale)), max(1, round(44 * scale))),
                     pose, ink, theme.surface, theme.muted, state == "读取失败")


def rounded_path(points, radius):
    """Round polygon corners with short quadratic curves, retaining flat tooth tips."""
    result = []
    for index, point in enumerate(points):
        previous, following = points[index - 1], points[(index + 1) % len(points)]
        a, b = math.dist(point, previous), math.dist(point, following)
        distance = min(radius, a * .35, b * .35)
        start = tuple(point[i] + (previous[i] - point[i]) * distance / a for i in (0, 1))
        end = tuple(point[i] + (following[i] - point[i]) * distance / b for i in (0, 1))
        for step in range(7):
            t = step / 6
            result.append(tuple((1-t)**2 * start[i] + 2*(1-t)*t * point[i] + t*t * end[i]
                                for i in (0, 1)))
    return result


def render_icon(frame):
    """Return RGBA with transparent padding; Tk composites it over the actual material."""
    width, height = frame.size
    image = Image.new("RGBA", (width * SUPERSAMPLE, height * SUPERSAMPLE))
    draw = ImageDraw.Draw(image)
    scale = min(width / 48, height / 44)
    unit = scale * SUPERSAMPLE
    origin = (22 * width / 48 * SUPERSAMPLE, height / 2 * SUPERSAMPLE)
    ink = ImageColor.getrgb(frame.ink) + (255,)
    surface = ImageColor.getrgb(frame.surface) + (255,)
    stroke = max(1.85, 1.45 / scale)

    def xy(point):
        return (origin[0] + point[0] * unit, origin[1] + point[1] * unit)

    def box(bounds):
        return xy(bounds[:2]) + xy(bounds[2:])

    def ellipse(bounds, fill, outline=None, line_width=stroke):
        draw.ellipse(box(bounds), fill=fill, outline=outline, width=round(line_width * unit))

    def line(points, color=ink, line_width=stroke, caps=True):
        coords = [xy(point) for point in points]
        draw.line(coords, fill=color, width=round(line_width * unit), joint="curve")
        if caps:
            radius = line_width / 2
            for x, y in points:
                ellipse((x-radius, y-radius, x+radius, y+radius), color)

    def rounded(bounds, radius, fill=surface, outline=ink, line_width=stroke):
        draw.rounded_rectangle(box(bounds), radius=radius * unit, fill=fill,
                               outline=outline, width=round(line_width * unit))

    if frame.icon == "robot":
        offset, eye = frame.pose
        # A single clean head, short ear tabs and two equally weighted eyes.
        line([(0, -10+offset), (0, -15+offset)], line_width=1.65)
        ellipse((-1.8, -17+offset, 1.8, -13.4+offset), ink)
        for x in (-15, 15):
            line([(x, -1+offset), (x, 4+offset)], line_width=2.2)
        rounded((-13, -10+offset, 13, 12+offset), 5.5)
        eye_height = min(2.5, eye)
        for x in (-5.5, 5.5):
            rounded((x-1.35, 1-eye_height+offset, x+1.35, 1+eye_height+offset),
                    1.25, ink, None)
        line([(-3, 7.3+offset), (3, 7.3+offset)], line_width=1.6)
    elif frame.icon == "terminal":
        rounded((-16, -12, 16, 12), 5)
        line([(-9, -5), (-4, 0), (-9, 5)], line_width=2.15)
        muted = ImageColor.getrgb(frame.muted)
        cursor = tuple(round(a * (1-frame.pose[0]) + b * frame.pose[0]) for a, b in zip(muted, ink)) + (255,)
        line([(2, 5.5), (9.5, 5.5)], color=cursor, line_width=2.15)
    elif frame.icon == "pulse":
        for index, half_height in enumerate(frame.pose):
            x = (index - 2) * 6.5
            line([(x, -half_height), (x, half_height)], line_width=3.1)
    elif frame.icon == "orbit":
        def orbit(angle):
            x, y = 16.5 * math.cos(angle), 10.5 * math.sin(angle)
            tilt = -.35
            return (x*math.cos(tilt)-y*math.sin(tilt), x*math.sin(tilt)+y*math.cos(tilt))
        line([orbit(step * math.tau / 128) for step in range(129)], line_width=stroke, caps=False)
        star = [(0, -7.3), (2.3, -2.3), (7.3, 0), (2.3, 2.3),
                (0, 7.3), (-2.3, 2.3), (-7.3, 0), (-2.3, -2.3)]
        draw.polygon([xy(point) for point in rounded_path(star, .55)], fill=ink)
        x, y = orbit(frame.pose[0])
        # A clear halo separates the satellite from the ring in small sizes.
        ellipse((x-3.9, y-3.9, x+3.9, y+3.9), (0, 0, 0, 0))
        ellipse((x-2.65, y-2.65, x+2.65, y+2.65), ink)
    elif frame.icon == "gear":
        points = []
        for tooth in range(6):
            for degrees, radius in ((-30, 11.8), (-18, 11.8), (-11, 15.6), (11, 15.6), (18, 11.8)):
                angle = math.radians(tooth*60 + degrees) + frame.pose[0]
                points.append((radius*math.cos(angle), radius*math.sin(angle)))
        outline = rounded_path(points, 1.15)
        draw.polygon([xy(point) for point in outline], fill=surface)
        line(outline + outline[:1], caps=False)
        ellipse((-5.4, -5.4, 5.4, 5.4), (0, 0, 0, 0), ink)
    else:
        raise ValueError(f"Unknown status icon: {frame.icon}")

    if frame.error:
        ellipse((13, -19, 25, -7), surface, ink, 1.5)
        line([(19, -16), (19, -12.8)], line_width=1.65)
        ellipse((18.1, -10.9, 19.9, -9.1), ink)
    return image.resize(frame.size, Image.Resampling.LANCZOS)


class IconImages:
    """Bounded, per-window Tk images; invalidate old sizes/palettes during resizing."""
    LIMIT = 96

    def __init__(self, master):
        self.master = master
        self.images = OrderedDict()
        self.style = None

    def get(self, frame):
        style = (frame.icon, frame.size, frame.ink, frame.surface, frame.muted, frame.error)
        if style != self.style:
            self.clear()
            self.style = style
        if frame not in self.images:
            self.images[frame] = ImageTk.PhotoImage(render_icon(frame), master=self.master)
            if len(self.images) > self.LIMIT:
                self.images.popitem(last=False)
        self.images.move_to_end(frame)
        return self.images[frame]

    def clear(self):
        self.images.clear()
        self.style = None
