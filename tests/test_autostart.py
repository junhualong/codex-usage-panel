import unittest
import json
from unittest.mock import patch
from test_usage import panel
import codex_autostart as auto


class AutoStartTests(unittest.TestCase):
    def setUp(self):
        self.gate = auto.LaunchGate()
        self.calls = []

    def launch(self):
        self.calls.append(True)
        return 12345

    def test_no_codex_window_does_not_launch(self):
        self.assertIsNone(self.gate.step(None, 0, self.launch))
        self.assertFalse(self.calls)

    def test_open_codex_launches_exactly_once(self):
        self.assertEqual(self.gate.step((7, 80), 0, self.launch)["event"], "panel_started")
        for second in range(1, 20):
            self.assertIsNone(self.gate.step((7, 80), second, self.launch))
        self.assertEqual(len(self.calls), 1)

    def test_user_closing_panel_does_not_cause_respawn(self):
        self.gate.step((7, 80), 0, self.launch)
        self.assertIsNone(self.gate.step((7, 80), 1000, self.launch))
        self.assertEqual(len(self.calls), 1)

    def test_codex_close_and_reopen_triggers_again(self):
        self.gate.step((7, 80), 0, self.launch)
        self.gate.step(None, 1, self.launch)
        self.gate.step((7, 80), 2, self.launch)
        self.assertEqual(len(self.calls), 2)

    def test_recreated_window_or_restarted_process_triggers_again(self):
        for second, identity in enumerate(((7, 80), (7, 90), (8, 100))):
            self.gate.step(identity, second, self.launch)
        self.assertEqual(len(self.calls), 3)

    def test_failed_launch_retries_with_backoff_and_limit(self):
        def fail():
            self.calls.append(True)
            raise OSError("isolated simulated failure")
        for second in range(20):
            self.gate.step((7, 80), second, fail)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.gate.step((8, 81), 21, self.launch)["event"], "panel_started")

    def test_install_does_not_overwrite_unrelated_entry(self):
        with patch.object(auto, "registration", return_value="different command"), \
                patch.object(auto, "_startup_registry") as registry:
            with self.assertRaises(RuntimeError):
                auto.install()
            registry.assert_not_called()

    def test_disable_does_not_delete_unrelated_entry(self):
        with patch.object(auto, "registration", return_value="different command"), \
                patch.object(auto, "_startup_registry") as registry:
            with self.assertRaises(RuntimeError):
                auto.disable()
            registry.assert_not_called()

    def test_private_registry_value_does_not_claim_windows_startup_is_enabled(self):
        with patch.object(auto, "_startup_registry", return_value={"sNames": (), "Types": ()}), \
                patch.object(auto.winreg, "QueryValueEx", return_value=("private-only", 1)) as private:
            self.assertIsNone(auto.registration())
            private.assert_not_called()

    def test_registration_reads_windows_value_case_insensitively(self):
        records = {"sNames": ("other", auto.RUN_NAME.lower()), "Types": (1, 1)}
        with patch.object(auto, "_startup_registry", side_effect=[records, {"sValue": "visible command"}]):
            self.assertEqual(auto.registration(), "visible command")

    def test_unexpected_registry_type_is_not_overwritten(self):
        with patch.object(auto, "_startup_registry", return_value={"sNames": (auto.RUN_NAME,), "Types": (4,)}):
            with self.assertRaisesRegex(RuntimeError, "类型不符"):
                auto.install()

    def test_registry_read_failure_is_not_reported_as_disabled(self):
        with patch.object(auto, "_startup_registry", side_effect=PermissionError("registry unavailable")):
            with self.assertRaises(PermissionError):
                auto.registration()

    def test_install_requires_windows_readback_to_match(self):
        with patch.object(auto, "registration", side_effect=[None, None]), \
                patch.object(auto, "_startup_registry"):
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                auto.install()

    def test_unregister_requires_windows_readback_to_be_empty(self):
        with patch.object(auto, "registration", return_value=auto.startup_command()), \
                patch.object(auto, "_startup_registry"):
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                auto.unregister()

    def test_stale_ready_file_from_dead_process_does_not_abort_startup(self):
        stale = {"running": True, "process_in_job": False, "watcher_process_id": 99}
        fresh = dict(stale, watcher_process_id=123)
        with patch.object(auto, "load_config", return_value={"state_dir": "."}), \
                patch.object(auto, "create_independent_process", return_value=123), \
                patch.object(auto, "panel_is_running", side_effect=[False, True, True]), \
                patch.object(auto.Path, "read_text", side_effect=[json.dumps(stale), json.dumps(fresh)]), \
                patch.object(auto, "process_in_job", side_effect=[OSError("old PID is gone"), False]), \
                patch.object(auto.time, "monotonic", side_effect=[0, 1, 2]), \
                patch.object(auto.time, "sleep"):
            result = auto.start_independent()
        self.assertTrue(result["ready"])
        self.assertEqual(result["watcher_process_id"], 123)
        self.assertFalse(result["already_running"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
