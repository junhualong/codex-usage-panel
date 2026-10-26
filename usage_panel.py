"""A read-only, local Codex token-usage panel."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
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


APP_TITLE = "Codex 实时用量"
BACKGROUND = "#1c1e24"
SURFACE = "#24272f"
BORDER = "#383c47"
TEXT = "#e5e9f1"
MUTED = "#929ba9"
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
                self.offset = stream.tell()
                self.consume(data)
                self.snapshot.loading = self.offset < os.fstat(stream.fileno()).st_size
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
        return "尚无用量记录"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        return "时间格式未知"


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


def work_area(root):
    if os.name == "nt":
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


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
    SPEED_WIDTH = 132
    WIDTH = 600 + SPEED_WIDTH
    BAR_HEIGHT = 48
    EXPANDED_HEIGHT = 316

    def __init__(self, root, config, state_dir, session_override=None, close_signal=None):
        self.root = root
        self.close_signal = close_signal
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
        self.topmost = bool(self.saved.get("topmost", True))
        self.expanded = False
        self.snapshot = Snapshot()
        self.stop = threading.Event()
        self.queue = queue.Queue(maxsize=2)
        self.follow_queue = queue.Queue(maxsize=2)
        self.follow_stop = threading.Event()
        self.follow_selection = Selection()
        self.last_status = None
        self.generation = 0
        self.closed = False
        self.tooltip = None
        self.tooltip_job = None
        self.fonts = {}
        self.poll_job = None
        self.drag_origin = None
        root.withdraw()
        root.title(APP_TITLE)
        root.configure(bg=TRANSPARENT)
        root.overrideredirect(True)
        root.attributes("-topmost", self.topmost)
        if os.name == "nt":
            root.attributes("-transparentcolor", TRANSPARENT)
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.canvas = tk.Canvas(root, bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        left, top, right, bottom = work_area(root)
        width, height = self.px(self.WIDTH), self.px(self.BAR_HEIGHT)
        x = int(self.saved.get("x", right - width - self.px(24)))
        y = int(self.saved.get("y", bottom - height - self.px(24)))
        # Recover windows saved beyond the current primary desktop.
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        self.compact_position = (x, y)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _e: self.set_expanded(False))
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_motion)
        self.canvas.bind("<ButtonRelease-1>", self.drag_end)
        self.canvas.bind("<Button-3>", self.context_menu)
        self.render()
        root.deiconify()
        if self.follow_enabled:
            self.snapshot = Snapshot(loading=False, error="正在识别当前聊天")
            self.start_follow_reader()
        else:
            self.start_reader()
        self.render()
        self.poll_job = root.after(100, self.tick)

    def px(self, value):
        return int(round(value * self.scale))

    def font(self, size=12, bold=False, mono=False):
        key = (size, bold, mono)
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(family="Consolas" if mono else "Microsoft YaHei UI",
                                          size=-self.px(size), weight="bold" if bold else "normal")
        return self.fonts[key]

    def text(self, x, y, value, color=TEXT, size=12, anchor="w", bold=False, mono=False, tags=()):
        return self.canvas.create_text(self.px(x), self.px(y), text=value, fill=color,
                                       font=self.font(size, bold, mono), anchor=anchor, tags=tags)

    def line(self, x1, y1, x2, y2, color=BORDER, width=1, tags=()):
        return self.canvas.create_line(self.px(x1), self.px(y1), self.px(x2), self.px(y2),
                                       fill=color, width=self.px(width), tags=tags)

    def rounded(self, x1, y1, x2, y2, radius, fill, outline="", tags=()):
        coords = [x1+radius,y1, x2-radius,y1, x2,y1, x2,y1+radius,
                  x2,y2-radius, x2,y2, x2-radius,y2, x1+radius,y2,
                  x1,y2, x1,y2-radius, x1,y1+radius, x1,y1]
        return self.canvas.create_polygon([self.px(v) for v in coords], smooth=True,
                                          splinesteps=24, fill=fill, outline=outline, tags=tags)

    def ellipsis(self, value, width, size=12):
        font = self.font(size)
        if font.measure(value) <= self.px(width):
            return value
        while value and font.measure(value + "…") > self.px(width):
            value = value[:-1]
        return value + "…"

    def action(self, tag, callback, help_text):
        self.canvas.tag_bind(tag, "<Button-1>", lambda _e: callback())
        self.canvas.tag_bind(tag, "<Enter>", lambda e: self.hover(e, help_text, True))
        self.canvas.tag_bind(tag, "<Leave>", self.hide_tooltip)

    def hitbox(self, x1, x2, tag):
        self.canvas.create_rectangle(self.px(x1), self.px(6), self.px(x2), self.px(42),
                                     fill=BACKGROUND, outline="", tags=("button", tag))

    def render(self):
        c = self.canvas
        c.delete("all")
        height = self.EXPANDED_HEIGHT if self.expanded else self.BAR_HEIGHT
        self.rounded(1, 1, self.WIDTH-1, height-1, 12, BACKGROUND, BORDER)
        usage = self.snapshot.total if self.mode == "total" else self.snapshot.last
        rate = usage.cache_rate
        if self.snapshot.error:
            state_color, brand = AMBER, "未连接"
        elif self.snapshot.loading:
            state_color, brand = AMBER, "读取中"
        else:
            state_color, brand = (GREEN if self.snapshot.active else MUTED), "CODEX"
        c.create_oval(self.px(15), self.px(21), self.px(21), self.px(27), fill=state_color, outline="")
        self.text(30, 24, brand, MUTED, 11, bold=True)
        self.line(91, 15, 91, 33)
        fixed_items = set(c.find_all())
        self.hitbox(99, 149, "scope")
        self.text(110, 24, "累计" if self.mode == "total" else "单次", MUTED, 11, tags=("button", "scope"))
        self.line(140, 22, 143, 25, MUTED, tags=("button", "scope"))
        self.line(143, 25, 146, 22, MUTED, tags=("button", "scope"))
        self.action("scope", self.toggle_mode, "点击切换：会话累计 / 最近一次模型调用")
        self.text(161, 24, compact(usage.total_tokens), ACCENT, 18, bold=True, mono=True, tags=("tokens",))
        self.text(249, 25, "tok", MUTED, 11, tags=("tokens",))
        self.canvas.tag_bind("tokens", "<Enter>", lambda e: self.hover(e,
            f"输入 {exact(usage.input_tokens)} + 输出 {exact(usage.output_tokens)} = {exact(usage.total_tokens)} tokens\nK = 1,000，M = 1,000,000；缓存已包含在输入中"))
        self.canvas.tag_bind("tokens", "<Leave>", self.hide_tooltip)
        self.line(281, 15, 281, 33)
        # Small database icon, drawn without relying on emoji glyphs.
        c.create_rectangle(self.px(296), self.px(20), self.px(306), self.px(28), outline=MUTED)
        c.create_oval(self.px(296), self.px(17), self.px(306), self.px(23), fill=BACKGROUND, outline=MUTED)
        c.create_arc(self.px(296), self.px(25), self.px(306), self.px(31), start=180, extent=180, style="arc", outline=MUTED)
        self.text(316, 23, "缓存命中", MUTED, 11, tags=("cache",))
        self.text(378, 23, "—" if rate is None else f"{rate:.1f}%", GREEN, 15, bold=True, mono=True, tags=("cache",))
        self.line(316, 35, 424, 35, BORDER, 2)
        if rate is not None and rate > 0:
            self.line(316, 35, 316 + 108 * rate / 100, 35, GREEN, 2)
        self.canvas.tag_bind("cache", "<Enter>", lambda e: self.hover(e,
            f"缓存命中 {exact(usage.cached_input_tokens)} / 输入 {exact(usage.input_tokens)} tokens\n缓存命中率不是费用节省比例"))
        self.canvas.tag_bind("cache", "<Leave>", self.hide_tooltip)
        self.line(440, 15, 440, 33)
        self.hitbox(446, 477, "pin")
        pin_color = ACCENT if self.topmost else MUTED
        self.line(457, 17, 467, 17, pin_color, 1, ("button", "pin"))
        self.line(459, 17, 459, 23, pin_color, 1, ("button", "pin"))
        self.line(465, 17, 465, 23, pin_color, 1, ("button", "pin"))
        self.line(457, 24, 467, 24, pin_color, 1, ("button", "pin"))
        self.line(462, 24, 462, 31, pin_color, 1, ("button", "pin"))
        self.action("pin", self.toggle_topmost, "置顶已开启，点击取消" if self.topmost else "点击保持面板置顶")
        self.hitbox(480, 513, "follow")
        self.text(496, 24, "自动" if self.follow_enabled else "固定", ACCENT, 11,
                  anchor="center", tags=("button", "follow"))
        self.action("follow", lambda: self.set_expanded(True),
                    "自动跟随 Codex 主窗口当前打开的聊天" if self.follow_enabled else "正在跟踪指定日志")
        self.hitbox(516, 556, "details")
        self.text(536, 24, "收起" if self.expanded else "明细", MUTED, 11, anchor="center", tags=("button", "details"))
        self.action("details", lambda: self.set_expanded(not self.expanded), "展开或收起精确用量；也可右键查看菜单")
        self.hitbox(562, 592, "close")
        self.line(573, 20, 581, 28, MUTED, tags=("button", "close"))
        self.line(581, 20, 573, 28, MUTED, tags=("button", "close"))
        self.action("close", self.close, "关闭统计面板")
        # Move the existing controls as one block to keep their hit areas aligned.
        for item in set(c.find_all()) - fixed_items:
            c.move(item, self.px(self.SPEED_WIDTH), 0)
        self.draw_speed()
        if self.expanded:
            self.draw_details(usage)

    def draw_speed(self):
        sample = self.snapshot.speed
        self.canvas.create_arc(self.px(103), self.px(17), self.px(118), self.px(32),
                               start=0, extent=180, style="arc", outline=MUTED, tags=("speed",))
        self.line(111, 25, 115, 20, MUTED, tags=("speed",))
        self.text(124, 24, speed_label(sample), ACCENT, 16, bold=True, mono=True, tags=("speed",))
        self.text(188, 25, "tok/s", MUTED, 10, tags=("speed",))
        self.line(223, 15, 223, 33)
        if sample.tokens_per_second is None:
            detail = "等待本轮调用结算及完整生成时间"
        else:
            detail = f"输出 {exact(sample.output_tokens)} tokens ÷ {sample.duration_ms / 1000:.3f} 秒"
        help_text = ("本轮最近一次模型调用的平均生成速度\n" + detail +
                     "\n含推理和工具参数输出；不计工具执行等待\n≈ 为本地记录估算，非逐 token 瞬时速度")
        self.canvas.tag_bind("speed", "<Enter>", lambda e: self.hover(e, help_text))
        self.canvas.tag_bind("speed", "<Leave>", self.hide_tooltip)

    def draw_details(self, usage):
        right, middle = self.WIDTH - 22, self.WIDTH // 2
        self.line(18, 49, self.WIDTH - 18, 49)
        session_id = self.snapshot.session_id
        name = self.titles.get(session_id) or ("当前 Codex 会话" if session_id else "等待当前会话")
        self.text(22, 74, self.ellipsis(name, self.WIDTH - 210, 13), TEXT, 13, bold=True)
        self.text(right, 74, session_id[:8] or "等待日志", MUTED, 10, anchor="e", mono=True)
        state = self.snapshot.error or ("正在读取历史记录" if self.snapshot.loading else
                "生成中" if self.snapshot.active else "等待新的调用")
        if self.follow_enabled and not self.snapshot.error:
            state = "自动跟随 · " + state
        self.text(22, 98, self.ellipsis(state, 285, 11), AMBER if self.snapshot.error else MUTED, 11)
        self.text(right, 98, "更新于 " + timestamp_label(self.snapshot.timestamp), MUTED, 10, anchor="e")
        self.rounded(16, 120, self.WIDTH - 16, 235, 8, SURFACE)
        self.line(middle, 136, middle, 219, BORDER)
        entries = [
            (28, 145, "输入 tokens", usage.input_tokens, middle - 24, TEXT),
            (28, 179, "缓存命中 tokens", usage.cached_input_tokens, middle - 24, GREEN),
            (28, 213, "未命中输入 tokens", usage.uncached_input, middle - 24, MUTED),
            (middle + 20, 145, "输出 tokens", usage.output_tokens, right - 8, TEXT),
            (middle + 20, 179, "其中推理 tokens", usage.reasoning_output_tokens, right - 8, MUTED),
            (middle + 20, 213, "合计 tokens", usage.total_tokens, right - 8, ACCENT),
        ]
        for x, y, label, number, value_x, color in entries:
            self.text(x, y, label, MUTED, 11)
            self.text(value_x, y, exact(number), color, 13, anchor="e", mono=True)
        scope = "会话累计" if self.mode == "total" else "最近一次模型调用"
        self.text(22, 255, f"{scope} · 缓存率 = 缓存命中 ÷ 输入", MUTED, 11)
        speed = self.snapshot.speed
        detail = (f"本轮最近调用：{speed_label(speed)} tok/s · {exact(speed.output_tokens)} 输出 / {speed.duration_ms / 1000:.2f} 秒 · 含推理"
                  if speed.tokens_per_second is not None else "生成速度：等待本轮调用结算及完整时间记录")
        self.text(22, 276, detail, ACCENT, 11)
        self.text(22, 300, "自动跟随主窗口 · 每秒检查用量 · 速度为最近调用平均值" if self.follow_enabled
                  else "每秒检查日志 · 用量在模型调用结算后更新 · 只读本地记录", MUTED, 10)

    def hover(self, event, text, clickable=False):
        self.hide_tooltip()
        self.canvas.configure(cursor="hand2" if clickable else "")
        self.tooltip_job = self.root.after(450, lambda: self.show_tooltip(event.x_root, event.y_root, text))

    def show_tooltip(self, x, y, text):
        self.tooltip_job = None
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        label = tk.Label(tip, text=text, bg=SURFACE, fg=TEXT, padx=12, pady=9,
                         justify="left", font=self.font(11), relief="solid", bd=1)
        label.pack()
        tip.update_idletasks()
        left, top, right, bottom = work_area(self.root)
        tip.geometry(f"+{max(left, min(x, right-tip.winfo_reqwidth()-8))}+{max(top, min(y+20, bottom-tip.winfo_reqheight()-8))}")
        self.tooltip = tip

    def hide_tooltip(self, _event=None):
        if self.tooltip_job:
            self.root.after_cancel(self.tooltip_job)
            self.tooltip_job = None
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None
        self.canvas.configure(cursor="")

    def start_reader(self):
        self.stop.set()
        self.stop = threading.Event()
        self.generation += 1
        generation, stop, path = self.generation, self.stop, self.path
        self.snapshot = Snapshot()
        interval = max(0.25, float(self.config.get("refresh_ms", 1000)) / 1000)

        def worker():
            tailer = LogTailer(path)
            while not stop.is_set():
                current = tailer.poll()
                copy = Snapshot(**vars(current))
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
                stop.wait(0.01 if current.loading else interval)

        self.worker = threading.Thread(target=worker, daemon=True, name="CodexLogReader")
        self.worker.start()

    def start_follow_reader(self):
        def worker():
            follower = DesktopSessionFollower(self.config["desktop_logs_root"], self.sessions_root,
                                              window_id=int(self.config.get("follow_window_id", 1)))
            while not self.follow_stop.is_set():
                try:
                    selection = follower.poll()
                except (OSError, ValueError) as exc:
                    selection = Selection(message=f"当前聊天识别暂不可用：{type(exc).__name__}")
                try:
                    self.follow_queue.put_nowait(selection)
                except queue.Full:
                    try:
                        self.follow_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.follow_queue.put_nowait(selection)
                    except queue.Full:
                        pass
                self.follow_stop.wait(0.5)

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
            self.snapshot = Snapshot(session_id=selection.session_id, loading=False, error=selection.message)
        return True

    def publish_status(self):
        data = {"running": not self.closed, "process_id": os.getpid(), "close_reason": self.close_reason,
                "auto_follow": self.follow_enabled,
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
                "error": self.snapshot.error, "loading": self.snapshot.loading}
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
        if self.close_signal is not None and self.close_signal.pending():
            self.close(reason="codex_closed")
            return
        changed = False
        latest_selection = None
        try:
            while True:
                latest_selection = self.follow_queue.get_nowait()
        except queue.Empty:
            pass
        if latest_selection is not None:
            changed = self.apply_follow_selection(latest_selection)
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
        self.publish_status()
        self.poll_job = self.root.after(150, self.tick)

    def toggle_mode(self):
        self.mode = "last" if self.mode == "total" else "total"
        self.hide_tooltip()
        self.render()
        self.save_settings()

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
        self.expanded = expanded
        height = self.px(self.EXPANDED_HEIGHT if expanded else self.BAR_HEIGHT)
        left, top, right, bottom = work_area(self.root)
        x, y = self.compact_position
        x = max(left, min(x, right - self.px(self.WIDTH)))
        # Prefer expanding down; only move up when needed to fit the work area.
        # Collapse always returns to the saved compact anchor, never the expanded bottom edge.
        y = max(top, min(y, bottom - height))
        self.root.geometry(f"{self.px(self.WIDTH)}x{height}+{x}+{y}")
        self.hide_tooltip()
        self.render()

    def drag_start(self, event):
        if "button" in self.canvas.gettags("current"):
            self.drag_origin = None
            return
        self.hide_tooltip()
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def drag_motion(self, event):
        if self.drag_origin:
            sx, sy, x, y = self.drag_origin
            self.root.geometry(f"+{x+event.x_root-sx}+{y+event.y_root-sy}")

    def drag_end(self, _event):
        if self.drag_origin:
            self.drag_origin = None
            self.root.update_idletasks()
            self.compact_position = (self.root.winfo_x(), self.root.winfo_y())
            self.save_settings()

    def context_menu(self, event):
        self.hide_tooltip()
        menu = tk.Menu(self.root, tearoff=False, bg=SURFACE, fg=TEXT, activebackground=BORDER,
                       activeforeground=TEXT, font=self.font(12))
        menu.add_command(label="展开明细" if not self.expanded else "收起明细", command=lambda: self.set_expanded(not self.expanded))
        menu.add_command(label="切换到最近调用" if self.mode == "total" else "切换到会话累计", command=self.toggle_mode)
        menu.add_command(label="自动跟随当前聊天" if self.follow_enabled else "跟踪指定日志", state="disabled")
        menu.add_command(label="取消置顶" if self.topmost else "保持置顶", command=self.toggle_topmost)
        menu.add_command(label="复制当前统计", command=self.copy_stats)
        menu.add_command(label="启动设置…", command=self.open_launcher)
        menu.add_separator()
        menu.add_command(label="退出", command=self.close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_launcher(self):
        import subprocess
        subprocess.Popen(app_command("--settings"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def copy_stats(self):
        usage = self.snapshot.total if self.mode == "total" else self.snapshot.last
        rate = "—" if usage.cache_rate is None else f"{usage.cache_rate:.2f}%"
        summary = (f"Codex {'会话累计' if self.mode == 'total' else '最近调用'}\n"
                   f"会话：{self.snapshot.session_id}\n输入：{exact(usage.input_tokens)} tokens\n"
                   f"缓存命中：{exact(usage.cached_input_tokens)} tokens（{rate}）\n"
                   f"输出：{exact(usage.output_tokens)} tokens\n合计：{exact(usage.total_tokens)} tokens\n"
                   f"本轮最近调用平均生成速度：{speed_label(self.snapshot.speed)} tok/s（本地记录估算）\n"
                   f"记录时间：{timestamp_label(self.snapshot.timestamp)}")
        self.root.clipboard_clear()
        self.root.clipboard_append(summary)

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
            return
        if not self.expanded:
            self.root.update_idletasks()
            self.compact_position = (self.root.winfo_x(), self.root.winfo_y())
        x, y = self.compact_position
        value = {"session": str(self.path), "mode": self.mode, "topmost": self.topmost,
                 "x": x, "y": y}
        temporary = self.state_dir / "settings.tmp"
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_dir / "settings.json")
        except OSError:
            pass

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
        self.hide_tooltip()
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
    close_signal = None
    try:
        close_signal = PanelCloseSignal()
        UsagePanel(root, config, state_dir, args.session, close_signal=close_signal)
        root.mainloop()
    finally:
        if close_signal is not None:
            close_signal.close()
        if mutex:
            kernel.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
