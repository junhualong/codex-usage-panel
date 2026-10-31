"""A read-only, local Codex token-usage panel."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox
import traceback

from active_session import DesktopSessionFollower, Selection
from codex_autostart import PANEL_MUTEX_NAME, PanelCloseSignal
from generation_speed import SpeedSample, SpeedTracker, speed_label
from app_runtime import app_command, load_config
from panel_visuals import (WindowMaterial, visual_preferences, THEMES, ICONS,
                           DEFAULT_THEME, appearance_ids)
from panel_icons import IconImages, frame_for
from panel_menu import ThemedMenu
from session_cache import SessionReadCache


APP_TITLE = "Codex 实时用量"
BACKGROUND = "#17222e"
SURFACE = "#202f3e"
BORDER = "#405363"
TEXT = "#eef4fa"
MUTED = "#c1ceda"
HOVER = "#334c60"
SELECTED = "#29475e"
ACCENT = "#9ed5ff"
GREEN = "#8ed6b2"
AMBER = "#e9bf80"
TRANSPARENT = "#010203"
MAX_LINE = 2 * 1024 * 1024


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict):
            return cls()
        values = {name: count(data.get(name)) for name in cls.__dataclass_fields__}
        # Cached input and reasoning output are subsets, not additional tokens.
        if values["input_tokens"] is not None and values["output_tokens"] is not None:
            values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
        return cls(**values)

    @property
    def cache_rate(self):
        if not self.input_tokens or self.cached_input_tokens is None:
            return None
        if self.cached_input_tokens > self.input_tokens:
            return None
        return 100.0 * self.cached_input_tokens / self.input_tokens

    @property
    def uncached_input(self):
        if self.input_tokens is None or self.cached_input_tokens is None:
            return None
        if self.cached_input_tokens > self.input_tokens:
            return None
        return self.input_tokens - self.cached_input_tokens


@dataclass
class Snapshot:
    total: Usage = field(default_factory=Usage)
    last: Usage = field(default_factory=Usage)
    speed: SpeedSample = field(default_factory=SpeedSample)
    session_id: str = ""
    timestamp: str = ""
    active: bool = False
    has_usage: bool = False
    loading: bool = True
    error: str = ""
    message: str = ""
    skipped: int = 0


class LogTailer:
    """Read only appended bytes; commit only complete JSONL records."""

    def __init__(self, path):
        self.path = Path(path)
        self.reset()

    def reset(self):
        self.offset = 0
        self.pending = b""
        self.discarding = False
        self.identity = None
        self.snapshot = Snapshot()
        self.speed_tracker = SpeedTracker()
        self.prefix = self.boundary = b""
        self.file_stamp = None

    def can_resume(self):
        """Reject replaced/truncated/rewritten logs before exposing a cached snapshot."""
        if self.identity is None or self.file_stamp is None:
            return False
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                if (stat.st_dev, stat.st_ino) != self.identity or stat.st_size < self.offset:
                    return False
                if stat.st_size == self.file_stamp[0] and stat.st_mtime_ns != self.file_stamp[1]:
                    return False
                if stream.read(len(self.prefix)) != self.prefix:
                    return False
                stream.seek(self.offset - len(self.boundary))
                return stream.read(len(self.boundary)) == self.boundary
        except OSError:
            return False

    def accept(self, record):
        self.speed_tracker.accept(record)
        self.snapshot.speed = self.speed_tracker.sample
        if not isinstance(record, dict):
            return
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if record.get("type") == "session_meta":
            self.snapshot.session_id = str(payload.get("id", ""))
            return
        if record.get("type") != "event_msg":
            return
        event = payload.get("type")
        if event == "task_started":
            self.snapshot.active = True
        elif event in ("task_complete", "turn_aborted", "task_failed"):
            self.snapshot.active = False
        elif event == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            # Codex emits cumulative snapshots, sometimes more than once.
            # Replace, never sum these values; missing usage is not zero usage.
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if isinstance(total, dict):
                self.snapshot.total = Usage.parse(total)
            if isinstance(last, dict):
                self.snapshot.last = Usage.parse(last)
            if isinstance(total, dict) or isinstance(last, dict):
                self.snapshot.has_usage = True
                self.snapshot.timestamp = str(record.get("timestamp", ""))

    def consume(self, data):
        if self.discarding:
            newline = data.find(b"\n")
            if newline < 0:
                return
            data = data[newline + 1:]
            self.discarding = False
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            if not line.strip():
                continue
            if len(line) > MAX_LINE:
                self.snapshot.skipped += 1
                continue
            try:
                self.accept(json.loads(line))
            except (ValueError, UnicodeError):
                self.snapshot.skipped += 1
        if len(self.pending) > MAX_LINE:
            self.pending = b""
            self.discarding = True
            self.snapshot.skipped += 1

    def poll(self, byte_budget=4 * 1024 * 1024):
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
                    self.reset()
                self.identity = identity
                if self.offset == 0:
                    header = stream.read(32)
                    if header.startswith(b"%TSD-Header-"):
                        self.snapshot.error = "日志受 TSD 保护，无法通过此面板读取"
                        self.snapshot.loading = False
                        return self.snapshot
                stream.seek(self.offset)
                data = stream.read(byte_budget)
                if self.offset < 512:
                    self.prefix = (self.prefix + data)[:512]
                self.boundary = (self.boundary + data)[-512:]
                self.offset = stream.tell()
                self.consume(data)
                after = os.fstat(stream.fileno())
                self.snapshot.loading = self.offset < after.st_size
                self.file_stamp = (after.st_size, after.st_mtime_ns)
                self.snapshot.error = ""
        except FileNotFoundError:
            self.snapshot.error = "日志暂不可用，请检查会话或文件位置"
            self.snapshot.loading = False
        except OSError as exc:
            self.snapshot.error = f"日志读取失败：{exc.strerror or type(exc).__name__}"
            self.snapshot.loading = False
        return self.snapshot


def read_snapshot(path):
    tailer = LogTailer(path)
    while True:
        snapshot = tailer.poll()
        if not snapshot.loading:
            return snapshot


def compact(number):
    if number is None:
        return "—"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def exact(number):
    return "—" if number is None else f"{number:,}"


def timestamp_label(value):
    if not value:
        return "—（尚无用量记录）"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return "—（记录时间格式未知）"


def data_age(value, now=None):
    if not value:
        return "—"
    try:
        recorded = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        seconds = int(((now or datetime.now().astimezone()) - recorded).total_seconds())
    except (ValueError, OverflowError, OSError):
        return "—"
    if seconds < 0:
        return recorded.strftime("%m-%d %H:%M:%S")
    if seconds < 60:
        return "刚刚" if seconds < 5 else f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def display_state(snapshot, following=False, selection=None):
    """Use reader/navigation results and explicit turn events, never data age."""
    if snapshot.error:
        return "读取失败", snapshot.error, AMBER
    if snapshot.loading:
        return "读取中", snapshot.message or "正在读取本地历史记录，已读取的统计可能尚不完整", MUTED
    if following and selection is not None and not selection.path:
        label = "等待统计" if selection.session_id else "等待会话"
        return label, selection.message, MUTED
    if snapshot.active:
        return "生成中", "本轮正在进行；用量和均速在调用结算后更新", GREEN
    if not snapshot.has_usage or not any(value is not None for usage in (snapshot.total, snapshot.last)
                                         for value in vars(usage).values()):
        return "等待统计", "日志尚无有效用量统计，等待模型调用结算", MUTED
    return "等待新的调用", "保留上次有效用量，等待新的调用", MUTED


def read_titles(root):
    titles = {}
    try:
        with (root.parent / "session_index.jsonl").open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 1024 * 1024))
            if stream.tell():
                stream.readline()
            for line in stream:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("id"):
                        titles[str(item["id"])] = str(item.get("thread_name", ""))
                except (ValueError, UnicodeError):
                    continue
    except OSError:
        pass
    return titles


def recent_sessions(root, selected):
    """Bounded discovery; do not follow junctions or directory symlinks."""
    def directories(parent):
        try:
            return [p for p in parent.iterdir() if p.name.isdigit() and p.is_dir()
                    and not p.is_symlink() and not p.is_junction()]
        except OSError:
            return []

    days = []
    for year in directories(root):
        for month in directories(year):
            days.extend(directories(month))
    paths = []
    for day in sorted(days, reverse=True)[:30]:
        paths.extend(p for p in day.glob("rollout-*.jsonl") if not p.is_symlink())
    paths = sorted(set(paths), key=lambda p: p.name, reverse=True)[:100]
    if selected not in paths:
        paths.insert(0, selected)
    titles = read_titles(root)
    rows = []
    for path in paths:
        try:
            with path.open("rb") as stream:
                line = stream.readline(128 * 1024)
            record = json.loads(line)
            meta = record.get("payload", {})
            if record.get("type") != "session_meta" or not isinstance(meta, dict):
                continue
            source = meta.get("source")
            if isinstance(source, dict) and "subagent" in source:
                continue
            session_id = str(meta.get("id", ""))
            rows.append((path, titles.get(session_id) or Path(str(meta.get("cwd", ""))).name or "未命名会话",
                         timestamp_label(str(meta.get("timestamp", ""))), session_id))
        except (OSError, ValueError, UnicodeError, AttributeError):
            continue
    return rows


def enable_dpi():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


def work_area(root, bounds=None):
    """Use the nearest monitor's usable desktop, including negative coordinates."""
    if os.name == "nt":
        if bounds is None:
            x, y = root.winfo_x(), root.winfo_y()
            bounds = (x, y, x + root.winfo_width(), y + root.winfo_height())
        user = ctypes.windll.user32
        user.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
        user.MonitorFromRect.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        rect = wintypes.RECT(*bounds)
        monitor = user.MonitorFromRect(ctypes.byref(rect), 2)  # MONITOR_DEFAULTTONEAREST
        info = MonitorInfo(size=ctypes.sizeof(MonitorInfo))
        if monitor and user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return info.work.left, info.work.top, info.work.right, info.work.bottom
        rect = wintypes.RECT()
        if user.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def clamp_position(x, y, width, height, area):
    left, top, right, bottom = area
    return (max(left, min(int(x), right - width)), max(top, min(int(y), bottom - height)))


