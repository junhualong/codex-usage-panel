"""Run the real watcher, panel process and IPC with an isolated Codex detector."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from test_usage import panel, WORK, event, usage, line

base = WORK / "lifecycle-fixtures"
app_dir = base / "app"
state = base / "state"
app_dir.mkdir(parents=True, exist_ok=True)
state.mkdir(parents=True, exist_ok=True)
for name in ("usage_panel.py", "active_session.py", "generation_speed.py", "codex_autostart.py", "app_runtime.py", "launcher.py",
             "panel_visuals.py", "panel_icons.py", "panel_menu.py", "session_cache.py", "panel_control.py", "panel_tray.py", "panel_diagnostics.py"):
    shutil.copyfile(Path(panel.__file__).parent / name, app_dir / name)
source = base / "usage.jsonl"
source.write_bytes(line(event(usage(), usage())))
config = {"default_session": str(source), "sessions_root": str(base / "sessions"),
          "state_dir": str(state), "refresh_ms": 250, "auto_follow": False}
(app_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
expected_settings = {"session": str(source), "mode": "last", "topmost": False, "x": 80, "y": 90,
                     "theme": "classic", "icon": "robot"}
(state / "settings.json").write_text(json.dumps(expected_settings), encoding="utf-8")

spec = importlib.util.spec_from_file_location("isolated_autostart", app_dir / "codex_autostart.py")
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)
assert not auto.panel_is_running(), "An earlier isolated test panel is still running"
control = base / "window.json"
runner = app_dir / "run_watcher.py"
runner.write_text('''import json
import threading
import time
from pathlib import Path
import codex_autostart as auto
import panel_tray
RealTray = panel_tray.TrayIcon
class TestTray(RealTray):
    def __init__(self, action):
        super().__init__(action)
        def control():
            previous = None
            while not self.stopping:
                try:
                    value = json.loads((Path(__file__).parent.parent / "tray-action.json").read_text(encoding="utf-8"))
                    if value != previous:
                        previous = value
                        action(value[1])
                except (OSError, ValueError):
                    pass
                time.sleep(.05)
        threading.Thread(target=control, daemon=True).start()
panel_tray.TrayIcon = TestTray
def probe(preferred=None):
    value = json.loads((Path(__file__).parent.parent / "window.json").read_text(encoding="utf-8"))
    return tuple(value) if value else None
auto.codex_window = probe
auto.watch()
''', encoding="utf-8")


def set_window(identity):
    temporary = control.with_suffix(".tmp")
    temporary.write_text(json.dumps(identity), encoding="utf-8")
    temporary.replace(control)


action_sequence = 0
def tray_action(action):
    global action_sequence
    action_sequence += 1
    temporary = base / "tray-action.tmp"
    temporary.write_text(json.dumps([action_sequence, action]), encoding="utf-8")
    temporary.replace(base / "tray-action.json")


def read(name):
    try:
        return json.loads((state / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def until(predicate, label, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if watcher.poll() is not None:
            raise AssertionError("Watcher exited: " + str(watcher.returncode))
        time.sleep(0.05)
    raise AssertionError(label + ": " + repr(read("autostart-status.json")) + repr(read("status.json")))


set_window(None)
watch_log = (base / "watcher.log").open("w", encoding="utf-8")
watcher = subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), "-B", "-X", "utf8", str(runner)],
                           cwd=app_dir, stdin=subprocess.DEVNULL, stdout=watch_log, stderr=watch_log,
                           creationflags=subprocess.CREATE_NO_WINDOW)
results = []
try:
    until(lambda: read("autostart-status.json").get("watcher_process_id") == watcher.pid, "watcher ready")
    assert not auto.panel_is_running()
    results.append({"case": "Codex absent", "panel_running": False})
    set_window([7, 80])
    until(lambda: auto.panel_is_running() and read("status.json").get("running"), "initial panel")
    first = read("status.json")["process_id"]
    assert read("status.json")["mode"] == "last"
    assert read("status.json")["total_tokens"] == 1100
    results.append({"case": "Codex opens", "panel_pid": first})
    assert read("autostart-status.json")["tray_available"]
    tray_action("hide")
    until(lambda: read("status.json").get("hidden"), "tray hides panel")
    assert read("status.json")["process_id"] == first
    tray_action("show")
    until(lambda: not read("status.json").get("hidden"), "tray restores panel")
    assert read("status.json")["process_id"] == first
    tray_action("hide")
    until(lambda: read("status.json").get("hidden"), "hide before Codex closes")
    results.append({"case": "tray hide/show keeps process and stats", "panel_pid": first})

    started = time.monotonic()
    set_window(None)
    until(lambda: not auto.panel_is_running() and not read("status.json").get("running"), "graceful exit")
    assert read("status.json")["close_reason"] == "codex_closed"
    assert read("settings.json") == expected_settings
    results.append({"case": "Codex closes", "elapsed_seconds": round(time.monotonic() - started, 3),
                    "settings_saved": True, "close_reason": "codex_closed"})

    set_window([8, 90])
    until(lambda: read("status.json").get("running") and read("status.json").get("process_id") != first,
          "panel reopens")
    second = read("status.json")["process_id"]
    assert read("settings.json") == expected_settings
    results.append({"case": "Codex reopens", "panel_pid": second, "settings_restored": True})

    previous_close = read("autostart-status.json").get("last_close")
    set_window(None)
    until(lambda: read("autostart-status.json").get("last_close") != previous_close, "close signal sent")
    set_window([9, 100])
    until(lambda: read("status.json").get("running") and read("status.json").get("process_id") != second,
          "fast reopen")
    third = read("status.json")["process_id"]
    assert read("status.json")["mode"] == "last"
    results.append({"case": "immediate reopen after close request", "panel_pid": third})

    # Same Codex identity must respect a panel dismissed by the user. The event
    # closes the isolated panel without touching any real app window.
    assert auto.request_panel_close()
    until(lambda: not auto.panel_is_running(), "standalone panel dismissal")
    time.sleep(1.2)
    assert not auto.panel_is_running()
    assert read("autostart-status.json")["last_launch"]["panel_process_id"] == third
    results.append({"case": "panel dismissed while Codex stays open", "respawned": False})
    tray_action("show")
    until(lambda: read("status.json").get("running") and read("status.json").get("process_id") != third
          and read("status.json").get("total_tokens") == 1100 and not read("status.json").get("loading"),
          "tray reopens dismissed panel")
    third = read("status.json")["process_id"]
    assert read("autostart-status.json")["last_launch"]["source"] == "tray"
    assert read("status.json")["total_tokens"] == 1100
    results.append({"case": "tray explicitly reopens dismissed panel", "panel_pid": third})

    set_window(None)
    until(lambda: read("autostart-status.json").get("codex_window") is None, "watcher reset")
    set_window([10, 110])
    until(lambda: read("status.json").get("running") and read("status.json").get("process_id") != third,
          "reopen after dismissal")
    fourth = read("status.json")["process_id"]
    set_window(None)
    until(lambda: not auto.panel_is_running() and not read("status.json").get("running"), "final exit")
    assert read("settings.json") == expected_settings
    assert not (state / "panel-error.log").exists()
    results.append({"case": "next Codex open after dismissal", "panel_pid": fourth, "final_exit": True})
    set_window([11, 120])
    until(lambda: read("status.json").get("running") and read("status.json").get("process_id") != fourth,
          "panel before tray exit")
    tray_action("exit")
    until(lambda: watcher.poll() is not None and not auto.panel_is_running(), "tray exits both processes")
    assert not read("autostart-status.json")["running"]
    assert not read("autostart-status.json")["tray_available"]
    results.append({"case": "tray exit closes panel, watcher and icon", "passed": True})
    report = {"result": "passed", "watcher_pid": watcher.pid, "isolated_detector": True, "cases": results}
    (WORK / "lifecycle-process-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
finally:
    auto.stop_watcher()
    watcher.wait(timeout=5)
    if auto.panel_is_running():
        auto.request_panel_close()
    watch_log.close()
