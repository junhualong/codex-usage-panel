"""Local runtime diagnostics; the shareable report is an explicit privacy allowlist."""
from datetime import datetime
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from app_runtime import VERSION, load_config


def read_status(path, errors=None):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.loads(stream.read(1024 * 1024))
        if not isinstance(value, dict):
            raise ValueError("状态记录必须是 JSON 对象")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError) as exc:
        if errors is not None:
            errors.append(f"{Path(path).name}：{type(exc).__name__}：{exc}")
        return {}


def collect(config=None):
    import codex_autostart as auto
    config = config or load_config()
    state = Path(config["state_dir"])
    errors = []
    return {"version": VERSION, "watcher_running": auto.panel_is_running(auto.MUTEX_NAME),
            "panel_running": auto.panel_is_running(),
            "sessions_found": Path(config["sessions_root"]).is_dir(),
            "navigation_logs_found": Path(config["desktop_logs_root"]).is_dir(),
            "panel": read_status(state / "status.json", errors),
            "watcher": read_status(state / "autostart-status.json", errors), "status_errors": errors}


def clock_text(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError, OverflowError):
        return "—"


def safe_time(value):
    return value if isinstance(value, str) and clock_text(value) != "—" else ""


def public_report(report):
    """Do not export chat names/IDs, source paths, command lines or free-form errors."""
    panel = report.get("panel", {})
    result = {"version": VERSION, "privacy": "已省略聊天名称、会话 ID、路径和原始错误文本"}
    for key in ("watcher_running", "panel_running", "sessions_found", "navigation_logs_found"):
        result[key] = report.get(key) is True
    for key in ("reported_at", "usage_timestamp"):
        result[key] = safe_time(panel.get(key))
    for key in ("auto_follow", "hidden", "follow_reader_alive", "usage_reader_alive", "loading"):
        result[key] = panel.get(key) is True
    result["read_error_present"] = bool(panel.get("error"))
    result["status_error_present"] = bool(report.get("status_errors"))
    result["settings_error_present"] = bool(panel.get("settings_error"))
    result["readers"] = {}
    for name in ("follow", "usage"):
        health = panel.get("reader_health", {}).get(name, {})
        result["readers"][name] = {"last_check": safe_time(health.get("last_check")),
                                   "last_success": safe_time(health.get("last_success")),
                                   "error_present": bool(health.get("error"))}
    result["read_cache"] = {key: value for key in ("entries", "limit", "hits", "misses")
                            if isinstance(value := panel.get("read_cache", {}).get(key), int) and not isinstance(value, bool)}
    return result


def format_report(report):
    panel, watcher = report.get("panel", {}), report.get("watcher", {})
    alive = report.get("panel_running", False)
    yes = lambda value: "运行中" if value else "未运行"
    lines = [f"程序版本：v{VERSION}",
             f"后台监听：{yes(report.get('watcher_running'))}    面板：{yes(alive)}" + ("（已隐藏）" if alive and panel.get("hidden") else ""),
             "托盘入口：" + ("已就绪" if report.get("watcher_running") and watcher.get("tray_available") else "未就绪"),
             "会话目录：" + ("已找到" if report.get("sessions_found") else "未找到"),
             "导航日志目录：" + ("已找到" if report.get("navigation_logs_found") else "未找到")]
    if watcher.get("tray_error"):
        lines.append("托盘原因：" + str(watcher["tray_error"]))
    lines.extend("状态文件读取失败：" + str(error) for error in report.get("status_errors", []))
    if not alive:
        lines += ["", "面板尚未运行，可从托盘或启动设置显示面板。"]
        if panel.get("settings_error"):
            lines.append("上次退出前设置未保存：" + str(panel["settings_error"]))
        return "\n".join(lines)
    lines += ["", "跟随方式：" + ("自动跟随当前聊天" if panel.get("auto_follow") else "固定会话"),
              "当前聊天：" + str(panel.get("session_name") or "尚未识别"),
              "状态：" + str(panel.get("display_state") or "等待首份运行报告"),
              "说明：" + str(panel.get("state_reason") or "—"),
              "状态上报时间：" + clock_text(panel.get("reported_at")),
              "数据时间（统计记录）：" + clock_text(panel.get("usage_timestamp")), ""]
    for name, label in (("follow", "聊天识别"), ("usage", "用量读取")):
        health = panel.get("reader_health", {}).get(name, {})
        enabled = panel.get("auto_follow") if name == "follow" else panel.get("usage_log_available", bool(panel.get("session_id")) or not panel.get("auto_follow", True))
        lines += [label + "线程：" + (yes(panel.get(name + "_reader_alive")) if enabled else "当前无需运行"),
                  "  最近检查：" + clock_text(health.get("last_check")) + "    最近成功：" + clock_text(health.get("last_success"))]
        if health.get("error"):
            lines.append("  原因：" + str(health["error"]))
    cache = panel.get("read_cache", {})
    lines += ["", f"读取进度缓存：{cache.get('entries', 0)} / {cache.get('limit', 6)} 个聊天；复用 {cache.get('hits', 0)} 次",
              "设置保存：" + (str(panel["settings_error"]) + "（每 5 秒重试）" if panel.get("settings_error") else "无待处理的保存错误"),
              "最近操作：" + str(panel.get("last_action") or "—"),
              "用量日志：" + str(panel.get("usage_log") or "—"),
              "导航日志：" + str(panel.get("source_log") or "—"), "",
              "数据时间较早可以是正常空闲，不据此判断故障。\n状态上报和最近检查时间用于观察程序是否继续工作。"]
    return "\n".join(lines)


