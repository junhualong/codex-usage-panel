"""Independent panel palettes, vector icon motion, and the Windows material adapter."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import math
import os
import sys


@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    surface: str
    border: str
    text: str
    muted: str
    hover: str
    selected: str
    accent: str
    success: str
    warning: str
    glass: bool = True


THEMES = {
    "classic": Theme("蓝灰经典", "#17222e", "#202f3e", "#405363", "#eef4fa", "#c1ceda",
                     "#334c60", "#29475e", "#9ed5ff", "#8ed6b2", "#f0c68b"),
    "obsidian": Theme("曜石灰", "#171b22", "#232a34", "#536070", "#f3f6fa", "#bbc6d2",
                      "#35404e", "#344353", "#cdd9e7", "#9bd7b8", "#f0c68b"),
    "frost": Theme("雾凇白", "#f3f6fa", "#e8eef4", "#9aaabb", "#172b3b", "#465d70",
                   "#dce7f2", "#d1e2f4", "#185a9d", "#216c4e", "#984009", False),
    "teal": Theme("松石青", "#102d30", "#1c3c3f", "#4a7372", "#e8f6f2", "#b9d5ce",
                  "#2c5052", "#2a5656", "#9be0d1", "#badf9c", "#f0c68b"),
    "nebula": Theme("星云紫", "#262235", "#352f47", "#716183", "#f5f0fc", "#d0c6df",
                    "#50425f", "#4d3c65", "#d6bdff", "#a5dcc9", "#f0c68b"),
    "sand": Theme("暖砂金", "#302a24", "#3f372e", "#7c6b56", "#fff4e5", "#decdb8",
                  "#574a3c", "#5a4935", "#eed1a0", "#b7d6b5", "#ffd18a"),
}
DEFAULT_THEME = "classic"
ICONS = {"terminal": "终端光标", "robot": "微型机器人", "pulse": "数据脉冲",
         "orbit": "轨道星核", "gear": "齿轮核心"}
DEFAULT_ICON = "robot"


def appearance_ids(theme, icon):
    """Old or damaged preferences fall back independently, never as a paired preset."""
    return (theme if isinstance(theme, str) and theme in THEMES else DEFAULT_THEME,
            icon if isinstance(icon, str) and icon in ICONS else DEFAULT_ICON)


def icon_pose(icon, state, elapsed, animate=True):
    """Only explicit task activity animates; idle robot blinking keeps its old behavior."""
    active = animate and state == "生成中"
    if icon == "robot":
        return robot_pose(state, elapsed, animate)[:2]
    if icon == "terminal":
        return (0.5 + 0.5 * math.cos(elapsed * math.tau / 1.8) if active else 1.0,)
    if icon == "pulse":
        return tuple(4 + 5 * math.sin(elapsed * 2.5 + index * .7) ** 2 for index in range(5)) if active else (3, 6, 9, 6, 3)
    if icon == "orbit":
        return ((elapsed % 6) * math.tau / 6 if active else 0.0,)
    if icon == "gear":
        return ((elapsed % 12) * math.tau / 12 if active else 0.0,)
    return ()


def acrylic_color(tint):
    """Windows expects AABBGGRR; opacity applies to the material, not foreground text."""
    return 0xD9000000 | int(tint[5:7] + tint[3:5] + tint[1:3], 16)


class AccentPolicy(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                ("color", wintypes.DWORD), ("animation", ctypes.c_int)]


class CompositionData(ctypes.Structure):
    _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]


class HighContrast(ctypes.Structure):
    _fields_ = [("size", wintypes.UINT), ("flags", wintypes.DWORD), ("scheme", wintypes.LPWSTR)]


def visual_preferences():
    """Read accessibility preferences without changing any Windows settings."""
    if os.name != "nt":
        return False, True
    glass, animation = True, wintypes.BOOL(True)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            glass = bool(winreg.QueryValueEx(key, "EnableTransparency")[0])
    except OSError:
        pass
    user = ctypes.windll.user32
    contrast = HighContrast(ctypes.sizeof(HighContrast), 0, None)
    if user.SystemParametersInfoW(0x0042, contrast.size, ctypes.byref(contrast), 0) and contrast.flags & 1:
        glass = False
    user.SystemParametersInfoW(0x1042, 0, ctypes.byref(animation), 0)  # SPI_GETCLIENTAREAANIMATION
    return glass, bool(animation.value)


def robot_pose(state, elapsed, animate=True):
    """Animation communicates task state, never token throughput."""
    if state == "读取失败":
        return 0.0, 3.0, "#f0c68b"
    if state in ("等待会话", "等待统计"):
        return 0.0, 2.0, "#b9cada"
    if state == "读取中":
        return 0.0, 3.0, "#94c7e5"
    offset = math.sin(elapsed * math.tau / 2.2) * 1.1 if animate and state == "生成中" else 0.0
    blink = animate and elapsed % 5.2 >= 5.02
    return offset, 0.7 if blink else 3.0, "#a8dfff"


class WindowMaterial:
    """Acrylic on the existing Tk HWND; no translucent foreground or extra windows.

    Tk's GDI Canvas cannot use a full-client DWM system backdrop reliably. The
    Windows accent API supports that surface, but is undocumented, so every call
    is checked and a solid, usable background remains the fallback.
    """
    def __init__(self, root):
        self.root = root
        self.enabled = False
        self.reason = "实色背景"
        self.hwnd = None
        self.user = self.gdi = None
        self.last_size = None
        if os.name != "nt" or sys.getwindowsversion().build < 17763:
            self.reason = "当前系统使用实色背景"
            return
        try:
            self.user = ctypes.WinDLL("user32", use_last_error=True)
            self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
            self.user.GetParent.argtypes = [wintypes.HWND]
            self.user.GetParent.restype = wintypes.HWND
            self.user.SetWindowCompositionAttribute.argtypes = [wintypes.HWND, ctypes.POINTER(CompositionData)]
            self.user.SetWindowCompositionAttribute.restype = wintypes.BOOL
            self.user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
            self.user.SetWindowRgn.restype = ctypes.c_int
            self.gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
            self.gdi.CreateRoundRectRgn.restype = wintypes.HRGN
            self.gdi.DeleteObject.argtypes = [wintypes.HANDLE]
            self.gdi.DeleteObject.restype = wintypes.BOOL
            self.hwnd = self.user.GetParent(root.winfo_id())
        except (AttributeError, OSError) as exc:
            self.reason = f"毛玻璃不可用，使用实色背景：{exc}"
            self.hwnd = None

    def apply(self, allowed, tint=THEMES[DEFAULT_THEME].background):
        self.enabled = False
        if not self.hwnd:
            return
        # Canvas black reveals the selected material; labels remain fully opaque.
        accent = AccentPolicy(4 if allowed else 0, 2, acrylic_color(tint), 0)
        data = CompositionData(19, ctypes.addressof(accent), ctypes.sizeof(accent))
        try:
            self.root.attributes("-transparentcolor", "")
            if not self.user.SetWindowCompositionAttribute(self.hwnd, ctypes.byref(data)):
                raise OSError(ctypes.get_last_error(), "Windows 未能启用背景材质")
            self.enabled = allowed
            self.reason = "Windows 原生毛玻璃" if allowed else "系统关闭透明效果或启用高对比度，使用实色背景"
        except (OSError, AttributeError) as exc:
            self.reason = f"毛玻璃不可用，使用实色背景：{exc}"
        self.last_size = None
        if not self.enabled:
            self.user.SetWindowRgn(self.hwnd, None, True)

    def resize(self, width, height, radius):
        if not self.enabled or self.last_size == (width, height, radius):
            return
        region = self.gdi.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if region:
            if self.user.SetWindowRgn(self.hwnd, region, True):
                # On success Windows owns the region; deleting it here would be invalid.
                self.last_size = (width, height, radius)
            else:
                self.gdi.DeleteObject(region)
