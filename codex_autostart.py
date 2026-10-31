"""Per-user watcher: start and gracefully close the panel with the Codex window."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import time
import traceback
import winreg

from active_session import desktop_pid_is_alive
from app_runtime import app_directory, app_command, instance_suffix, load_config, profile_arguments, profile_directory, pythonw


BASE = app_directory(__file__)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SUFFIX = instance_suffix(BASE)
RUN_NAME = "CodexUsagePanelAutoStart" + ("-" + SUFFIX if profile_directory() else "")
MUTEX_NAME = "Local\\CodexUsageWatcher-" + SUFFIX
STOP_NAME = "Local\\CodexUsageWatcherStop-" + SUFFIX
PANEL_MUTEX_NAME = "Local\\CodexUsagePanel-" + SUFFIX
PANEL_CLOSE_NAME = "Local\\CodexUsagePanelClose-" + SUFFIX


def startup_command():
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline(app_command("--start", BASE))
    # Keep existing source installations' registration valid during upgrade.
    return subprocess.list2cmdline([str(pythonw()), "-B", "-X", "utf8",
                                   str(BASE / "codex_autostart.py"), *profile_arguments()])


def create_independent_process(command):
    """Local WMI owns this process; no external shell or PowerShell is required."""
    import pythoncom
    from win32com.client import dynamic

    pythoncom.CoInitialize()
    locator = service = startup = process = parameters = result = None
    try:
        locator = dynamic.Dispatch("WbemScripting.SWbemLocator")
        service = locator.ConnectServer(".", r"root\cimv2")
        startup = service.Get("Win32_ProcessStartup").SpawnInstance_()
        startup.Properties_.Item("ShowWindow").Value = 0
        process = service.Get("Win32_Process")
        parameters = process.Methods_.Item("Create").InParameters.SpawnInstance_()
        parameters.Properties_.Item("CommandLine").Value = subprocess.list2cmdline(command)
        parameters.Properties_.Item("CurrentDirectory").Value = str(BASE)
        parameters.Properties_.Item("ProcessStartupInformation").Value = startup
        result = process.ExecMethod_("Create", parameters)
        code = int(result.Properties_.Item("ReturnValue").Value)
        if code:
            raise RuntimeError(f"Windows WMI 无法启动后台监听，错误码：{code}")
        return int(result.Properties_.Item("ProcessId").Value)
    finally:
        # Release COM wrappers while this thread's apartment is still alive.
        result = parameters = process = startup = service = locator = None
        pythoncom.CoUninitialize()


def process_in_job(pid=None):
    kernel = kernel_api()
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    handle = kernel.OpenProcess(0x1000, False, pid or os.getpid())
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        inside = wintypes.BOOL()
        if not kernel.IsProcessInJob(handle, None, ctypes.byref(inside)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(inside.value)
    finally:
        kernel.CloseHandle(handle)


def start_independent():
    """WMI owns the watcher, so closing Codex cannot terminate its process tree."""
    if panel_is_running(MUTEX_NAME):
        return {"already_running": True}
    config = load_config(BASE)
    started = {"watcher_process_id": create_independent_process(app_command("--watch", BASE)), "launcher": "WMI"}
    status_file = Path(config["state_dir"]) / "autostart-status.json"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        if status.get("running") and status.get("process_in_job") is False and panel_is_running(MUTEX_NAME):
            actual_pid = status["watcher_process_id"]
            try:
                independent = not process_in_job(actual_pid)
            except OSError:
                # A previous crash can leave a ready file while the new watcher
                # has acquired its mutex but has not published its first status.
                independent = False
            if independent:
                return {**started, "watcher_process_id": actual_pid, "ready": True, "process_in_job": False,
                        "already_running": actual_pid != started["watcher_process_id"]}
        time.sleep(0.1)
    raise RuntimeError("Independent watcher did not report ready within 10 seconds")


def _startup_registry(method, **values):
    """Use Windows' registry view, not a packaged caller's private HKCU view."""
    import pythoncom
    from win32com.client import dynamic

    pythoncom.CoInitialize()
    locator = service = registry = parameters = result = None
    try:
        locator = dynamic.Dispatch("WbemScripting.SWbemLocator")
        service = locator.ConnectServer(".", r"root\default")
        # Local impersonation keeps the operation scoped to the current user.
        service.Security_.ImpersonationLevel = 3
        registry = service.Get("StdRegProv")
        parameters = registry.Methods_.Item(method).InParameters.SpawnInstance_()
        for name, value in dict(hDefKey=0x80000001, sSubKeyName=RUN_KEY, **values).items():
            parameters.Properties_.Item(name).Value = value
        result = registry.ExecMethod_(method, parameters)
        code = int(result.Properties_.Item("ReturnValue").Value)
        if method == "EnumValues" and code == 2:  # A new user may not have a Run key yet.
            return {"sNames": (), "Types": ()}
        if code:
            raise OSError(code, f"Windows 登录启动项 {method} 失败：{ctypes.FormatError(code).strip()}")
        return {item.Name: item.Value for item in result.Properties_ if item.Name != "ReturnValue"}
    finally:
        result = parameters = registry = service = locator = None
        pythoncom.CoUninitialize()


def registration():
    values = _startup_registry("EnumValues")
    for name, kind in zip(values.get("sNames") or (), values.get("Types") or ()):
        if name.casefold() == RUN_NAME.casefold():
            # Enumerate first: GetStringValue uses a generic failure for a missing value.
            if kind != winreg.REG_SZ:
                raise RuntimeError("登录启动项类型不符；保留原记录，请检查是否来自其他安装")
            return _startup_registry("GetStringValue", sValueName=RUN_NAME)["sValue"]
    return None


def install():
    command = startup_command()
    existing = registration()
    if existing is not None and existing != command:
        raise RuntimeError("The startup entry already belongs to a different installation")
    _startup_registry("CreateKey")
    _startup_registry("SetStringValue", sValueName=RUN_NAME, sValue=command)
    if registration() != command:
        raise RuntimeError("Windows 登录启动项写入后校验失败，请重新启用自动启动")
    return {"installed": True, "scope": "current-user", "name": RUN_NAME, "command": command}


def kernel_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


class PanelCloseSignal:
    """The singleton panel owns this event until its Tk loop has exited."""
    def __init__(self, name=PANEL_CLOSE_NAME):
        self.kernel = kernel_api()
        self.handle = self.kernel.CreateEventW(None, True, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.kernel.ResetEvent(self.handle)

    def pending(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def request_panel_close(name=PANEL_CLOSE_NAME):
    kernel = kernel_api()
    handle = kernel.OpenEventW(0x0002, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:  # The panel has not started, or has already exited.
            return False
        raise ctypes.WinError(error)
    try:
        if not kernel.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        kernel.CloseHandle(handle)


def panel_is_running(name=PANEL_MUTEX_NAME):
    kernel = kernel_api()
    handle = kernel.OpenMutexW(0x00100000, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:
            return False
        raise ctypes.WinError(error)
    kernel.CloseHandle(handle)
    return True


def stop_watcher():
    kernel = kernel_api()
    handle = kernel.OpenEventW(0x0002, False, STOP_NAME)
    if not handle:
        return False
    try:
        return bool(kernel.SetEvent(handle))
    finally:
        kernel.CloseHandle(handle)


def unregister():
    existing = registration()
    if existing is not None:
        if existing != startup_command():
            raise RuntimeError("Startup entry changed; refusing to remove a different installation")
        _startup_registry("DeleteValue", sValueName=RUN_NAME)
    if registration() is not None:
        raise RuntimeError("Windows 登录启动项移除后校验失败，请重试")
    return {"disabled": True}


def disable():
    return {**unregister(), "watcher_signaled": stop_watcher()}


def codex_window(preferred=None):
    """Read visible top-level windows; exclude CLI and editor-extension processes."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    windows, checked = [], {}

    @callback_type
    def visit(window, _data):
        if not user.IsWindowVisible(window) or user.GetWindow(window, 4):
            return True
        process = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(process))
        pid = process.value
        if pid not in checked:
            checked[pid] = desktop_pid_is_alive(pid)
        if checked[pid]:
            title = ctypes.create_unicode_buffer(512)
            user.GetWindowTextW(window, title, len(title))
            if title.value.strip().casefold() in ("codex", "chatgpt"):
                windows.append((pid, int(window)))
        return True

    if not user.EnumWindows(visit, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return preferred if preferred in windows else min(windows, default=None)


class LaunchGate:
    """Launch once per main window, respecting a manually dismissed panel."""
    def __init__(self):
        self.identity = None
        self.launched = False
        self.attempts = 0
        self.retry_at = 0.0

    def step(self, identity, now, launch):
        if identity != self.identity:
            self.identity, self.launched, self.attempts, self.retry_at = identity, False, 0, 0.0
        if identity is None or self.launched or self.attempts >= 3 or now < self.retry_at:
            return None
        self.attempts += 1
        try:
            pid = launch()
        except OSError as exc:
            self.retry_at = now + 5
            return {"event": "launch_failed", "error": str(exc), "attempt": self.attempts}
        self.launched = True
        return {"event": "panel_started", "panel_process_id": pid, "codex_process_id": identity[0],
                "codex_window": identity[1]}


class LifecycleGate(LaunchGate):
    def __init__(self):
        super().__init__()
        self.closing = False
        self.close_sent = False

    def step(self, identity, now, launch, running, request_close):
        if identity is None or self.closing:
            if identity is None:
                super().step(None, now, launch)
            if running():
                self.closing = True
                # A newly spawned Python process may not have created its event yet.
                requested = request_close()
                if requested and not self.close_sent:
                    self.close_sent = True
                    return {"event": "panel_close_requested", "reason": "codex_closed"}
                return None
            self.closing = self.close_sent = False
            if identity is None:
                return None
        # Wait for the old process/mutex to disappear before a rapid reopen.
        return super().step(identity, now, launch)


def launch_panel():
    child = subprocess.Popen(app_command("--panel", BASE),
                             cwd=BASE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    return child


def watch():
    kernel = kernel_api()
    ctypes.set_last_error(0)
    mutex = kernel.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        return
    stop = kernel.CreateEventW(None, True, False, STOP_NAME)
    if not stop:
        kernel.CloseHandle(mutex)
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.ResetEvent(stop)
    config = load_config(BASE)
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    lifetime = {"parent_process_id": os.getppid(), "process_in_job": process_in_job()}
    gate, previous, last_launch, last_close = LifecycleGate(), None, None, None
    child = None
    from panel_control import request as panel_request
    from panel_tray import TrayIcon
    commands = queue.Queue()
    wake = kernel.CreateEventW(None, False, False, None)
    if not wake:
        kernel.CloseHandle(stop)
        kernel.CloseHandle(mutex)
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
    kernel.WaitForMultipleObjects.restype = wintypes.DWORD
    handles = (wintypes.HANDLE * 2)(stop, wake)

    def tray_action(action):
        commands.put(action)
        kernel.SetEvent(wake)

    tray = None
    tray_error = ""
    pending_action = None
    action_deadline = 0
    exiting = False
    exit_deadline = 0
    next_scan = 0

    def launch():
        nonlocal child
        child = launch_panel()
        return child.pid

    def running():
        # Include a child still initializing, and an instance started manually.
        return (child is not None and child.poll() is None) or panel_is_running()

    def publish(status):
        nonlocal previous
        status = {**status, **lifetime, "tray_available": bool(tray and tray.present),
                  "tray_error": tray_error or (tray.error if tray else "")}
        if status == previous:
            return
        temporary = state / "autostart-status.tmp"
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(state / "autostart-status.json")
        previous = status

    try:
        try:
            tray = TrayIcon(tray_action)
        except Exception as exc:
            tray_error = str(exc)
        while kernel.WaitForSingleObject(stop, 0) == 258:
            try:
                while not commands.empty():
                    action = commands.get_nowait()
                    if action == "exit":
                        exiting = True
                        exit_deadline = time.monotonic() + 10
                        pending_action = None
                    elif action in ("settings", "diagnostics"):
                        subprocess.Popen(app_command("--" + action, BASE), cwd=BASE,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                    elif not exiting and action in ("show", "hide", "recenter"):
                        if action == "hide":
                            pending_action = None
                            panel_request(action)
                        else:
                            pending_action, action_deadline = action, time.monotonic() + 10
                            next_scan = 0
                if exiting:
                    # Retry while a newly launched panel is still creating its IPC event.
                    if running():
                        request_panel_close()
                        if time.monotonic() >= exit_deadline:
                            exiting = False
                            tray_error = "面板尚未确认退出，请打开运行状态检查后重试"
                    else:
                        break
                now = time.monotonic()
                if now >= next_scan:
                    next_scan = now + 1
                    identity = codex_window(gate.identity)
                    event = gate.step(identity, now, launch, running, request_panel_close) if not exiting else None
                    if event:
                        if event["event"] == "panel_close_requested":
                            last_close = dict(event, time=time.time())
                        else:
                            last_launch = dict(event, time=time.time())
                if pending_action:
                    if gate.identity is None:
                        pending_action = None
                    elif not gate.closing:
                        if not running():
                            pid = launch()
                            gate.launched = True
                            last_launch = {"event": "panel_started", "panel_process_id": pid, "source": "tray", "time": time.time()}
                        if panel_request(pending_action):
                            pending_action = None
                            tray_error = ""
                    if pending_action and now >= action_deadline:
                        tray_error = "面板控制入口尚未就绪，请通过启动设置重试"
                        pending_action = None
                if tray:
                    try:
                        panel_status = json.loads((state / "status.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        panel_status = {}
                    if child and child.pid != panel_status.get("process_id"):
                        panel_status = {}
                    tray.set_state({"codex_open": bool(gate.identity), "panel_running": running(),
                                    "hidden": panel_status.get("hidden", False)})
                publish({"running": True, "watcher_process_id": os.getpid(), "codex_window": gate.identity,
                         "panel_launched_for_window": gate.launched, "panel_closing": gate.closing,
                         "last_launch": last_launch, "last_close": last_close})
            except OSError as exc:
                publish({"running": True, "watcher_process_id": os.getpid(), "error": str(exc)})
            kernel.WaitForMultipleObjects(2, handles, False, 100 if pending_action or exiting else 1000)
    finally:
        if tray:
            tray.close()
        publish({"running": False, "watcher_process_id": os.getpid(),
                 "last_launch": last_launch, "last_close": last_close})
        kernel.CloseHandle(stop)
        kernel.CloseHandle(wake)
        kernel.CloseHandle(mutex)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--disable", action="store_true")
    modes.add_argument("--stop", action="store_true")
    modes.add_argument("--inspect", action="store_true")
    modes.add_argument("--watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.install:
        result = install()
    elif args.disable:
        result = disable()
    elif args.stop:
        result = {"watcher_signaled": stop_watcher()}
    elif args.inspect:
        result = {"registered_command": registration(), "current_codex_window": codex_window(),
                  "watcher_running": panel_is_running(MUTEX_NAME)}
    elif args.watch:
        watch()
        return
    else:
        result = start_independent()
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        config = load_config(BASE)
        state = Path(config["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        with (state / "autostart-error.log").open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("\n%Y-%m-%d %H:%M:%S ") + "watcher startup/runtime failure\n")
            traceback.print_exc(file=stream)
        if sys.stderr is not None:
            traceback.print_exc()
        raise SystemExit(1)