class DiagnosticsWindow:
    def __init__(self, root, provider=collect, sender=None):
        from panel_control import request
        from usage_panel import work_area
        self.root, self.provider, self.sender = root, provider, sender or request
        self.job = None
        self.report = {}
        self.last_text = None
        root.title("Codex 用量面板 · 运行状态与诊断")
        root.configure(bg="#17222e")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Diagnostic.TButton", background="#29475e", foreground="#eef4fa", bordercolor="#405363",
                        lightcolor="#29475e", darkcolor="#29475e", padding=(10, 6), font=("Microsoft YaHei UI", 9))
        style.map("Diagnostic.TButton", background=[("active", "#334c60"), ("disabled", "#202f3e")],
                  foreground=[("disabled", "#95a8b8")])
        style.configure("Diagnostic.Vertical.TScrollbar", background="#405363", troughcolor="#202f3e",
                        bordercolor="#202f3e", lightcolor="#405363", darkcolor="#405363", arrowcolor="#c1ceda")
        left, top, right, bottom = work_area(root)
        width, height = min(780, max(1, right-left-40)), min(650, max(1, bottom-top-80))
        root.geometry(f"{width}x{height}+{left+(right-left-width)//2}+{top+(bottom-top-height)//2}")
        root.minsize(min(600, width), min(400, height))
        tk.Label(root, text="运行状态与诊断", font=("Microsoft YaHei UI", 15, "bold"),
                 bg="#17222e", fg="#eef4fa", anchor="w").pack(fill="x", padx=18, pady=(14, 8))
        frame = tk.Frame(root, bg="#17222e")
        frame.pack(fill="both", expand=True, padx=18)
        self.body = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10), bd=0, highlightthickness=0,
                            bg="#202f3e", fg="#eef4fa", padx=12, pady=10, state="disabled")
        scroll = ttk.Scrollbar(frame, command=self.body.yview, style="Diagnostic.Vertical.TScrollbar")
        self.body.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.body.pack(side="left", fill="both", expand=True)
        buttons = tk.Frame(root, bg="#17222e")
        buttons.pack(fill="x", padx=18, pady=(12, 6))
        self.buttons = {}
        for command, label in (("rediscover", "重新识别聊天"), ("reload", "重新读取统计"), ("retry_save", "重试保存设置")):
            button = ttk.Button(buttons, text=label, style="Diagnostic.TButton", command=lambda command=command: self.perform(command))
            button.pack(side="left", padx=(0, 8))
            self.buttons[command] = button
        ttk.Button(buttons, text="复制脱敏诊断", style="Diagnostic.TButton", command=self.copy).pack(side="right")
        self.feedback = tk.StringVar(value="仅在本机读取；复制时不包含聊天名称、路径和原始错误文本。")
        tk.Label(root, textvariable=self.feedback, font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
                 wraplength=max(400, width-36), bg="#17222e", fg="#c1ceda").pack(fill="x", padx=18, pady=(0, 12))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def refresh(self):
        self.job = None
        try:
            self.report = self.provider()
            text = format_report(self.report)
            alive = bool(self.report.get("panel_running"))
            for button in self.buttons.values():
                button.configure(state="normal" if alive else "disabled")
            panel = self.report.get("panel", {})
            if not panel.get("auto_follow"):
                self.buttons["rediscover"].configure(state="disabled")
            if not panel.get("usage_log_available", bool(panel.get("session_id")) or not panel.get("auto_follow", True)):
                self.buttons["reload"].configure(state="disabled")
            if not panel.get("settings_error"):
                self.buttons["retry_save"].configure(state="disabled")
        except Exception as exc:
            text = "无法读取运行状态：" + str(exc)
            for button in self.buttons.values():
                button.configure(state="disabled")
        if text != self.last_text:
            view = self.body.yview()
            self.body.configure(state="normal")
            self.body.delete("1.0", "end")
            self.body.insert("1.0", text)
            self.body.configure(state="disabled")
            self.body.yview_moveto(view[0])
            self.last_text = text
        self.job = self.root.after(1000, self.refresh)

    def perform(self, command):
        try:
            sent = self.sender(command)
            self.feedback.set("操作请求已发送，执行结果见“最近操作”。" if sent else "面板控制入口尚未就绪，请先显示面板后重试。")
        except Exception as exc:
            self.feedback.set("操作未发送：" + str(exc))

    def copy(self):
        try:
            text = json.dumps(public_report(self.report), ensure_ascii=False, indent=2)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.feedback.set("已复制脱敏诊断；未包含聊天名称、会话 ID、路径或原始错误文本。")
        except Exception:
            self.feedback.set("诊断记录暂不可用，未复制原始内容。请等待下一次状态刷新后重试。")

    def close(self):
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None
        self.root.destroy()


def main():
    from usage_panel import enable_dpi
    enable_dpi()
    root = tk.Tk()
    DiagnosticsWindow(root)
    root.mainloop()

