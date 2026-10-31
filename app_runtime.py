"""Paths and commands shared by the source and portable Windows builds."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

VERSION = "1.0.6"
APP_NAME = "Codex Usage Panel"
REPOSITORY = "https://github.com/junhualong/codex-usage-panel"


def app_directory(module_file=__file__):
    # In a bundle __file__ points into _internal, not next to the executable.
    return Path(sys.executable if getattr(sys, "frozen", False) else module_file).resolve().parent


def profile_directory():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir")
    args, _ = parser.parse_known_args()
    return Path(args.data_dir).expanduser().resolve() if args.data_dir else None


def default_data_directory():
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    return local / "CodexUsagePanel"


def config_path(base=None):
    base = Path(base or app_directory())
    profile = profile_directory()
    if profile:
        return profile / "config.json"
    legacy = base / "config.json"
    if not getattr(sys, "frozen", False) and legacy.is_file():
        return legacy
    return default_data_directory() / "config.json"


def expanded_path(value):
    return str(Path(os.path.expandvars(value)).expanduser().resolve())


def load_config(base=None):
    path = config_path(base)
    settings = {}
    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"配置文件不是有效的 UTF-8 JSON：{path}") from exc
        if not isinstance(settings, dict):
            raise ValueError(f"配置文件必须是 JSON 对象：{path}")
    codex_home = settings.get("codex_home") or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    config = {
        "default_session": "", "sessions_root": str(Path(expanded_path(codex_home)) / "sessions"),
        "desktop_logs_root": str(local / "Codex" / "Logs"),
        "state_dir": str(default_data_directory()), "refresh_ms": 1000,
        "auto_follow": True, "follow_window_id": 1,
    }
    config.update({key: value for key, value in settings.items() if value not in (None, "")})
    for key in ("sessions_root", "desktop_logs_root", "state_dir", "default_session"):
        if config.get(key):
            config[key] = expanded_path(config[key])
    profile = profile_directory()
    if profile:
        config["state_dir"] = str(profile)
    if not isinstance(config["auto_follow"], bool):
        raise ValueError("auto_follow 必须为 true 或 false")
    if not isinstance(config["refresh_ms"], (int, float)) or not 250 <= config["refresh_ms"] <= 60000:
        raise ValueError("refresh_ms 必须在 250～60000 毫秒之间")
    if not isinstance(config["follow_window_id"], int) or config["follow_window_id"] < 1:
        raise ValueError("follow_window_id 必须是正整数")
    return config


def ensure_config(base=None):
    config = load_config(base)
    path = config_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Store overrides only; paths remain portable across user accounts.
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump({"codex_home": "", "sessions_root": "", "desktop_logs_root": "",
                       "refresh_ms": 1000, "auto_follow": True, "follow_window_id": 1},
                      stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    Path(config["state_dir"]).mkdir(parents=True, exist_ok=True)
    return config


def instance_suffix(base=None):
    identity = str(Path(base or app_directory()))
    profile = profile_directory()
    if profile:
        identity += "|" + os.path.normcase(str(profile))
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def pythonw():
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        raise FileNotFoundError("找不到 pythonw.exe，请使用 Windows Python 官方安装包或便携发布版")
    return executable


def profile_arguments():
    profile = profile_directory()
    return ["--data-dir", str(profile)] if profile else []


def app_command(mode, base=None):
    if getattr(sys, "frozen", False):
        return [sys.executable, mode, *profile_arguments()]
    base = Path(base or app_directory())
    return [str(pythonw()), "-B", "-X", "utf8", str(base / "launcher.py"), mode, *profile_arguments()]