def singleton():
    if os.name != "nt":
        return None, False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, PANEL_MUTEX_NAME)
    exists = ctypes.get_last_error() == 183
    if exists:
        user = ctypes.windll.user32
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        window = user.FindWindowW(None, APP_TITLE)
        if window:
            user.ShowWindow(window, 5)
            user.SetForegroundWindow(window)
    return handle, exists


class UsagePanel:
    theme = THEMES[DEFAULT_THEME]
    WIDTH = 912
    BAR_HEIGHT = 56
    EXPANDED_HEIGHT = 372
    MIN_ZOOM = 0.8
    MAX_ZOOM = 2.0
    RESIZE_CURSORS = {"n": "size_ns", "s": "size_ns", "e": "size_we", "w": "size_we",
                      "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}

    def __init__(self, root, config, state_dir, session_override=None, close_signal=None, commands=None):
        self.root = root
        self.close_signal = close_signal
        self.commands = commands
        self.hidden = False
        self.settings_error = ""
        self.settings_retry_at = 0
        self.save_notice = self.save_notice_job = None
        self.active_popup = None
        self.last_action = ""
        self.reported_at = ""
        self.heartbeat_at = 0
        self.reader_health = {}
        self.health_lock = threading.Lock()
        self.read_cache = SessionReadCache()
        self.follow_generation = 0
        self.close_reason = ""
        self.config = config
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.saved = self.load_settings()
        self.follow_enabled = session_override is None and config.get("auto_follow", True)
        default_path = session_override or self.saved.get("session") or config["default_session"]
        self.path = Path(default_path)
        self.sessions_root = Path(config["sessions_root"])
        self.titles = read_titles(self.sessions_root)
        self.mode = self.saved.get("mode", "total")
        if self.mode not in ("total", "last"):
            self.mode = "total"
        self.theme_id, self.icon = appearance_ids(self.saved.get("theme"), self.saved.get("icon"))
        self.theme = THEMES[self.theme_id]
        self.topmost = bool(self.saved.get("topmost", True))
        self.expanded = False
        self.snapshot = Snapshot()
        self.stop = threading.Event()
        self.queue = queue.Queue(maxsize=2)
        self.follow_queue = queue.Queue(maxsize=2)
        self.follow_stop = threading.Event()
        self.follow_selection = Selection()
        self.worker = self.follow_worker = None
        self.reader_errors = {}
        self.reader_log_lock = threading.Lock()
        self.last_status = None
        self.generation = 0
        self.closed = False
        self.tooltip = None
        self.tooltip_job = None
        self.hovered_tag = None
        self.hints = {}
        self.button_fills = {}
        self.copy_until = 0
        self.scope_menu = None
        self.appearance_menus = {}
        self.context_popup = None
        self.menu_open = False
        self.pressed_action = None
        self.scope_variable = tk.StringVar(root, value=self.mode)
        self.theme_variable = tk.StringVar(root, value=self.theme_id)
        self.icon_variable = tk.StringVar(root, value=self.icon)
        self.fonts = {}
        self.poll_job = None
        self.animation_job = None
        self.animation_start = time.monotonic()
        self.animation_enabled = True
        self.icon_images = IconImages(root)
        self.icon_image = None
        self.icon_frame = None
        self.material = None
        self.preferences = None
        self.preference_check = 0
        self.background = self.theme.background
        self.drag_origin = None
        self.resize_origin = None
        try:
            zoom = float(self.saved.get("zoom", 1.0))
        except (TypeError, ValueError):
            zoom = 1.0
        self.zoom = max(self.MIN_ZOOM, min(zoom, self.MAX_ZOOM)) if math.isfinite(zoom) else 1.0
        root.withdraw()
        root.title(APP_TITLE)
        root.configure(bg=TRANSPARENT)
        root.overrideredirect(True)
        root.attributes("-topmost", self.topmost)
        if os.name == "nt":
            root.attributes("-transparentcolor", TRANSPARENT)
        self.dpi_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.scale = self.dpi_scale * self.zoom
        self.canvas = tk.Canvas(root, bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        left, top, right, bottom = work_area(root)
        width, height = self.px(self.WIDTH), self.px(self.BAR_HEIGHT)
        x = int(self.saved.get("x", right - width - self.px(24)))
        y = int(self.saved.get("y", bottom - height - self.px(24)))
        self.compact_position = (x, y)
        self.last_work_area = None
        self.place_window(x, y)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _e: self.set_expanded(False))
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self.drag_end)
        self.canvas.bind("<Motion>", self.pointer_motion)
        self.canvas.bind("<Button-3>", self.context_menu)
        self.render()
        root.deiconify()
        root.update_idletasks()
        self.material = WindowMaterial(root)
        self.refresh_visual_settings()
        if self.follow_enabled:
            self.snapshot = Snapshot(loading=False, message=self.follow_selection.message)
            self.start_follow_reader()
        else:
            self.start_reader()
        self.render()
        self.poll_job = root.after(100, self.tick)
        self.animation_job = root.after(100, self.animate_icon)

    def px(self, value):
        return int(round(value * self.scale))

    def limited_scale(self, scale, area):
        left, top, right, bottom = area
        # Both views must fit at the same zoom, so expanding never clips controls.
        maximum = min(self.dpi_scale * self.MAX_ZOOM,
                      (right - left) / self.WIDTH, (bottom - top) / self.EXPANDED_HEIGHT)
        minimum = min(self.dpi_scale * self.MIN_ZOOM, maximum)
        return max(minimum, min(scale, maximum))

    def place_window(self, x, y, scale=None, area=None):
        height = self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT
        if area is None:
            area = work_area(self.root, (int(x), int(y), int(x) + self.px(self.WIDTH), int(y) + self.px(height)))
        if scale is None:
            scale = self.dpi_scale * self.zoom if area != self.last_work_area else self.scale
        scale = self.limited_scale(scale, area)
        scaled = not math.isclose(scale, self.scale)
        if scaled:
            self.scale = scale
            self.fonts.clear()
        width, height = self.px(self.WIDTH), self.px(height)
        x, y = clamp_position(x, y, width, height, area)
        target = (x, y, width, height)
        current = (self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())
        if target != current:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        if self.material:
            # Apply Tk's queued move before SetWindowRgn sends WM_WINDOWPOSCHANGED;
            # otherwise Windows can restore the pre-expansion bottom-edge position.
            self.root.update_idletasks()
            self.material.resize(width, height, max(4, self.px(10)))
        self.last_work_area = area
        anchor = self.compact_position if self.expanded else (x, y)
        self.compact_position = clamp_position(*anchor, width, self.px(self.BAR_HEIGHT), area)
        return target != current or scaled

    def ensure_visible(self):
        if self.drag_origin or self.resize_origin or not self.root.winfo_ismapped():
            return
        if self.place_window(self.root.winfo_x(), self.root.winfo_y()):
            self.render()
            self.save_settings()

    def resize_edge(self, x, y):
        width, height = self.px(self.WIDTH), self.px(self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT)
        margin, corner = max(4, self.px(5)), max(8, self.px(10))
        if not (0 <= x <= width and 0 <= y <= height):
            return ""
        horizontal = "w" if x <= corner else "e" if x >= width - corner else ""
        vertical = "n" if y <= corner else "s" if y >= height - corner else ""
        if horizontal and vertical:
            return vertical + horizontal
        if x <= margin:
            return "w"
        if x >= width - margin:
            return "e"
        if y <= margin:
            return "n"
        if y >= height - margin:
            return "s"
        return ""

    def pointer_motion(self, event):
        edge = self.resize_origin[0] if self.resize_origin else self.resize_edge(event.x, event.y)
        if edge:
            self.hide_tooltip()
            self.canvas.configure(cursor=self.RESIZE_CURSORS[edge])
        elif self.drag_origin:
            self.canvas.configure(cursor="fleur")
        else:
            self.canvas.configure(cursor="hand2" if "button" in self.canvas.gettags("current") else "")

    def reset_size(self):
        self.zoom = 1.0
        self.place_window(self.root.winfo_x(), self.root.winfo_y(), self.dpi_scale)
        self.render()
        self.save_settings()

    def font(self, size=12, bold=False, mono=False):
        key = (size, bold, mono)
        if key not in self.fonts:
            # Keep type legible at minimum zoom; only an undersized work area can
            # lower the DPI floor so the complete panel still remains reachable.
            floor_dpi = min(self.dpi_scale, self.scale / self.MIN_ZOOM)
            pixels = max(self.px(size), round((16 if size >= 15 else 12) * floor_dpi))
            self.fonts[key] = tkfont.Font(family="Consolas" if mono else "Microsoft YaHei UI",
                                          size=-pixels, weight="bold" if bold else "normal")
        return self.fonts[key]

    def text(self, x, y, value, color=None, size=12, anchor="w", bold=False, mono=False, tags=()):
        return self.canvas.create_text(self.px(x), self.px(y), text=value, fill=color or self.theme.text,
                                       font=self.font(size, bold, mono), anchor=anchor, tags=tags)

    def line(self, x1, y1, x2, y2, color=None, width=1, tags=()):
        return self.canvas.create_line(self.px(x1), self.px(y1), self.px(x2), self.px(y2),
                                       fill=color or self.theme.border, width=self.px(width), tags=tags)

    def rounded(self, x1, y1, x2, y2, radius, fill, outline="", tags=()):
        coords = [x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
                  x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
                  x1,y2, x1,y2-radius, x1,y1+radius, x1,y1]
        return self.canvas.create_polygon([self.px(v) for v in coords], smooth=True,
                                          splinesteps=24, fill=fill, outline=outline, tags=tags)

    def ellipsis(self, value, width, size=12, bold=False):
        font = self.font(size, bold)
        if font.measure(value) <= self.px(width):
            return value
        while value and font.measure(value + "…") > self.px(width):
            value = value[:-1]
        return value + "…"

    @property
    def scope_label(self):
        return "会话累计" if self.mode == "total" else "最近调用"

    @property
    def current_usage(self):
        return self.snapshot.total if self.mode == "total" else self.snapshot.last

    def state(self):
        state, reason, color = display_state(self.snapshot, self.follow_enabled, self.follow_selection)
        return state, reason, {AMBER: self.theme.warning, MUTED: self.theme.muted,
                              GREEN: self.theme.success}[color]

    def session_name(self):
        return self.titles.get(self.snapshot.session_id) or (
            "当前 Codex 会话（索引暂无名称）" if self.snapshot.session_id else "尚未识别聊天名称")

    def follow_hint(self):
        method = ("跟随方式：读取 Codex 主窗口的本地导航记录，随切换聊天更新"
                  if self.follow_enabled else f"固定会话：读取指定日志\n{self.path}")
        state, reason, _ = self.state()
        return f"{method}\n当前聊天：{self.session_name()}\n会话 ID：{self.snapshot.session_id or '—'}\n{state}：{reason}"

    def usage_hint(self):
        usage = self.current_usage
        state, reason, _ = self.state()
        return (f"统计范围：{self.scope_label}（同时影响用量和缓存命中率）\n"
                f"输入 {exact(usage.input_tokens)} + 输出 {exact(usage.output_tokens)} = {exact(usage.total_tokens)} tokens\n"
                "K = 1,000，M = 1,000,000，B = 1,000,000,000\n"
                "横条极大数值使用科学计数法（e+ 表示 10 的幂），精确数值见明细或复制统计\n"
                "会话累计用量不代表当前上下文占用；缓存已包含在输入中\n"
                f"{state}：{reason}\n— 表示该范围的统计字段缺失，不能视为 0")

    def cache_hint(self):
        usage = self.current_usage
        reason = ("— 表示输入为 0、计数缺失或缓存计数超出输入，无法计算比例"
                  if usage.cache_rate is None else f"缓存命中率：{usage.cache_rate:.2f}%")
        return (f"统计范围：{self.scope_label}\n已缓存 {exact(usage.cached_input_tokens)} ÷ 输入 {exact(usage.input_tokens)} tokens\n"
                f"{reason}\n缓存命中率不是费用节省比例\n{self.state()[1]}")

    def speed_hint(self):
        sample = self.snapshot.speed
        detail = ("—：等待本轮调用结算及完整生成时间；旧日志可能缺少计时记录"
                  if sample.tokens_per_second is None else
                  f"输出 {exact(sample.output_tokens)} tokens ÷ {sample.duration_ms / 1000:.3f} 秒")
        return ("速度始终表示本轮最近一次已结算调用的平均生成速度，不随统计范围切换\n" + detail +
                "\n含推理和工具参数输出；不计工具执行等待\n≈ 为本地记录估算，非逐 token 瞬时速度")

    def method_hint(self):
        interval = max(0.25, float(self.config.get("refresh_ms", 1000)) / 1000)
        return ("用量和缓存命中率使用所选统计范围；最近调用指一次模型调用\n"
                "总用量 = 输入 + 输出；已缓存、未缓存组成输入，其中推理属于输出\n"
                "缓存命中率 = 已缓存 ÷ 输入；会话累计用量不代表当前上下文占用\n" + self.speed_hint() +
                f"\n每 {interval:g} 秒检查本地用量日志，通常在调用结算后更新；新一轮开始清空上一轮速度\n"
                "数据时间对应最近一次用量统计记录时间，不是面板刷新时间；空闲时保留上次数据")

    def call_summary(self):
        sample = self.snapshot.speed
        duration = "—" if sample.tokens_per_second is None else f"{sample.duration_ms / 1000:.2f} 秒"
        return f"最近调用：{speed_label(sample)} tok/s · 输出 {exact(sample.output_tokens)} · 耗时 {duration}"

    def hint(self, tag, help_text, clickable=False):
        self.hints[tag] = help_text
        self.canvas.tag_bind(tag, "<Enter>", lambda e: self.hover(e, tag, clickable))
        self.canvas.tag_bind(tag, "<Leave>", self.hide_tooltip)

    def action(self, tag, callback, help_text):
        # A press-time redraw removes Canvas's current item before drag_start runs.
        # Activate on release so a bottom-edge expansion cannot become a drag anchor.
        self.canvas.tag_bind(tag, "<ButtonPress-1>", lambda e: self.press_action(tag, e))
        self.canvas.tag_bind(tag, "<ButtonRelease-1>", lambda _e: self.release_action(tag, callback))
        self.hint(tag, help_text, True)

    def press_action(self, tag, event):
        self.pressed_action = None if self.resize_edge(event.x, event.y) else tag

    def release_action(self, tag, callback):
        pressed, self.pressed_action = self.pressed_action, None
        if pressed == tag and not self.drag_origin and not self.resize_origin:
            callback()

    def hitbox(self, x1, x2, tag, y1=8, y2=48, selected=False):
        fill = self.theme.selected if selected else self.background
        self.button_fills[tag] = fill
        self.rounded(x1, y1, x2, y2, 6, fill, tags=("button", tag, tag + "_bg"))

    def number(self, right, y, value, width, color=None, size=13, bold=False, tags=()):
        # Keep exact, right-aligned digits even for large cumulative counts and DPI scaling.
        minimum = 15 if size >= 15 else 12
        while size > minimum and self.font(size, bold, True).measure(value) > self.px(width):
            size -= 1
        return self.text(right, y, value, color, size, anchor="e", bold=bold, mono=True, tags=tags)

    def metric_label(self, label, raw, width, prefix=""):
        if raw is not None and self.font(15, True, True).measure(label) > self.px(width):
            return prefix + f"{raw:.0e}"
        return label

    def render(self):
        self.hide_tooltip()
        self.hints.clear()
        self.button_fills.clear()
        c = self.canvas
        c.delete("all")
        height = self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT
        self.rounded(1, 1, self.WIDTH-1, height-1, 12, self.background, self.theme.border)
        self.line(14, 2, self.WIDTH-14, 2, self.theme.border)
        usage, rate = self.current_usage, self.current_usage.cache_rate
        state, reason, state_color = self.state()
        self.draw_icon()
        self.hint("state", f"{state}：{reason}\n图标：{ICONS[self.icon]} · 主题：{self.theme.name}"
                  "\n动效只表示任务状态，不表示实时生成速度")
        self.line(62, 17, 62, 39)
        self.draw_speed()
        self.hitbox(264, 375, "scope")
        self.text(276, 28, self.scope_label, self.theme.text, 12, tags=("button", "scope"))
        self.line(357, 26, 361, 30, self.theme.muted, tags=("button", "scope"))
        self.line(361, 30, 365, 26, self.theme.muted, tags=("button", "scope"))
        self.action("scope", self.open_scope_menu, "选择会话累计或最近调用：影响用量及缓存命中率，均速保持本轮最近已结算调用口径")
        total_label = self.metric_label(compact(usage.total_tokens), usage.total_tokens, 104)
        self.number(488, 28, total_label, 104, self.theme.accent, 20, True, ("tokens", "compact_total"))
        self.text(500, 29, "tok", self.theme.muted, 12, tags=("tokens",))
        self.hint("tokens", self.usage_hint)
        self.line(536, 17, 536, 39)
        self.text(550, 27, "缓存命中", self.theme.muted, 12, tags=("cache",))
        self.text(680, 27, "—" if rate is None else f"{rate:.1f}%", self.theme.success, 18, anchor="e", bold=True, mono=True, tags=("cache", "cache_rate"))
        self.line(550, 41, 680, 41, self.theme.border, 2, ("cache",))
        if rate is not None and rate > 0:
            self.line(550, 41, 550 + 130 * rate / 100, 41, self.theme.success, 2, ("cache",))
        self.hint("cache", self.cache_hint)
        self.line(694, 17, 694, 39)
        following = bool(self.follow_selection.path)
        badge = ("跟随中" if following else state) if self.follow_enabled else "固定会话"
        self.rounded(704, 11, 782, 45, 6, self.theme.surface, tags=("follow",))
        self.text(743, 28, badge, self.theme.accent if following or not self.follow_enabled else self.theme.muted,
                  12, anchor="center", tags=("follow",))
        self.hint("follow", self.follow_hint)
        self.hitbox(790, 820, "pin", selected=self.topmost)
        pin_color = self.theme.accent if self.topmost else self.theme.muted
        self.line(800, 21, 810, 21, pin_color, 1, ("button", "pin"))
        self.line(802, 21, 802, 27, pin_color, 1, ("button", "pin"))
        self.line(808, 21, 808, 27, pin_color, 1, ("button", "pin"))
        self.line(800, 28, 810, 28, pin_color, 1, ("button", "pin"))
        self.line(805, 28, 805, 35, pin_color, 1, ("button", "pin"))
        self.action("pin", self.toggle_topmost, "置顶已开启，点击取消" if self.topmost else "点击保持面板置顶")
        self.hitbox(826, 866, "details")
        self.text(846, 28, "收起" if self.expanded else "明细", self.theme.text, 12, anchor="center", tags=("button", "details"))
        self.action("details", lambda: self.set_expanded(not self.expanded), "展开或收起精确用量；Esc 可收起")
        self.hitbox(872, 904, "close")
        self.line(884, 24, 892, 32, self.theme.muted, tags=("button", "close"))
        self.line(892, 24, 884, 32, self.theme.muted, tags=("button", "close"))
        self.action("close", self.close, "关闭统计面板")
        if self.expanded:
            first = c.find_all()[-1]
            self.draw_details(usage)
            for item in c.find_all():
                if item > first:
                    c.move(item, 0, self.px(8))
        self.line(self.WIDTH - 13, height - 4, self.WIDTH - 4, height - 13, self.theme.muted, tags=("resize_grip",))
        self.line(self.WIDTH - 8, height - 4, self.WIDTH - 4, height - 8, self.theme.muted, tags=("resize_grip",))
        if self.resize_origin:
            self.canvas.configure(cursor=self.RESIZE_CURSORS[self.resize_origin[0]])
        if self.active_popup:
            # Tk may restack the layered panel while its canvas is redrawn.
            # Restore menu order after pending drawing, without taking focus.
            self.active_popup.schedule_restack()

    def draw_speed(self):
        self.text(76, 28, "均速", self.theme.muted, 12, tags=("speed",))
        speed = self.snapshot.speed
        speed_text = self.metric_label(speed_label(speed), speed.tokens_per_second, 78, "≈")
        self.number(190, 28, speed_text, 78, self.theme.accent, 18, True, ("speed", "speed_value"))
        self.text(200, 29, "tok/s", self.theme.muted, 12, tags=("speed",))
        self.line(256, 17, 256, 39)
        self.hint("speed", self.speed_hint)

    def draw_icon(self):
        self.rounded(8, 7, 54, 49, 7, self.background, tags=("state",))
        self.canvas.create_image(self.px(8), self.px(6), anchor="nw",
                                 tags=("state", "status_icon"))
        self.icon_frame = None
        self.update_icon()

    def update_icon(self):
        if self.active_popup:
            self.active_popup.schedule_restack()
        frame = frame_for(self.icon, self.scale, self.state()[0],
                          time.monotonic() - self.animation_start, self.theme, self.animation_enabled)
        if frame == self.icon_frame:
            return
        self.icon_image = self.icon_images.get(frame)
        self.canvas.itemconfigure("status_icon", image=self.icon_image)
        self.icon_frame = frame

    def animate_icon(self):
        self.animation_job = None
        if self.closed:
            return
        try:
            self.update_icon()
        except Exception as exc:
            self.report_reader_error("状态图标绘制", exc)
        finally:
            if not self.closed:
                self.animation_job = self.root.after(65 if self.state()[0] == "生成中" else 100, self.animate_icon)

    def refresh_visual_settings(self, force=False):
        now = time.monotonic()
        if (not force and now < self.preference_check) or not self.material:
            return
        self.preference_check = now + 5
        preferences = visual_preferences()
        if not force and preferences == self.preferences:
            return
        self.preferences = preferences
        self.animation_enabled = preferences[1]
        # The current GDI/acrylic composition adds its tint to foreground pixels;
        # a pale tint washes out dark ink. Use opaque light surfaces for legibility.
        self.material.apply(preferences[0] and self.theme.glass, self.theme.background)
        if not self.theme.glass:
            self.material.reason = f"{self.theme.name}使用实色背景，确保深色文字清晰"
        self.background = "#000000" if self.material.enabled else self.theme.background
        canvas_background = "#000000" if self.material.enabled else TRANSPARENT
        if os.name == "nt" and not self.material.enabled:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.configure(bg=canvas_background)
        self.canvas.configure(bg=canvas_background)
        self.material.resize(self.px(self.WIDTH), self.px(self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT), self.px(10))
        self.render()

    def draw_details(self, usage):
        right, middle = self.WIDTH - 22, self.WIDTH // 2
        self.line(18, 49, self.WIDTH - 18, 49)
        session_id = self.snapshot.session_id
        self.text(22, 72, self.ellipsis(self.session_name(), self.WIDTH - 165, 13, True), self.theme.text, 13, bold=True, tags=("session", "session_title"))
        self.text(right, 72, session_id[:8] or "尚无 ID", self.theme.muted, 11, anchor="e", mono=True, tags=("session",))
        self.hint("session", self.follow_hint)
        state, reason, state_color = self.state()
        self.text(22, 96, state, state_color, 11, tags=("detail_state",))
        self.hint("detail_state", f"{state}：{reason}")
        self.text(right, 96, self.data_time_text(), self.theme.muted, 11, anchor="e", tags=("data_time",))
        self.hint("data_time", lambda: "用量统计记录时间（本地时区）：" + timestamp_label(self.snapshot.timestamp) +
                  "\n不是界面刷新时间；空闲时保留上次有效记录时间")
        if state in ("等待会话", "读取中", "等待统计", "读取失败"):
            self.text(22, 117, self.ellipsis(reason, self.WIDTH - 44, 11), state_color, 11, tags=("reason",))
            self.hint("reason", reason)
        self.rounded(16, 136, self.WIDTH - 16, 287, 8, self.theme.surface)
        self.text(28, 154, f"{self.scope_label}用量 · tokens", self.theme.muted, 11, tags=("usage_scope",))
        self.hint("usage_scope", self.usage_hint)
        self.line(middle, 175, middle, 247, self.theme.border)
        entries = [
            (28, 183, "输入总量", usage.input_tokens, middle - 24, self.theme.text, True, "input"),
            (44, 210, "已缓存", usage.cached_input_tokens, middle - 24, self.theme.success, False, "cached"),
            (44, 237, "未缓存", usage.uncached_input, middle - 24, self.theme.muted, False, "uncached"),
            (middle + 20, 183, "输出总量", usage.output_tokens, right - 8, self.theme.text, True, "output"),
            (middle + 36, 210, "其中推理", usage.reasoning_output_tokens, right - 8, self.theme.muted, False, "reasoning"),
        ]
        for x, y, label, number, value_x, color, bold, tag in entries:
            self.text(x, y, label, self.theme.text if bold else self.theme.muted, 12, bold=bold, tags=(tag,))
            self.number(value_x, y, exact(number), value_x-x-90, color, 14 if bold else 13, bold, (tag, tag + "_value"))
            self.hint(tag, self.usage_hint)
        self.line(28, 255, right - 8, 255)
        self.text(28, 272, "总用量", self.theme.text, 12, bold=True, tags=("total",))
        self.text(95, 272, "= 输入 + 输出", self.theme.muted, 11, tags=("total",))
        self.number(right - 8, 272, exact(usage.total_tokens), self.WIDTH - 250, self.theme.accent, 15, True, ("total", "total_value"))
        self.hint("total", self.usage_hint)
        self.text(22, 309, self.ellipsis(self.call_summary(), self.WIDTH - 44, 12), self.theme.accent, 12, tags=("call_summary",))
        self.hint("call_summary", lambda: self.call_summary() + "\n" + self.speed_hint())
        self.text(22, 341, "统计口径 ⓘ", self.theme.muted, 11, tags=("method",))
        self.hint("method", self.method_hint)
        for kind, x1, x2, label in (
                ("theme", 236, 416, f"主题：{self.theme.name}"),
                ("icon", 442, 650, f"图标：{ICONS[self.icon]}")):
            self.hitbox(x1, x2, kind, 326, 356)
            self.text(x1 + 12, 341, label, self.theme.text, 11, tags=("button", kind, kind + "_label"))
            self.line(x2 - 20, 339, x2 - 16, 343, self.theme.muted, tags=("button", kind))
            self.line(x2 - 16, 343, x2 - 12, 339, self.theme.muted, tags=("button", kind))
            self.action(kind, lambda value=kind: self.open_appearance_menu(value),
                        "主题与图标独立选择，切换立即生效并保存；也可从右键菜单选择")
        self.hitbox(self.WIDTH - 115, self.WIDTH - 20, "copy", 326, 356)
        self.text(self.WIDTH - 67, 341, "已复制" if self.copy_until else "复制统计", self.theme.accent, 11,
                  anchor="center", tags=("button", "copy", "copy_label"))
        self.action("copy", self.copy_stats, "复制当前范围的精确用量、缓存命中率、速度口径及数据时间")

    def data_time_text(self):
        return "数据时间：" + data_age(self.snapshot.timestamp)

    def refresh_transient(self):
        # Update only these items: idle clocks must not redraw hovered controls.
        self.refresh_visual_settings()
        if self.expanded:
            self.canvas.itemconfigure("data_time", text=self.data_time_text())
        if self.copy_until and time.monotonic() >= self.copy_until:
            self.copy_until = 0
            self.canvas.itemconfigure("copy_label", text="复制统计")
        if self.settings_error and time.monotonic() >= self.settings_retry_at:
            self.save_settings()

    def hover(self, event, tag, clickable=False):
        self.hide_tooltip()
        if self.menu_open or self.resize_origin or self.drag_origin:
            return
        self.hovered_tag = tag
        if clickable:
            self.canvas.itemconfigure(tag + "_bg", fill=self.theme.hover)
        self.canvas.configure(cursor="hand2" if clickable else "")
        def show():
            hint = self.hints.get(tag, "")
            self.show_tooltip(event.x_root, event.y_root, hint() if callable(hint) else hint)
        self.tooltip_job = self.root.after(450, show)

    def show_tooltip(self, x, y, text):
        self.tooltip_job = None
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        label = tk.Label(tip, text=text, bg=self.theme.surface, fg=self.theme.text, padx=12, pady=9,
                         justify="left", font=self.font(11), relief="solid", bd=1,
                         wraplength=min(self.px(560), work_area(self.root)[2] - work_area(self.root)[0] - 40))
        label.pack()
        tip.update_idletasks()
        left, top, right, bottom = work_area(self.root)
        tip.geometry(f"+{max(left, min(x, right-tip.winfo_reqwidth()-8))}+{max(top, min(y+20, bottom-tip.winfo_reqheight()-8))}")
        self.tooltip = tip

    def hide_tooltip(self, _event=None):
        if self.hovered_tag in self.button_fills:
            self.canvas.itemconfigure(self.hovered_tag + "_bg", fill=self.button_fills[self.hovered_tag])
        self.hovered_tag = None
        if self.tooltip_job:
            self.root.after_cancel(self.tooltip_job)
            self.tooltip_job = None
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None
        self.canvas.configure(cursor="")

    def report_reader_error(self, name, error):
        # pythonw has no stderr: record background failures explicitly, with a rate limit.
        signature = (type(error).__name__, str(error))
        now = time.monotonic()
        with self.reader_log_lock:
            previous = self.reader_errors.get(name)
            if previous and previous[0] == signature and now - previous[1] < 60:
                return
            self.reader_errors[name] = (signature, now)
            try:
                with (self.state_dir / "panel-error.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"\n{datetime.now().astimezone().isoformat(timespec='seconds')} [{name}] 失败，自动重试\n")
                    traceback.print_exception(type(error), error, error.__traceback__, file=stream)
            except OSError:
                pass

    def report_reader_recovered(self, name):
        with self.reader_log_lock:
            if self.reader_errors.pop(name, None) is None:
                return
            try:
                with (self.state_dir / "panel-error.log").open("a", encoding="utf-8") as stream:
                    stream.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} [{name}] 已恢复\n")
            except OSError:
                pass

    def ensure_readers(self):
        # A stopped worker is evidence of failure; old usage timestamps are not.
        if (self.follow_enabled and not self.follow_stop.is_set()
                and self.follow_worker is not None and not self.follow_worker.is_alive()):
            reason = "自动跟随线程意外停止，正在恢复"
            self.report_reader_error("自动跟随", RuntimeError(reason))
            while not self.follow_queue.empty():
                self.follow_queue.get_nowait()
            self.apply_follow_selection(Selection(error=reason))
            self.start_follow_reader()
            return True
        if (not self.stop.is_set() and self.worker is not None and not self.worker.is_alive()
                and (not self.follow_enabled or self.follow_selection.path)):
            reason = "用量读取线程意外停止，正在恢复"
            self.report_reader_error("用量读取", RuntimeError(reason))
            self.start_reader()
            self.snapshot.loading, self.snapshot.error = False, reason
            return True
        return False

    def reader_checked(self, name, error="", generation=None, **details):
        with self.health_lock:
            current = self.generation if name == "usage" else self.follow_generation
            if generation != current:
                return
            health = self.reader_health.setdefault(name, {})
            health.update(last_check=datetime.now().astimezone().isoformat(timespec="seconds"), error=error, **details)
            if not error:
                health["last_success"] = health["last_check"]

    def restart_following(self):
        if not self.follow_enabled:
            raise ValueError("当前为固定会话模式，请使用“重新读取统计”")
        self.follow_stop.set()
        self.follow_stop = threading.Event()
        self.apply_follow_selection(Selection(loading=True, message="正在重新识别当前聊天"))
        self.start_follow_reader()
        self.render()

    def reload_usage(self):
        if self.follow_enabled and not self.follow_selection.path:
            raise ValueError("尚未识别本地会话，请先重新识别当前聊天")
        self.read_cache.invalidate(self.path)
        self.start_reader()
        self.render()

    def start_reader(self):
        self.stop.set()
        self.stop = threading.Event()
        self.generation += 1
        generation, stop, path = self.generation, self.stop, self.path
        self.read_cache.activate(generation)
        with self.health_lock:
            self.reader_health["usage"] = {}
        session_id = self.follow_selection.session_id if self.follow_enabled else ""
        self.snapshot = Snapshot(session_id=session_id)
        interval = max(0.25, float(self.config.get("refresh_ms", 1000)) / 1000)

        def worker():
            tailer = None
            reused = False
            while not stop.is_set():
                try:
                    if tailer is None:
                        tailer, reused = self.read_cache.acquire(path, generation, LogTailer)
                    copy = Snapshot(**vars(tailer.poll()))
                    self.read_cache.store(path, generation, tailer)
                    self.reader_checked("usage", copy.error, generation, offset=tailer.offset,
                                        reused_checkpoint=reused, skipped=copy.skipped)
                    if copy.error:
                        self.report_reader_error("用量读取", RuntimeError(copy.error))
                    else:
                        self.report_reader_recovered("用量读取")
                except Exception as exc:
                    self.report_reader_error("用量读取", exc)
                    # A failed batch may have advanced its offset; replay from the start.
                    self.read_cache.invalidate(path)
                    tailer = None
                    copy = Snapshot(loading=False, error=f"用量读取异常，正在重试：{type(exc).__name__}: {exc}")
                    self.reader_checked("usage", copy.error, generation)
                # Navigation already identifies the chat even if its log cannot be read yet.
                copy.session_id = copy.session_id or session_id
                try:
                    self.queue.put_nowait((generation, copy))
                except queue.Full:
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.queue.put_nowait((generation, copy))
                    except queue.Full:
                        pass
                stop.wait(max(1.0, interval) if copy.error else 0.01 if copy.loading else interval)

        self.worker = threading.Thread(target=worker, daemon=True, name="CodexLogReader")
        self.worker.start()

    def start_follow_reader(self):
        self.follow_generation += 1
        generation, stop = self.follow_generation, self.follow_stop
        with self.health_lock:
            self.reader_health["follow"] = {}
        def worker():
            follower = None
            while not stop.is_set():
                try:
                    if follower is None:
                        follower = DesktopSessionFollower(self.config["desktop_logs_root"], self.sessions_root,
                                                          window_id=int(self.config.get("follow_window_id", 1)))
                    selection = follower.poll()
                    if selection.error:
                        self.report_reader_error("自动跟随", RuntimeError(selection.error))
                    else:
                        self.report_reader_recovered("自动跟随")
                except Exception as exc:
                    self.report_reader_error("自动跟随", exc)
                    follower = None
                    selection = Selection(error=f"当前聊天识别失败，正在重试：{type(exc).__name__}: {exc}")
                self.reader_checked("follow", selection.error, generation)
                try:
                    self.follow_queue.put_nowait((generation, selection))
                except queue.Full:
                    try:
                        self.follow_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.follow_queue.put_nowait((generation, selection))
                    except queue.Full:
                        pass
                stop.wait(1.0 if selection.error else 0.5)

        self.follow_worker = threading.Thread(target=worker, daemon=True, name="CodexCurrentChat")
        self.follow_worker.start()

    def apply_follow_selection(self, selection):
        if selection == self.follow_selection:
            return False
        previous = self.follow_selection
        self.follow_selection = selection
        if selection.path:
            if previous.path != selection.path or previous.session_id != selection.session_id:
                self.select_session(selection.path)
                self.snapshot.session_id = selection.session_id
        else:
            # Never present the previous chat's counters as the newly opened chat.
            self.stop.set()
            self.generation += 1
            self.read_cache.activate(self.generation)
            with self.health_lock:
                self.reader_health["usage"] = {}
            self.snapshot = Snapshot(session_id=selection.session_id, loading=selection.loading,
                                     error=selection.error, message=selection.message)
        return True

    def publish_status(self):
        if time.monotonic() >= self.heartbeat_at:
            self.reported_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.heartbeat_at = time.monotonic() + 1
        with self.health_lock:
            health = {name: dict(value) for name, value in self.reader_health.items()}
        data = {"running": not self.closed, "process_id": os.getpid(), "close_reason": self.close_reason,
                "reported_at": self.reported_at, "hidden": self.hidden,
                "reader_health": health, "read_cache": self.read_cache.summary(),
                "settings_error": self.settings_error, "last_action": self.last_action,
                "session_name": self.session_name(),
                "usage_log": str(self.path) if not self.follow_enabled or self.follow_selection.path else "",
                "usage_log_available": bool(not self.follow_enabled or self.follow_selection.path),
                "auto_follow": self.follow_enabled,
                "follow_reader_alive": bool(self.follow_worker and self.follow_worker.is_alive()),
                "usage_reader_alive": bool(self.worker and self.worker.is_alive()),
                "material": "acrylic" if self.material and self.material.enabled else "solid",
                "material_reason": self.material.reason if self.material else "尚未初始化",
                "animation_enabled": self.animation_enabled,
                "theme": self.theme_id, "icon": self.icon,
                "session_id": self.snapshot.session_id, "mode": self.mode,
                "route": self.follow_selection.route if self.follow_enabled else "",
                "source_log": self.follow_selection.source_log if self.follow_enabled else "",
                "total_tokens": self.snapshot.total.total_tokens,
                "cached_input_tokens": self.snapshot.total.cached_input_tokens,
                "input_tokens": self.snapshot.total.input_tokens,
                "last_tokens": self.snapshot.last.total_tokens,
                "cache_percent": self.snapshot.total.cache_rate,
                "tokens_per_second": self.snapshot.speed.tokens_per_second,
                "speed_output_tokens": self.snapshot.speed.output_tokens,
                "speed_duration_ms": self.snapshot.speed.duration_ms,
                "speed_timestamp": self.snapshot.speed.timestamp,
                "speed_turn_id": self.snapshot.speed.turn_id,
                "usage_timestamp": self.snapshot.timestamp,
                "error": self.snapshot.error, "loading": self.snapshot.loading,
                "display_state": self.state()[0], "state_reason": self.state()[1]}
        if data == self.last_status:
            return
        try:
            temporary = self.state_dir / "status.tmp"
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_dir / "status.json")
            self.last_status = data
        except OSError:
            pass

    def tick(self):
        if self.closed:
            return
        self.poll_job = None
        try:
            self.refresh_state()
            self.report_reader_recovered("界面刷新")
        except Exception as exc:
            self.report_reader_error("界面刷新", exc)
        finally:
            # An unexpected callback failure must not permanently cancel all future updates.
            if not self.closed:
                self.poll_job = self.root.after(150, self.tick)

    def refresh_state(self):
        if self.close_signal is not None and self.close_signal.pending():
            self.close(reason="codex_closed")
            return
        if self.commands:
            for command in self.commands.pending():
                self.handle_command(command)
        self.ensure_visible()
        changed = self.ensure_readers()
        latest_selection = None
        try:
            while True:
                generation, selection = self.follow_queue.get_nowait()
                if generation == self.follow_generation:
                    latest_selection = selection
        except queue.Empty:
            pass
        if latest_selection is not None:
            changed = self.apply_follow_selection(latest_selection) or changed
        try:
            while True:
                generation, snapshot = self.queue.get_nowait()
                if generation == self.generation and snapshot != self.snapshot:
                    self.snapshot = snapshot
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self.render()
        self.refresh_transient()
        if self.active_popup:
            self.active_popup.schedule_restack()
        self.publish_status()

    def toggle_mode(self):
        self.set_mode("last" if self.mode == "total" else "total")

    def set_mode(self, mode):
        if mode not in ("total", "last"):
            return
        self.mode = mode
        self.scope_variable.set(mode)
        self.copy_until = 0
        self.hide_tooltip()
        self.render()
        self.save_settings()

    def open_scope_menu(self):
        self.hide_tooltip()
        if self.scope_menu is not None:
            self.scope_menu.destroy()
        menu = self.scope_menu = self.new_menu()
        self.scope_variable.set(self.mode)
        for mode, label in (("total", "会话累计"), ("last", "最近调用")):
            menu.add_radiobutton(label=label, value=mode, variable=self.scope_variable,
                                 command=lambda value=mode: self.set_mode(value))
        self.popup_menu(menu, self.canvas.winfo_rootx() + self.px(264), self.canvas.winfo_rooty() + self.px(48))

    def new_menu(self, parent=None):
        return tk.Menu(parent or self.root, tearoff=False, bg=self.theme.surface, fg=self.theme.text,
                       activebackground=self.theme.hover, activeforeground=self.theme.text,
                       disabledforeground=self.theme.muted, selectcolor=self.theme.accent, font=self.font(12))

    def appearance_menu(self, kind, parent=None):
        menu = self.new_menu(parent)
        if kind == "theme":
            variable, selected, setter = self.theme_variable, self.theme_id, self.set_theme
            options = [(key, value.name) for key, value in THEMES.items()]
        else:
            variable, selected, setter = self.icon_variable, self.icon, self.set_icon
            options = ICONS.items()
        variable.set(selected)
        for value, label in options:
            menu.add_radiobutton(label=label, value=value, variable=variable,
                                 command=lambda selected=value: setter(selected))
        return menu

    def open_appearance_menu(self, kind):
        self.hide_tooltip()
        previous = self.appearance_menus.get(kind)
        if previous is not None:
            previous.destroy()
        menu = self.appearance_menus[kind] = self.appearance_menu(kind)
        x = 236 if kind == "theme" else 442
        self.popup_menu(menu, self.canvas.winfo_rootx() + self.px(x), self.canvas.winfo_rooty() + self.px(364))

    def set_theme(self, theme_id):
        if theme_id not in THEMES or theme_id == self.theme_id:
            return
        self.hide_tooltip()
        self.theme_id, self.theme = theme_id, THEMES[theme_id]
        self.theme_variable.set(theme_id)
        self.refresh_visual_settings(force=True)
        self.save_settings()
        self.publish_status()

    def set_icon(self, icon):
        if icon not in ICONS or icon == self.icon:
            return
        self.hide_tooltip()
        self.icon = icon
        self.icon_variable.set(icon)
        self.animation_start = time.monotonic()
        self.render()
        self.save_settings()
        self.publish_status()

    def popup_menu(self, menu, x, y):
        self.menu_open = True
        self.hide_tooltip()
        try:
            self.active_popup = ThemedMenu(self.root, menu, self.theme, self.font(12), self.scale, work_area(self.root))
            self.active_popup.show(x, y)
        finally:
            self.active_popup = None
            self.menu_open = False
            if not self.closed:
                self.hide_tooltip()

    def toggle_topmost(self):
        self.topmost = not self.topmost
        self.root.attributes("-topmost", self.topmost)
        self.hide_tooltip()
        self.render()
        self.save_settings()

    def set_expanded(self, expanded):
        if expanded == self.expanded:
            return
        self.root.update_idletasks()
        if expanded:
            self.compact_position = (self.root.winfo_x(), self.root.winfo_y())
        area = work_area(self.root)
        self.expanded = expanded
        x, y = self.compact_position
        # Collapse always returns to the saved compact anchor, never the expanded bottom edge.
        self.place_window(x, y, area=area)
        self.hide_tooltip()
        self.render()

    def drag_start(self, event):
        self.root.update_idletasks()
        edge = self.resize_edge(event.x, event.y)
        if edge:
            self.pressed_action = None
            self.drag_origin = None
            self.hide_tooltip()
            self.resize_origin = (edge, event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y(), self.scale)
            self.canvas.grab_set()
            self.canvas.configure(cursor=self.RESIZE_CURSORS[edge])
            return
        if "button" in self.canvas.gettags("current"):
            self.drag_origin = None
            return
        self.pressed_action = None
        self.hide_tooltip()
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.canvas.grab_set()
        self.canvas.configure(cursor="fleur")

    def drag_motion(self, event):
        if self.resize_origin:
            edge, sx, sy, x, y, original = self.resize_origin
            height = self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT
            dx, dy = event.x_root - sx, event.y_root - sy
            vx = -self.WIDTH if "w" in edge else self.WIDTH if "e" in edge else 0
            vy = -height if "n" in edge else height if "s" in edge else 0
            area = work_area(self.root)
            scale = self.limited_scale(original + (dx * vx + dy * vy) / (vx * vx + vy * vy), area)
            dw, dh = round(self.WIDTH * original) - round(self.WIDTH * scale), round(height * original) - round(height * scale)
            x += dw if "w" in edge else dw // 2 if not vx else 0
            y += dh if "n" in edge else dh // 2 if not vy else 0
            self.zoom = max(self.MIN_ZOOM, min(scale / self.dpi_scale, self.MAX_ZOOM))
            if self.place_window(x, y, scale, area):
                self.render()
        elif self.drag_origin:
            sx, sy, x, y = self.drag_origin
            # The pointer selects the destination monitor, allowing bounded cross-screen moves.
            area = work_area(self.root, (event.x_root, event.y_root, event.x_root + 1, event.y_root + 1))
            old_scale = self.scale
            self.place_window(x + event.x_root - sx, y + event.y_root - sy, area=area)
            if self.scale != old_scale:
                self.render()

    def drag_end(self, _event):
        self.pressed_action = None
        if self.drag_origin or self.resize_origin:
            moved = self.drag_origin is not None
            self.drag_origin = None
            self.resize_origin = None
            self.canvas.grab_release()
            self.root.update_idletasks()
            if moved or not self.expanded:
                self.compact_position = (self.root.winfo_x(), self.root.winfo_y())
            self.ensure_visible()
            self.save_settings()
            self.canvas.configure(cursor="")

    def context_menu(self, event):
        self.hide_tooltip()
        if self.context_popup is not None:
            self.context_popup.destroy()
        menu = self.context_popup = self.new_menu()
        menu.add_command(label="展开明细" if not self.expanded else "收起明细", command=lambda: self.set_expanded(not self.expanded))
        menu.add_command(label="切换到最近调用" if self.mode == "total" else "切换到会话累计", command=self.toggle_mode)
        menu.add_cascade(label=f"主题：{self.theme.name}", menu=self.appearance_menu("theme", menu))
        menu.add_cascade(label=f"图标：{ICONS[self.icon]}", menu=self.appearance_menu("icon", menu))
        menu.add_command(label="自动跟随当前聊天" if self.follow_enabled else "跟踪指定日志", state="disabled")
        menu.add_command(label="取消置顶" if self.topmost else "保持置顶", command=self.toggle_topmost)
        menu.add_command(label="恢复默认大小", command=self.reset_size)
        menu.add_command(label="复制当前统计", command=self.copy_stats)
        menu.add_command(label="运行状态与诊断…", command=self.open_diagnostics)
        if self.settings_error:
            menu.add_command(label="设置未保存：立即重试", command=self.save_settings)
        menu.add_command(label="启动设置…", command=self.open_launcher)
        menu.add_separator()
        menu.add_command(label="退出", command=self.close)
        self.popup_menu(menu, event.x_root, event.y_root)

    def open_launcher(self):
        import subprocess
        subprocess.Popen(app_command("--settings"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def open_diagnostics(self):
        import subprocess
        subprocess.Popen(app_command("--diagnostics"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def handle_command(self, command):
        labels = {"show": "显示面板", "hide": "隐藏面板", "recenter": "找回面板",
                  "rediscover": "重新识别聊天", "reload": "重新读取统计", "retry_save": "重试保存设置"}
        try:
            if command in ("show", "hide", "recenter"):
                if self.active_popup:
                    self.active_popup.dismiss()
                self.hide_tooltip()
                self.hide_save_notice()
                if command == "hide":
                    self.hidden = True
                    self.root.withdraw()
                else:
                    self.hidden = False
                    self.root.deiconify()
                    self.root.update_idletasks()
                    if command == "recenter":
                        x, y = self.root.winfo_pointerxy()
                        area = work_area(self.root, (x, y, x+1, y+1))
                        left, top, right, bottom = area
                        x = left + (right-left-self.px(self.WIDTH)) // 2
                        y = top + (bottom-top-self.px(self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT)) // 2
                        self.compact_position = (x, y)
                        self.place_window(x, y, area=area)
                        self.render()
                        self.save_settings()
                    self.ensure_visible()
                    self.root.lift()
            elif command == "rediscover":
                self.restart_following()
            elif command == "reload":
                self.reload_usage()
            elif command == "retry_save":
                if not self.save_settings():
                    raise OSError(self.settings_error)
            else:
                raise ValueError("未知操作")
            self.last_action = labels[command] + ("：已开始" if command in ("reload", "rediscover") else "：已完成")
        except Exception as exc:
            self.last_action = labels.get(command, command) + "：" + str(exc)
            self.report_reader_error("面板操作", exc)
        self.publish_status()

    def stats_text(self):
        usage = self.current_usage
        rate = "—" if usage.cache_rate is None else f"{usage.cache_rate:.2f}%"
        state, reason, _ = self.state()
        return (f"Codex 用量统计\n统计范围：{self.scope_label}\n"
                f"聊天：{self.session_name()}\n会话 ID：{self.snapshot.session_id or '—'}\n"
                f"状态：{state}（{reason}）\n单位：tokens\n"
                f"输入总量：{exact(usage.input_tokens)}\n  已缓存：{exact(usage.cached_input_tokens)}\n"
                f"  未缓存：{exact(usage.uncached_input)}\n输出总量：{exact(usage.output_tokens)}\n"
                f"  其中推理：{exact(usage.reasoning_output_tokens)}\n总用量（输入 + 输出）：{exact(usage.total_tokens)}\n"
                f"缓存命中率：{rate}（已缓存 ÷ 输入）\n{self.call_summary()}\n"
                "速度口径：本轮最近一次已结算调用的平均生成速度，不随统计范围切换；≈ 为本地记录估算，非逐 token 瞬时速度。"
                "包含推理和工具参数输出，不计工具执行等待。\n"
                "会话累计用量不代表当前上下文占用；缓存属于输入，推理属于输出；— 表示缺少有效数据。\n"
                f"数据时间（用量统计记录，本地时区）：{timestamp_label(self.snapshot.timestamp)}")

    def copy_stats(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.stats_text())
        self.copy_until = time.monotonic() + 1.8
        self.canvas.itemconfigure("copy_label", text="已复制")

    def select_session(self, path):
        self.path = Path(path)
        self.titles = read_titles(self.sessions_root)
        self.start_reader()
        self.render()
        self.save_settings()

    def load_settings(self):
        try:
            value = json.loads((self.state_dir / "settings.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, UnicodeError):
            return {}

    def save_settings(self):
        if self.closed:
            return False
        if not self.expanded:
            self.root.update_idletasks()
            self.compact_position = (self.root.winfo_x(), self.root.winfo_y())
        x, y = self.compact_position
        value = {"session": str(self.path), "mode": self.mode, "topmost": self.topmost,
                 "x": x, "y": y, "theme": self.theme_id, "icon": self.icon}
        if not math.isclose(self.zoom, 1.0):
            value["zoom"] = round(self.zoom, 6)
        temporary = self.state_dir / "settings.tmp"
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_dir / "settings.json")
        except OSError as exc:
            message = f"{type(exc).__name__}：{exc}"
            changed = message != self.settings_error
            self.settings_error = message
            self.settings_retry_at = time.monotonic() + 5
            self.report_reader_error("设置保存", exc)
            if changed:
                self.show_save_notice()
            return False
        self.settings_error = ""
        self.settings_retry_at = 0
        self.hide_save_notice()
        self.report_reader_recovered("设置保存")
        return True

    def hide_save_notice(self):
        if self.save_notice_job is not None:
            self.root.after_cancel(self.save_notice_job)
            self.save_notice_job = None
        if self.save_notice is not None:
            self.save_notice.destroy()
            self.save_notice = None

    def show_save_notice(self):
        self.hide_save_notice()
        if self.hidden:
            return
        tip = self.save_notice = tk.Toplevel(self.root)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=self.theme.border)
        label = tk.Label(tip, text="设置未保存，将自动重试\n当前外观仍生效；重启可能恢复旧设置。\n点击重试，或从右键菜单打开运行状态查看原因。",
                         bg=self.theme.surface, fg=self.theme.warning, font=self.font(12),
                         justify="left", wraplength=self.px(380), padx=self.px(12), pady=self.px(10))
        label.pack(padx=1, pady=1)
        label.bind("<Button-1>", lambda _e: self.save_settings())
        tip.update_idletasks()
        width, height = tip.winfo_reqwidth(), tip.winfo_reqheight()
        area = work_area(self.root)
        x, y = self.root.winfo_x(), self.root.winfo_y() - height - self.px(6)
        if y < area[1]:
            y = self.root.winfo_y() + self.root.winfo_height() + self.px(6)
        x, y = clamp_position(x, y, width, height, area)
        tip.geometry(f"+{x}+{y}")
        tip.deiconify()
        self.save_notice_job = self.root.after(8000, self.hide_save_notice)

    def close(self, reason="user"):
        if self.closed:
            return
        self.save_settings()
        self.close_reason = reason
        self.closed = True
        self.stop.set()
        self.follow_stop.set()
        self.publish_status()
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
        if self.animation_job:
            self.root.after_cancel(self.animation_job)
            self.animation_job = None
        self.hide_tooltip()
        self.hide_save_notice()
        self.icon_images.clear()
        self.icon_image = None
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--session", help="Path to a Codex rollout JSONL")
    parser.add_argument("--state-dir")
    parser.add_argument("--data-dir", help=argparse.SUPPRESS)
    parser.add_argument("--snapshot", action="store_true", help="Read usage once without opening a window")
    args = parser.parse_args(argv)
    config = load_config()
    state_dir = Path(args.state_dir or config["state_dir"])
    if args.snapshot:
        snapshot = read_snapshot(args.session or config["default_session"])
        result = asdict(snapshot)
        result["total_cache_percent"] = snapshot.total.cache_rate
        result["last_cache_percent"] = snapshot.last.cache_rate
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if snapshot.error else 0
    enable_dpi()
    mutex, exists = singleton()
    kernel = ctypes.windll.kernel32
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    if exists:
        if mutex:
            kernel.CloseHandle(mutex)
        return 0
    root = tk.Tk()

    def report_exception(exc_type, exc_value, exc_traceback):
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "panel-error.log").open("a", encoding="utf-8") as stream:
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=stream)
        messagebox.showerror(APP_TITLE, "面板遇到错误，详细信息已写入工作目录的 panel-error.log。", parent=root)

    root.report_callback_exception = report_exception
    from panel_control import PanelCommands
    close_signal = commands = None
    try:
        close_signal = PanelCloseSignal()
        commands = PanelCommands()
        UsagePanel(root, config, state_dir, args.session, close_signal=close_signal, commands=commands)
        root.mainloop()
    finally:
        if close_signal is not None:
            close_signal.close()
        if commands is not None:
            commands.close()
        if mutex:
            kernel.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
