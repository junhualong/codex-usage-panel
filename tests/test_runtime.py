import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_usage import WORK
import app_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="通用入口 ", dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "用户数据"
        self.argv = patch.object(sys, "argv", ["test", "--data-dir", str(self.profile)])
        self.argv.start()
        self.addCleanup(self.argv.stop)

    def test_first_run_uses_user_defaults_without_source_config(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "自定义 Codex"), "LOCALAPPDATA": str(self.root)}):
            config = runtime.ensure_config()
        self.assertEqual(Path(config["sessions_root"]), self.root / "自定义 Codex" / "sessions")
        self.assertEqual(Path(config["desktop_logs_root"]), self.root / "Codex" / "Logs")
        self.assertEqual(Path(config["state_dir"]), self.profile)
        stored = json.loads((self.profile / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["sessions_root"], "")
        self.assertNotIn(str(self.root), json.dumps(stored))

    def test_profile_overrides_legacy_state_location(self):
        self.profile.mkdir()
        (self.profile / "config.json").write_text(json.dumps({"state_dir": str(self.root / "other")}), encoding="utf-8")
        self.assertEqual(runtime.load_config()["state_dir"], str(self.profile))

    def test_expands_environment_paths_and_preserves_config(self):
        self.profile.mkdir()
        text = json.dumps({"sessions_root": "%PANEL_TEST_ROOT%/sessions", "auto_follow": False})
        (self.profile / "config.json").write_text(text, encoding="utf-8")
        with patch.dict(os.environ, {"PANEL_TEST_ROOT": str(self.root)}):
            config = runtime.ensure_config()
        self.assertEqual(Path(config["sessions_root"]), self.root / "sessions")
        self.assertFalse(config["auto_follow"])
        self.assertEqual((self.profile / "config.json").read_text(encoding="utf-8"), text)

    def test_bad_config_is_reported_without_overwrite(self):
        self.profile.mkdir()
        path = self.profile / "config.json"
        for content in ("{bad", "[]", '{"refresh_ms": 0}', '{"auto_follow": "yes"}'):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                runtime.ensure_config()
            self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_frozen_command_keeps_executable_location_and_profile(self):
        executable = str(self.root / "程序 1" / "CodexUsagePanel.exe")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", executable):
            self.assertEqual(runtime.app_directory(), Path(executable).parent)
            self.assertEqual(runtime.app_command("--watch"), [executable, "--watch", "--data-dir", str(self.profile)])

    def test_source_command_propagates_profile_and_chinese_path(self):
        command = runtime.app_command("--panel", self.root)
        self.assertEqual(command[-4:], [str(self.root / "launcher.py"), "--panel", "--data-dir", str(self.profile)])

    def test_profiles_have_independent_ipc(self):
        first = runtime.instance_suffix(self.root)
        with patch.object(sys, "argv", ["test", "--data-dir", str(self.root / "another")]):
            self.assertNotEqual(first, runtime.instance_suffix(self.root))

    def test_source_legacy_config_is_retained(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"state_dir": str(self.root / "old-state")}), encoding="utf-8")
        with patch.object(sys, "argv", ["test"]):
            self.assertEqual(runtime.config_path(self.root), path)
            self.assertEqual(Path(runtime.load_config(self.root)["state_dir"]), self.root / "old-state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
