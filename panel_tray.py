"""Native notification-area entry owned by the existing watcher process."""
import ctypes
from ctypes import wintypes
import os
import struct
import threading
import time

import win32api
import win32con
import win32gui


class TrayIcon:
    CALLBACK = win32con.WM_APP + 1
    UPDATE = win32con.WM_APP + 2
    ACTIONS = {1001: "show", 1002: "hide", 1003: "recenter", 1004: "diagnostics", 1005: "settings", 1006: "exit"}

    def __init__(self, action):
        self.action = action
        self.hwnd = self.icon = None
        self.error = ""
        self.present = False
        self.stopping = False
        self.state = {}
        self.retry_at = 0
        self.tip = "Codex 用量面板 · 后台待命"
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="CodexPanelTray")
        self.thread.start()
        self.ready.wait(2)

    @staticmethod
    def create_icon():
        # Original usage-chart artwork, built in memory; no installation-directory writes.
        size = 32
        pixels = bytearray()
        for y in range(size-1, -1, -1):
            for x in range(size):
                color = (28, 30, 36, 255)
                if 6 <= x < 11 and 17 <= y < 26:
                    color = (142, 214, 178, 255)
                elif 14 <= x < 19 and 11 <= y < 26:
                    color = (158, 213, 255, 255)
                elif 22 <= x < 27 and 6 <= y < 26:
                    color = (158, 213, 255, 255)
                r, g, b, a = color
                pixels.extend((b, g, r, a))
        dib = struct.pack("<IIIHHIIIIII", 40, size, size*2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
        dib += pixels + bytes(4*size)
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.CreateIconFromResourceEx.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                                 wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.CreateIconFromResourceEx.restype = wintypes.HICON
        buffer = ctypes.create_string_buffer(dib)
        icon = user.CreateIconFromResourceEx(buffer, len(dib), True, 0x00030000, size, size, 0)
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return icon

    def notify(self, add=False):
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        # NIF_SHOWTIP preserves the standard tooltip under NOTIFYICON_VERSION_4.
        data = (self.hwnd, 1, flags | 0x80, self.CALLBACK, self.icon, self.tip[:127])
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD if add else win32gui.NIM_MODIFY, data)
        if add:
            # The timeout member shares storage with uVersion in NOTIFYICONDATA.
            win32gui.Shell_NotifyIcon(win32gui.NIM_SETVERSION, (self.hwnd, 1, 0, 0, 0, "", "", 4))
        self.present, self.error = True, ""

    def set_state(self, state):
        if state == self.state and (self.present or time.monotonic() < self.retry_at):
            return
        self.state = dict(state)
        label = "等待 Codex 打开" if not state.get("codex_open") else (
            "面板已隐藏" if state.get("hidden") and state.get("panel_running") else
            "面板运行中" if state.get("panel_running") else "面板已关闭")
        self.tip = "Codex 用量面板 · " + label
        if self.hwnd:
            self.retry_at = time.monotonic() + 5
            win32gui.PostMessage(self.hwnd, self.UPDATE, 0, 0)

    def menu_items(self):
        opened = bool(self.state.get("codex_open"))
        running = bool(self.state.get("panel_running"))
        return [(1001, "显示面板", opened),
                (1002, "隐藏面板", running and not self.state.get("hidden")),
                (1003, "找回面板到当前屏幕", opened),
                (1004, "运行状态与诊断…", True), (1005, "启动设置…", True),
                (1006, "退出面板与监听", True)]

    def popup(self):
        menu = win32gui.CreatePopupMenu()
        try:
            for identifier, label, enabled in self.menu_items():
                if identifier in (1004, 1006):
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                win32gui.AppendMenu(menu, win32con.MF_STRING | (0 if enabled else win32con.MF_GRAYED), identifier, label)
            win32gui.SetForegroundWindow(self.hwnd)
            x, y = win32gui.GetCursorPos()
            choice = win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON | win32con.TPM_RETURNCMD,
                                             x, y, 0, self.hwnd, None)
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
            if choice in self.ACTIONS:
                self.action(self.ACTIONS[choice])
        finally:
            win32gui.DestroyMenu(menu)

    def window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.taskbar_created:
                self.present = False
                self.notify(add=True)
            elif message == self.UPDATE:
                self.notify(add=not self.present)
            elif message == self.CALLBACK:
                event = lparam & 0xffff
                if event in (win32con.WM_CONTEXTMENU, win32con.WM_RBUTTONUP):
                    self.popup()
                elif event in (0x400, 0x401):  # NIN_SELECT / NIN_KEYSELECT
                    self.action("show" if self.state.get("codex_open") else "settings")
            elif message == win32con.WM_CLOSE:
                win32gui.DestroyWindow(hwnd)
            elif message == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
            else:
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
        except Exception as exc:
            self.error = str(exc)
            if message in (self.UPDATE, self.taskbar_created):
                self.present = False
        return 0

    def run(self):
        name = f"CodexUsageTray-{os.getpid()}-{id(self)}"
        instance = win32api.GetModuleHandle(None)
        registered = False
        try:
            self.taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            wc = win32gui.WNDCLASS()
            wc.hInstance, wc.lpszClassName, wc.lpfnWndProc = instance, name, self.window_proc
            win32gui.RegisterClass(wc)
            registered = True
            self.hwnd = win32gui.CreateWindow(name, "Codex 用量面板后台", 0, 0, 0, 0, 0, 0, 0, instance, None)
            self.icon = self.create_icon()
            try:
                self.notify(add=True)
            except Exception as exc:
                # Explorer may not be ready at login; keep listening for TaskbarCreated.
                self.error = str(exc)
            self.ready.set()
            if not self.stopping:
                win32gui.PumpMessages()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ready.set()
            if self.hwnd:
                try:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 1))
                    if win32gui.IsWindow(self.hwnd):
                        win32gui.DestroyWindow(self.hwnd)
                except win32gui.error:
                    pass
            self.present, self.hwnd = False, None
            if self.icon:
                win32gui.DestroyIcon(self.icon)
                self.icon = None
            if registered:
                win32gui.UnregisterClass(name, instance)

    def close(self):
        self.stopping = True
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        self.thread.join(2)

