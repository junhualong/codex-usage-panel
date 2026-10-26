"""Build an allowlisted, self-contained Windows x64 portable release."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_runtime import VERSION


def icon(path):
    # Original application icon: a compact usage chart, no external artwork.
    size = 48
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            color = (28, 30, 36, 255)
            if 9 <= x < 16 and 25 <= y < 37:
                color = (142, 214, 178, 255)
            if 21 <= x < 28 and 17 <= y < 37:
                color = (158, 213, 255, 255)
            if 33 <= x < 40 and 9 <= y < 37:
                color = (158, 213, 255, 255)
            r, g, b, a = color
            pixels.extend((b, g, r, a))
    dib = struct.pack("<IIIHHIIIIII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    dib += pixels + bytes(((size + 31) // 32) * 4 * size)
    path.write_bytes(struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(dib), 22) + dib)


def collect_licenses(target):
    target.mkdir(parents=True, exist_ok=True)
    base = Path(sys.base_prefix)
    required = {"Python.txt": base / "LICENSE.txt", "Tk.txt": base / "tcl" / "tk8.6" / "license.terms",
                "Tcl.txt": ROOT / "packaging" / "licenses" / "Tcl.txt"}
    for name, source in required.items():
        if not source.is_file():
            raise FileNotFoundError(f"Required bundled license: {source}")
        shutil.copyfile(source, target / name)
    for package, suffixes in {"pyinstaller": ("COPYING.txt",), "pywin32": ("win32/license.txt", "win32com/License.txt")}.items():
        distribution = importlib.metadata.distribution(package)
        for suffix in suffixes:
            match = next((file for file in distribution.files if str(file).endswith(suffix)), None)
            if match is None:
                raise FileNotFoundError(f"Missing {package} license: {suffix}")
            name = package + "-" + suffix.replace("/", "-")
            shutil.copyfile(distribution.locate_file(match), target / name)


def main():
    if sys.platform != "win32" or struct.calcsize("P") != 8 or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("Build using Windows x64 and 64-bit Python 3.12 with Tk.")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("The release is pinned to Python 3.12.")
    work = ROOT / ".local" / "build"
    dist = ROOT / "dist"
    work.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    app_icon = work / "panel.ico"
    icon(app_icon)
    numbers = tuple(int(x) for x in VERSION.split(".")) + (0,)
    version_file = work / "version_info.txt"
    version_file.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)), "
        f"kids=[StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription', 'Codex Usage Panel'), StringStruct('ProductName', 'Codex Usage Panel'), "
        f"StringStruct('FileVersion', '{VERSION}'), StringStruct('ProductVersion', '{VERSION}'), StringStruct('OriginalFilename', 'CodexUsagePanel.exe')])]), "
        "VarFileInfo([VarStruct('Translation', [1033, 1200])])])", encoding="utf-8")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--noupx",
        "--name", "CodexUsagePanel", "--icon", str(app_icon), "--version-file", str(version_file),
        "--distpath", str(dist), "--workpath", str(work / "pyinstaller"), "--specpath", str(work),
        "--hidden-import", "win32com.client.dynamic", str(ROOT / "launcher.py"),
    ], cwd=ROOT, check=True)
    app = dist / "CodexUsagePanel"
    shutil.copyfile(app_icon, app / "panel.ico")
    for name in ("README.md", "使用说明.txt", "config.example.json", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, app / name)
    collect_licenses(app / "licenses")
    manifest = {"version": VERSION, "python": platform.python_version(), "platform": platform.platform(),
                "dependencies": {name: importlib.metadata.version(name) for name in ("pyinstaller", "pyinstaller-hooks-contrib", "pywin32")}}
    (app / "build-info.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    forbidden = {"config.json", "settings.json", "status.json", "auth.json", "autostart-status.json"}
    files = sorted(path for path in app.rglob("*") if path.is_file())
    for path in files:
        if path.name in forbidden or path.suffix.lower() in (".jsonl", ".log", ".lnk") or ".local" in path.relative_to(app).parts:
            raise RuntimeError(f"Refusing to package local data: {path}")
    archive = dist / f"CodexUsagePanel-v{VERSION}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(dist))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest,
                      "files": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
