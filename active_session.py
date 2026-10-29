"""Follow the desktop's main-window route using local, read-only logs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import unquote, urlsplit


ROUTE_EVENT = "[electron-message-handler] IAB_LIFECYCLE received browser sidebar owner sync "
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
LOG_NAME = re.compile(r"codex-desktop-" + UUID + r"-(\d+)-t0-i\d+-(\d+)-(\d+)\.log$")
ROUTE_FIELD = re.compile(r'\bownerRoutePath=("(?:\\.|[^"\\])*"|\S+)')
ROUTE_LINE = re.compile(r"^(\S+) info " + re.escape(ROUTE_EVENT) + r"(.*)$")


@dataclass(frozen=True)
class Selection:
    session_id: str = ""
    path: str = ""
    route: str = ""
    message: str = "正在识别当前聊天"
    source_log: str = ""
    loading: bool = False
    error: str = ""


def parse_route_event(line, window_id=1):
    """Only owner-sync navigation counts; background replies/resumes never do."""
    header = ROUTE_LINE.match(line)
    if header is None:
        return None
    fields = header[2]
    window = re.search(r"\bwindowId=(\d+)\b", fields)
    if window is None or int(window[1]) != window_id:
        return None
    match = ROUTE_FIELD.search(fields)
    if match is None:
        return None
    value = match[1]
    try:
        route = json.loads(value) if value.startswith('"') else value
        if route in ("null", "undefined"):
            route = ""
        timestamp = datetime.fromisoformat(header[1].replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return timestamp, str(route)


def classify_route(route):
    path = unquote(urlsplit(route).path)
    local = re.fullmatch(r"/local/(" + UUID + r")/?", path)
    if local:
        return local[1].lower(), ""
    if path.startswith("/remote/") or path.startswith("/remote-control/"):
        return "", "当前为远程会话，无法读取本地用量"
    if path.startswith("/local/"):
        return "", "等待新会话建立"
    if path.startswith(("/c/", "/chat/", "/chatgpt/", "/cloud/")):
        return "", "当前聊天没有可读取的本地用量"
    return "", "当前页面未打开本地 Codex 会话"


def desktop_pid_is_alive(pid):
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code, size = wintypes.DWORD(), wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return False
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        executable = buffer.value.lower()
        return (Path(executable).name in ("chatgpt.exe", "codex.exe")
                and "\\app\\" in executable and "codex" in executable)
    finally:
        kernel.CloseHandle(handle)


def foreground_pid():
    if os.name != "nt":
        return None
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    process = wintypes.DWORD()
    window = user.GetForegroundWindow()
    if window:
        user.GetWindowThreadProcessId(window, ctypes.byref(process))
    return process.value or None


def main_window_pid():
    if os.name != "nt":
        return None
    # Reuse the watcher's visible main-window check. Import lazily because the
    # watcher also uses desktop_pid_is_alive from this module.
    from codex_autostart import codex_window
    window = codex_window()
    return window[0] if window else None


def numeric_directories(parent):
    try:
        return [p for p in parent.iterdir() if p.name.isdigit() and p.is_dir()
                and not p.is_symlink() and not p.is_junction()]
    except OSError:
        return []


def dated_directories(root):
    return sorted((day for year in numeric_directories(root)
                   for month in numeric_directories(year)
                   for day in numeric_directories(month)), reverse=True)


class RouteLog:
    def __init__(self, path, window_id):
        self.path, self.window_id = path, window_id
        self.offset, self.pending, self.identity = 0, b"", None
        self.latest = None

    def poll(self):
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if stat.st_size < self.offset or (self.identity is not None and identity != self.identity):
                    self.offset, self.pending, self.latest = 0, b"", None
                self.identity = identity
                stream.seek(self.offset)
                chunk = stream.read(4 * 1024 * 1024)
                self.offset = stream.tell()
                lines = (self.pending + chunk).split(b"\n")
                self.pending = lines.pop()
                # Desktop log lines are bounded; avoid retaining huge unrelated output.
                if len(self.pending) > 2 * 1024 * 1024:
                    self.pending = b""
                for raw in lines:
                    if b"IAB_LIFECYCLE received browser sidebar owner sync " not in raw:
                        continue
                    event = parse_route_event(raw.decode("utf-8", "replace"), self.window_id)
                    if event is not None and (self.latest is None or event[0] >= self.latest[0]):
                        self.latest = event
                return self.latest, self.offset < stat.st_size, ""
        except OSError as exc:
            return self.latest, False, f"导航日志读取失败：{exc.strerror or type(exc).__name__}"


class SessionResolver:
    def __init__(self, sessions_root):
        self.root = Path(sessions_root)
        self.cache = {}
        self.databases = sorted(self.root.parent.glob("state_*.sqlite"),
                                key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else 0,
                                reverse=True)

    def resolve(self, session_id):
        cached = self.cache.get(session_id)
        if cached and cached.is_file():
            return str(cached)
        for database in self.databases[:1]:
            connection = None
            try:
                connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.15)
                row = connection.execute("SELECT rollout_path FROM threads WHERE id = ?", (session_id,)).fetchone()
                if row and row[0]:
                    value = row[0]
                    if value.startswith("\\\\?\\"):
                        value = value[4:]
                    path = Path(value)
                    if path.is_file():
                        self.cache[session_id] = path
                        return str(path)
            except (sqlite3.Error, OSError):
                pass
            finally:
                if connection:
                    connection.close()
        # UUIDv7 encodes creation time; use it to find older logs without a full scan.
        candidates = []
        try:
            created = datetime.fromtimestamp(int(session_id.replace("-", "")[:12], 16) / 1000, timezone.utc)
            candidates = [self.root / (created + timedelta(days=delta)).strftime("%Y/%m/%d") for delta in (0, -1, 1)]
        except (ValueError, OverflowError, OSError):
            pass
        candidates.extend(dated_directories(self.root)[:3])
        for directory in dict.fromkeys(candidates):
            if directory.is_symlink() or directory.is_junction():
                continue
            for path in directory.glob(f"rollout-*-{session_id}.jsonl"):
                if path.is_file() and not path.is_symlink():
                    self.cache[session_id] = path
                    return str(path)
        return ""


class DesktopSessionFollower:
    def __init__(self, log_root, sessions_root, window_id=1, alive=desktop_pid_is_alive,
                 foreground=foreground_pid, resolver=None, main_window=main_window_pid):
        self.log_root = Path(log_root)
        self.window_id = window_id
        self.alive, self.foreground = alive, foreground
        self.main_window = main_window
        self.resolver = resolver or SessionResolver(sessions_root)
        self.pid = None
        self.files = {}
        self.last_discovery = -float("inf")
        self.state = Selection()

    def discover(self):
        candidates, live = [], {}
        for day in dated_directories(self.log_root)[:4]:
            for path in day.glob("codex-desktop-*-t0-*.log"):
                match = LOG_NAME.fullmatch(path.name)
                if not match or path.is_symlink():
                    continue
                pid = int(match[1])
                if pid not in live:
                    live[pid] = self.alive(pid)
                if live[pid]:
                    candidates.append((path, pid, (str(day), match[2], int(match[3]))))
        if not candidates:
            self.pid, self.files = None, {}
            return
        focused = self.foreground()
        pids = {entry[1] for entry in candidates}
        visible = self.main_window() if focused not in pids else None
        # Utility processes share the executable and log naming scheme, but do
        # not own navigation. Restarting under another app must select the UI.
        chosen = (focused if focused in pids else visible if visible in pids else
                  self.pid if self.pid in pids else max(candidates, key=lambda x: x[2])[1])
        if chosen != self.pid:
            self.files = {}
        self.pid = chosen
        paths = [entry[0] for entry in sorted(candidates, key=lambda x: x[2]) if entry[1] == chosen]
        self.files = {path: self.files.get(path) or RouteLog(path, self.window_id) for path in paths}

    def poll(self):
        now = time.monotonic()
        if now - self.last_discovery >= 1:
            self.discover()
            self.last_discovery = now
        if not self.files:
            self.state = Selection(message="等待 Codex 主窗口打开")
            return self.state
        events, loading, errors = [], False, []
        for path, reader in self.files.items():
            event, pending, error = reader.poll()
            loading |= pending
            if error:
                errors.append(error)
            if event:
                events.append((event[0], event[1], str(path)))
        if errors:
            return Selection(error="；".join(dict.fromkeys(errors)))
        if loading:
            return Selection(message="正在读取界面导航记录", loading=True)
        if not events:
            return Selection(message="等待当前聊天的导航记录")
        _, route, source_log = max(events, key=lambda item: item[0])
        session_id, message = classify_route(route)
        path = self.resolver.resolve(session_id) if session_id else ""
        if session_id and not path:
            message = "已跟随当前聊天，等待本地用量日志"
        self.state = Selection(session_id, path, route, message, source_log)
        return self.state
