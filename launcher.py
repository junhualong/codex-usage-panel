"""Universal entry point for the portable executable and Python source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback

from app_runtime import VERSION, app_command, app_directory, config_path, ensure_config, load_config
import codex_autostart as auto


def shutdown():
    """Stop our watcher first so it cannot race a panel-close request."""
    auto.stop_watcher()
    deadline = time.monotonic() + 8
    while auto.panel_is_running(auto.MUTEX_NAME) and time.monotonic() < deadline:
        time.sleep(0.1)
    if auto.panel_is_running(auto.MUTEX_NAME):
        raise RuntimeError("后台监听尚未退出，请稍后重试")
    while time.monotonic() < deadline:
        auto.request_panel_close()
        if not auto.panel_is_running():
            return {"stopped": True}
        time.sleep(0.1)
    raise RuntimeError("面板尚未退出，请稍后重试")


def inspect():
    config = load_config()
    return {
        "version": VERSION, "frozen": bool(getattr(sys, "frozen", False)),
        "app_dir": str(app_directory()), "config_path": str(config_path()),
        "state_dir": config["state_dir"],
        "sessions_root": config["sessions_root"], "sessions_found": Path(config["sessions_root"]).is_dir(),
        "desktop_logs_root": config["desktop_logs_root"], "logs_found": Path(config["desktop_logs_root"]).is_dir(),
        "registered_command": auto.registration(), "startup_command": auto.startup_command(),
        "startup_entry_name": auto.RUN_NAME,
        "current_codex_window": auto.codex_window(),
        "watcher_running": auto.panel_is_running(auto.MUTEX_NAME), "panel_running": auto.panel_is_running(),
    }


def create_shortcut():
    import pythoncom
    from win32com.client import dynamic

    pythoncom.CoInitialize()
    shell = shortcut = old = None
    try:
        shell = dynamic.Dispatch("WScript.Shell")
        desktop = Path(shell.SpecialFolders("Desktop"))
        path = desktop / "Codex 用量面板.lnk"
        command = app_command("--settings")
        if path.exists():
            old = shell.CreateShortcut(str(path))
            if (os.path.normcase(old.TargetPath) != os.path.normcase(command[0])
                    or old.Arguments != subprocess.list2cmdline(command[1:])):
                raise RuntimeError(f"桌面已有其他安装的同名快捷方式，请先保留或重命名：{path}")
        shortcut = shell.CreateShortcut(str(path))
        shortcut.TargetPath = command[0]
        shortcut.Arguments = subprocess.list2cmdline(command[1:])
        shortcut.WorkingDirectory = str(app_directory())
        shortcut.Description = "Codex 当前聊天的 token、缓存命中率与生成速度"
        shortcut.Save()
        return {"shortcut": str(path)}
    finally:
        old = shortcut = shell = None
        pythoncom.CoUninitialize()


def log_error():
    try:
        state = Path(load_config()["state_dir"])
    except Exception:
        state = config_path().parent
    state.mkdir(parents=True, exist_ok=True)
    path = state / "launcher-error.log"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n"))
        traceback.print_exc(file=stream)
    return path


def settings_window():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from usage_panel import enable_dpi

    config = ensure_config()
    enable_dpi()
    root = tk.Tk()
    root.title("Codex 用量面板 · 启动设置")
    app_icon = app_directory() / "panel.ico"
    if app_icon.is_file():
        root.iconbitmap(default=str(app_icon))
    root.resizable(False, False)
    root.configure(bg="#1c1e24")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#1c1e24")
    style.configure("TLabel", background="#1c1e24", foreground="#e5e9f1", font=("Microsoft YaHei UI", 10))
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
    style.configure("Muted.TLabel", foreground="#929ba9")
    style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8),
                    background="#2b303c", foreground="#e5e9f1", bordercolor="#383c47")
    style.map("TButton", background=[("active", "#383e4d"), ("disabled", "#24272f")],
              foreground=[("disabled", "#929ba9")])
    style.configure("Primary.TButton", background="#9ed5ff", foreground="#17232f")
    style.map("Primary.TButton", background=[("active", "#c0e3ff"), ("disabled", "#384b59")],
              foreground=[("disabled", "#929ba9")])
    style.configure("TCheckbutton", background="#1c1e24", foreground="#e5e9f1", font=("Microsoft YaHei UI", 10))
    style.map("TCheckbutton", background=[("active", "#24272f")], foreground=[("disabled", "#929ba9")])
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Codex 用量面板", style="Title.TLabel").pack(anchor="w")
    ttk.Label(frame, text=f"v{VERSION}  ·  当前聊天自动跟随  ·  本地统计", style="Muted.TLabel").pack(anchor="w", pady=(8, 18))
    status = tk.StringVar(value="正在启动后台监听…")
    ttk.Label(frame, textvariable=status, wraplength=500).pack(anchor="w", pady=(0, 12))
    results = queue.Queue()
    busy = False
    startup = tk.BooleanVar(value=auto.registration() == auto.startup_command())

    def run(operation, message):
        nonlocal busy
        if busy:
            return
        busy = True
        status.set(message)
        for button in buttons:
            button.configure(state="disabled")
        check.configure(state="disabled")

        def worker():
            try:
                results.put((operation(), None))
            except Exception as exc:
                path = log_error()
                results.put((None, f"{exc}\n\n错误记录：{path}"))
        threading.Thread(target=worker, daemon=True).start()

    def show_panel():
        result = auto.start_independent()
        if auto.codex_window() is not None:
            # WMI keeps even a manually reopened panel outside Codex's job.
            # The panel mutex also handles a simultaneous watcher launch.
            auto.create_independent_process(app_command("--panel"))
        return result

    def toggle_startup():
        enabled = startup.get()
        def operation():
            if enabled:
                auto.install()
                return auto.start_independent()
            return auto.unregister()
        run(operation, "正在更新自动启动设置…")

    check = ttk.Checkbutton(frame, text="登录 Windows 后自动待命，随 Codex 打开和关闭面板",
                            variable=startup, command=toggle_startup)
    check.pack(anchor="w", pady=(0, 12))
    ttk.Label(frame, text="首次使用无需填写路径。请先把整个程序文件夹放在固定位置。\n关闭此设置窗口后，后台监听继续运行。", style="Muted.TLabel").pack(anchor="w", pady=(0, 16))
    row = ttk.Frame(frame)
    row.pack(fill="x")
    show = ttk.Button(row, text="启动 / 显示面板", style="Primary.TButton", command=lambda: run(show_panel, "正在启动面板…"))
    show.pack(side="left", padx=(0, 8))
    shortcut = ttk.Button(row, text="创建桌面快捷方式", command=lambda: run(create_shortcut, "正在创建快捷方式…"))
    shortcut.pack(side="left")
    row2 = ttk.Frame(frame)
    row2.pack(fill="x", pady=(10, 0))
    folder = ttk.Button(row2, text="打开配置目录", command=lambda: os.startfile(str(config_path().parent)))
    folder.pack(side="left", padx=(0, 8))
    stop = ttk.Button(row2, text="停止面板与监听", command=lambda: run(shutdown, "正在停止…"))
    stop.pack(side="left")
    buttons = (show, shortcut, folder, stop)
    ttk.Label(frame, text="缓存命中率 = 缓存输入 / 总输入\n≈ tok/s 为最近已结算调用的平均速度", style="Muted.TLabel").pack(anchor="w", pady=(18, 0))

    def update():
        nonlocal busy
        try:
            result, error = results.get_nowait()
        except queue.Empty:
            pass
        else:
            busy = False
            for button in buttons:
                button.configure(state="normal")
            check.configure(state="normal")
            startup.set(auto.registration() == auto.startup_command())
            if error:
                messagebox.showerror("启动失败", error, parent=root)
            elif result and result.get("shortcut"):
                messagebox.showinfo("快捷方式已创建", result["shortcut"], parent=root)
        if not busy:
            watching = auto.panel_is_running(auto.MUTEX_NAME)
            if not watching:
                status.set("后台监听已停止。点击“启动 / 显示面板”可继续。")
            elif auto.codex_window() is None:
                status.set("后台已就绪，等待打开 Codex。")
            elif not Path(config["sessions_root"]).is_dir() or not Path(config["desktop_logs_root"]).is_dir():
                status.set("尚未找到 Codex 日志。请在本机 Codex 中打开一个本地聊天。")
            else:
                status.set("Codex 正在运行，面板会自动跟随当前聊天。")
        root.after(500, update)

    run(show_panel, "正在启动后台监听…")
    update()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="Codex 实时用量面板")
    modes = parser.add_mutually_exclusive_group()
    for name in ("settings", "start", "watch", "panel", "install", "disable", "shutdown", "inspect"):
        modes.add_argument("--" + name, action="store_true")
    parser.add_argument("--data-dir", help="独立配置和状态目录，用于隔离测试")
    parser.add_argument("--report", help="将命令结果写入 JSON 文件")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    try:
        if args.inspect:
            result = inspect()
        elif args.shutdown:
            result = shutdown()
        elif args.disable:
            result = auto.disable()
        elif args.panel:
            from usage_panel import main as panel_main
            return panel_main([])
        else:
            ensure_config()
            if args.watch:
                auto.watch()
                return 0
            if args.install:
                result = auto.install()
            elif args.start:
                result = auto.start_independent()
            else:
                settings_window()
                return 0
        if args.report:
            Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        path = log_error()
        if sys.stderr is not None:
            traceback.print_exc()
        if not (args.watch or args.start or args.panel or args.report):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"{exc}\n\n错误记录：{path}", "Codex 用量面板", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
