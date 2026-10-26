"""Reproduce loss with an inherited job, then verify the WMI watcher survives it."""
import ctypes
from ctypes import wintypes
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from test_usage import panel, WORK, event, usage, line

base = WORK / "restart-fixtures"
app_dir, state = base / "app", base / "state"
app_dir.mkdir(parents=True, exist_ok=True)
state.mkdir(parents=True, exist_ok=True)
for name in ("usage_panel.py", "codex_autostart.py", "active_session.py", "generation_speed.py", "app_runtime.py", "launcher.py"):
    shutil.copyfile(Path(panel.__file__).parent / name, app_dir / name)
config = panel.load_config()
source = base / "usage.jsonl"
source.write_bytes(line(event(usage(), usage())))
config.update(default_session=str(source), state_dir=str(state), auto_follow=False, sessions_root=str(base / "sessions"))
(app_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
(state / "settings.json").write_text(json.dumps({"session": str(source), "x": 70, "y": 80, "topmost": False}), encoding="utf-8")
spec = importlib.util.spec_from_file_location("restart_autostart", app_dir / "codex_autostart.py")
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)
assert auto.codex_window() is not None, "This test requires the current Codex to stay open"
assert not auto.panel_is_running(auto.MUTEX_NAME) and not auto.panel_is_running()

launcher = app_dir / "test_launcher.py"
launcher.write_text('''import json, os, sys, time
from pathlib import Path
import codex_autostart as auto
base = Path(__file__).parent.parent
phase = sys.argv[1]
(base / (phase + ".ready")).write_text(str(os.getpid()))
while not (base / (phase + ".go")).exists():
    time.sleep(0.05)
if phase == "inherited":
    auto.watch()
else:
    result = auto.start_independent()
    (base / (phase + ".result.json")).write_text(json.dumps(result))
    while True:
        time.sleep(0.2)
''', encoding="utf-8")


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


kernel = auto.kernel_api()
kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel.CreateJobObjectW.restype = wintypes.HANDLE
kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]


def read(name):
    try:
        return json.loads((state / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def until(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(label + ": " + repr(read("autostart-status.json")))


results = []
try:
    for phase in ("inherited", "independent"):
        go = base / (phase + ".go")
        if go.exists():
            go.unlink()  # Exact test barrier only, to allow repeat verification.
        job = kernel.CreateJobObjectW(None, None)
        assert job
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; applies only to our test launcher.
        assert kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        log = (base / (phase + ".log")).open("w", encoding="utf-8")
        child = subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), "-B", "-X", "utf8", str(launcher), phase],
                                 cwd=app_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until(lambda: (base / (phase + ".ready")).exists()
                  and (base / (phase + ".ready")).read_text() == str(child.pid), "launcher barrier")
            assert kernel.AssignProcessToJobObject(job, wintypes.HANDLE(child._handle)), ctypes.get_last_error()
            go.write_text("start")
            if phase == "independent":
                until(lambda: (base / (phase + ".result.json")).exists() and auto.panel_is_running(auto.MUTEX_NAME), "independent ready")
            until(lambda: read("status.json").get("running") and auto.panel_is_running()
                  and read("status.json").get("process_id") ==
                  (read("autostart-status.json").get("last_launch") or {}).get("panel_process_id"), "panel ready")
            watcher_pid = read("autostart-status.json")["watcher_process_id"]
            panel_pid = read("status.json")["process_id"]
            inside = auto.process_in_job(watcher_pid)
            assert inside == (phase == "inherited"), (phase, inside)
            assert kernel.TerminateJobObject(job, 77)
            assert child.wait(timeout=5) == 77
            if phase == "inherited":
                until(lambda: not auto.panel_is_running(auto.MUTEX_NAME) and not auto.panel_is_running(), "old failure reproduced")
                results.append({"case": phase, "watcher_pid": watcher_pid, "panel_pid": panel_pid,
                                "in_job": inside, "after_launcher_job_ends": "watcher and panel both terminated"})
            else:
                # Multiple polling intervals after the initiating process is gone.
                time.sleep(2.2)
                assert auto.panel_is_running(auto.MUTEX_NAME) and auto.panel_is_running()
                assert not auto.process_in_job(watcher_pid) and not auto.process_in_job(panel_pid)
                assert read("status.json")["process_id"] == panel_pid
                duplicate = auto.start_independent()
                assert duplicate["already_running"]
                assert read("autostart-status.json")["watcher_process_id"] == watcher_pid
                results.append({"case": phase, "watcher_pid": watcher_pid, "panel_pid": panel_pid,
                                "in_job": inside, "after_launcher_job_ends": "watcher and panel still running",
                                "duplicate": "existing watcher retained"})
        finally:
            kernel.CloseHandle(job)
            log.close()
    report = {"result": "passed", "real_codex_untouched": True, "cases": results}
    (WORK / "restart-survival-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
finally:
    auto.stop_watcher()
    until(lambda: not auto.panel_is_running(auto.MUTEX_NAME), "isolated watcher stopped")
    auto.request_panel_close()
    until(lambda: not auto.panel_is_running(), "isolated panel closed")
