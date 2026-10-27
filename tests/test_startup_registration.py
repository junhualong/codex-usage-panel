"""Verify login registration from a separate Windows process, without restarting Codex."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
import winreg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_autostart as auto


def probe(name, report):
    # Deliberately use native winreg here, not the application's WMI reader.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, auto.RUN_KEY) as key:
            command, kind = winreg.QueryValueEx(key, name)
        result = {"command": command, "kind": kind}
    except FileNotFoundError:
        result = {"command": None}
    Path(report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", nargs=2, metavar=("NAME", "REPORT"), help=argparse.SUPPRESS)
    parser.add_argument("--exe", type=Path, help="Optional rebuilt portable EXE instead of source")
    args = parser.parse_args()
    if args.probe:
        probe(*args.probe)
        return

    folder = ROOT / ".local" / "tests" / ("startup-registration-" + uuid.uuid4().hex[:10])
    folder.mkdir(parents=True)
    profile = folder / "profile"
    entry = [str(args.exe.resolve())] if args.exe else [sys.executable, "-B", "-X", "utf8", str(ROOT / "launcher.py")]
    real_registration = auto.registration()

    def command(mode, label):
        report = folder / (label + ".json")
        # Frozen stdout follows the Windows code page; only the report is UTF-8.
        completed = subprocess.run([*entry, mode, "--data-dir", str(profile), "--report", str(report)],
                                   cwd=ROOT, capture_output=True, timeout=20)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(report.read_text(encoding="utf-8"))

    def independent_read(label):
        report = folder / (label + ".json")
        auto.create_independent_process([sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
                                         "--probe", name, str(report)])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return json.loads(report.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
        raise AssertionError("Independent Windows process did not publish registry results")

    before = command("--inspect", "before")
    name = before["startup_entry_name"]
    expected = before["startup_command"]
    assert name != auto.RUN_NAME and name.startswith("CodexUsagePanelAutoStart-")
    assert before["registered_command"] is None
    assert independent_read("outside-before")["command"] is None
    cases = []
    try:
        assert command("--install", "install")["installed"]
        outside = independent_read("outside-installed")
        assert outside == {"command": expected, "kind": winreg.REG_SZ}, outside
        assert command("--inspect", "installed")["registered_command"] == expected
        cases.append("installed entry is visible to a native reader outside the caller process tree")
        assert command("--install", "reinstall")["installed"]
        assert independent_read("outside-reinstalled")["command"] == expected
        cases.append("re-enabling the same installation is idempotent")
        assert command("--disable", "disable")["disabled"]
        assert independent_read("outside-disabled")["command"] is None
        assert command("--inspect", "disabled")["registered_command"] is None
        cases.append("disabled entry is absent in both application and independent Windows views")
    finally:
        command("--disable", "cleanup")
        assert independent_read("outside-cleanup")["command"] is None
        assert auto.registration() == real_registration, "Production registration changed"
    report = {"result": "passed", "cases": cases, "profile": str(profile),
              "production_startup_untouched": True, "real_codex_untouched": True}
    (folder / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(folder / "results.json")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
