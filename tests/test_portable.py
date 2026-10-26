"""Exercise the actual EXE from an extracted Chinese/space path and fresh profile."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import winreg
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import VERSION
from codex_autostart import process_in_job

archive = ROOT / "dist" / f"CodexUsagePanel-v{VERSION}-windows-x64.zip"
base = ROOT / ".local" / "tests" / ("便携 测试-" + uuid.uuid4().hex[:8])
app_dir = base / "应用 文件夹"
profile = base / "用户 数据"
app_dir.mkdir(parents=True)
profile.mkdir()
with zipfile.ZipFile(archive) as bundle:
    assert bundle.testzip() is None
    bundle.extractall(app_dir)
exe = app_dir / "CodexUsagePanel" / "CodexUsagePanel.exe"
environment = dict(os.environ)
for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "VIRTUAL_ENV"):
    environment.pop(key, None)
environment["PATH"] = str(Path(os.environ["WINDIR"]) / "System32")


def command(mode):
    report = base / (mode.removeprefix("--") + "-result.json")
    result = subprocess.run([str(exe), mode, "--data-dir", str(profile), "--report", str(report)],
                            cwd=base, env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=25)
    assert result.returncode == 0, (mode, result.returncode, result.stderr, str(profile))
    return json.loads(report.read_text(encoding="utf-8"))


def read(name):
    try:
        return json.loads((profile / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def until(predicate, message):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message + ": " + str(profile))


before = command("--inspect")
assert before["frozen"] and before["app_dir"] == str(exe.parent)
assert before["state_dir"] == str(profile)
assert not before["registered_command"]
assert before["startup_entry_name"].startswith("CodexUsagePanelAutoStart-")
assert before["startup_command"] == subprocess.list2cmdline([str(exe), "--start", "--data-dir", str(profile)])
assert not (exe.parent / "config.json").exists()
assert before["current_codex_window"], "Keep Codex open; this test does not close or modify it"

# A genuinely fresh profile must locate the actual active local chat by itself.
try:
    assert command("--start")["ready"]
    until(lambda: read("status.json").get("running") and read("status.json").get("session_id")
          and read("status.json").get("auto_follow"), "First-run automatic chat discovery")
    assert (profile / "config.json").is_file()
finally:
    command("--shutdown")

# Reproducible UI counters without publishing or copying a real conversation.
source = base / "fixture.jsonl"
usage = {"input_tokens": 1000, "cached_input_tokens": 630, "output_tokens": 200,
         "reasoning_output_tokens": 100, "total_tokens": 1200}
record = {"timestamp": "2026-10-26T01:00:00Z", "type": "event_msg", "payload": {
    "type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}}
source.write_text(json.dumps(record) + "\n", encoding="utf-8")
(profile / "config.json").write_text(json.dumps({"auto_follow": False, "default_session": str(source),
                                              "refresh_ms": 250}), encoding="utf-8")
(profile / "settings.json").write_text(json.dumps({"x": 80, "y": 150, "topmost": False}), encoding="utf-8")
cases = ["first run with no configuration discovers current local Codex chat"]
try:
    installed = command("--install")
    assert installed["installed"]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        assert winreg.QueryValueEx(key, before["startup_entry_name"])[0] == before["startup_command"]
    cases.append("isolated login startup registration points to EXE")
    ready = command("--start")
    assert ready["ready"] and ready["process_in_job"] is False
    until(lambda: read("status.json").get("running") and read("status.json").get("total_tokens") == 1200,
          "Bundled Tk panel did not load usage")
    status = read("status.json")
    assert status["cache_percent"] == 63
    assert not process_in_job(status["process_id"])
    first = status["process_id"]
    assert command("--start")["already_running"]
    assert read("status.json")["process_id"] == first
    cases.append("bundled Python/Tk/COM; 1200 tokens and 63% cache; single instance; outside caller job")
    assert command("--shutdown")["stopped"]
    assert read("settings.json")["x"] == 80 and read("settings.json")["y"] == 150
    assert command("--start")["ready"]
    until(lambda: read("status.json").get("running") and read("status.json").get("process_id") != first,
          "Panel did not restart")
    cases.append("shutdown and restart restore settings")
    disabled = command("--disable")
    assert disabled["disabled"]
    assert command("--shutdown")["stopped"]
    after = command("--inspect")
    assert not after["watcher_running"] and not after["panel_running"] and not after["registered_command"]
    assert not list(profile.glob("*-error.log"))
    cases.append("isolated registration removed; all test processes stopped; no error logs")
    report = {"result": "passed", "archive": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "bytes": archive.stat().st_size, "extracted_exe": str(exe), "cases": cases,
              "real_codex_untouched": True, "production_startup_untouched": True}
    (ROOT / ".local" / "tests" / "portable-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
finally:
    command("--disable")
    command("--shutdown")
