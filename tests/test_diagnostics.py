import json
import unittest
from uuid import uuid4

from test_usage import WORK
from panel_diagnostics import format_report, public_report, read_status
from panel_control import PanelCommands, event_name, request


class DiagnosticsTests(unittest.TestCase):
    def sample(self):
        return {"watcher_running": True, "panel_running": True, "sessions_found": True,
                "navigation_logs_found": True, "watcher": {"tray_available": True},
                "panel": {"auto_follow": True, "session_id": "secret-id", "session_name": "secret-chat",
                          "usage_log": r"C:\Users\secret-user\secret.jsonl", "reported_at": "2026-10-31T12:00:00+08:00",
                          "usage_timestamp": "2020-01-01T00:00:00+08:00", "display_state": "等待新的调用",
                          "error": "PermissionError: secret-path", "settings_error": "secret-settings-path",
                          "reader_health": {"usage": {"last_success": "2026-10-31T12:00:00+08:00", "error": "secret-error"}},
                          "read_cache": {"entries": 2, "limit": 6, "hits": 3, "misses": 4}}}

    def test_copy_allowlist_excludes_all_private_strings(self):
        result = public_report(self.sample())
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(result["settings_error_present"])
        self.assertTrue(result["read_error_present"])
        self.assertEqual(result["read_cache"]["hits"], 3)

    def test_old_usage_is_not_reported_as_disconnected(self):
        text = format_report(self.sample())
        self.assertIn("等待新的调用", text)
        self.assertIn("2020-01-01", text)
        self.assertIn("最近检查", text)
        self.assertNotIn("连接异常", text)

    def test_closed_panel_does_not_present_old_chat_as_current(self):
        report = self.sample()
        report["panel_running"] = False
        self.assertNotIn("secret-chat", format_report(report))
        self.assertIn("上次退出前设置未保存", format_report(report))

    def test_missing_or_malformed_status_is_safe(self):
        path = WORK / ("diagnostic-" + uuid4().hex[:8] + ".json")
        self.assertEqual(read_status(path), {})
        path.write_text("{incomplete", encoding="utf-8")
        self.assertEqual(read_status(path), {})
        self.assertIn("等待首份运行报告", format_report({"panel_running": True}))
        self.assertEqual(public_report({})["reported_at"], "")

    def test_actions_are_installation_scoped_and_auto_reset(self):
        suffix = "test-" + uuid4().hex
        commands = PanelCommands(suffix)
        try:
            self.assertTrue(request("hide", suffix))
            self.assertFalse(request("hide", suffix + "-other"))
            self.assertEqual(commands.pending(), ["hide"])
            self.assertEqual(commands.pending(), [])
            self.assertTrue(request("recenter", suffix))
            self.assertEqual(commands.pending(), ["recenter"])
        finally:
            commands.close()
        self.assertFalse(request("show", suffix))

    def test_arbitrary_commands_are_rejected(self):
        with self.assertRaises(ValueError):
            event_name("arbitrary.exe")


if __name__ == "__main__":
    unittest.main(verbosity=2)

