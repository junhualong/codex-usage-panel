import os
import tkinter as tk
import unittest
import uuid
from unittest.mock import Mock, patch
from test_usage import panel
import codex_autostart as auto


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.gate = auto.LifecycleGate()
        self.launch = Mock(return_value=12345)
        self.running = Mock(return_value=False)
        self.close = Mock(return_value=True)

    def step(self, identity, now=0):
        return self.gate.step(identity, now, self.launch, self.running, self.close)

    def test_idle_does_not_start_or_close(self):
        self.assertIsNone(self.step(None))
        self.launch.assert_not_called()
        self.close.assert_not_called()

    def test_close_requests_graceful_exit_once_in_status(self):
        self.step((7, 80))
        self.running.return_value = True
        self.assertEqual(self.step(None, 1)["event"], "panel_close_requested")
        self.assertIsNone(self.step(None, 2))
        self.assertTrue(self.gate.closing)
        self.running.return_value = False
        self.step(None, 3)
        self.assertFalse(self.gate.closing)
        self.assertEqual(self.launch.call_count, 1)

    def test_fast_reopen_waits_for_previous_process_to_exit(self):
        self.step((7, 80))
        self.running.return_value = True
        self.step(None, 1)
        for second in range(2, 7):
            self.assertIsNone(self.step((8, 90), second))
        self.assertEqual(self.launch.call_count, 1)
        self.running.return_value = False
        self.assertEqual(self.step((8, 90), 7)["event"], "panel_started")
        self.step((8, 90), 8)
        self.assertEqual(self.launch.call_count, 2)

    def test_close_while_python_initializes_retries_event(self):
        self.step((7, 80))
        self.running.return_value = True
        self.close.return_value = False
        self.assertIsNone(self.step(None, 1))
        self.assertTrue(self.gate.closing)
        self.close.return_value = True
        self.assertEqual(self.step((8, 90), 2)["event"], "panel_close_requested")
        self.assertEqual(self.launch.call_count, 1)
        self.running.return_value = False
        self.step((8, 90), 3)
        self.assertEqual(self.launch.call_count, 2)

    def test_manual_instance_is_closed_without_owned_child(self):
        self.running.return_value = True
        self.assertEqual(self.step(None)["event"], "panel_close_requested")
        self.launch.assert_not_called()

    def test_manual_dismissal_is_preserved_until_reopen(self):
        self.step((7, 80))
        for second in range(1, 10):
            self.step((7, 80), second)
        self.assertEqual(self.launch.call_count, 1)
        self.close.assert_not_called()
        self.step(None, 10)
        self.step((7, 80), 11)
        self.assertEqual(self.launch.call_count, 2)

    def test_real_windows_event_does_not_remain_signaled_after_reopen(self):
        name = "Local\\CodexPanelTest-" + uuid.uuid4().hex
        self.assertFalse(auto.request_panel_close(name))
        signal = auto.PanelCloseSignal(name)
        try:
            self.assertFalse(signal.pending())
            self.assertTrue(auto.request_panel_close(name))
            self.assertTrue(signal.pending())
        finally:
            signal.close()
        self.assertFalse(auto.request_panel_close(name))
        reopened = auto.PanelCloseSignal(name)
        try:
            self.assertFalse(reopened.pending())
        finally:
            reopened.close()

    def test_running_probe_tracks_real_mutex_lifetime(self):
        name = "Local\\CodexPanelMutexTest-" + uuid.uuid4().hex
        self.assertFalse(auto.panel_is_running(name))
        kernel = auto.kernel_api()
        mutex = kernel.CreateMutexW(None, False, name)
        try:
            self.assertTrue(auto.panel_is_running(name))
        finally:
            kernel.CloseHandle(mutex)
        self.assertFalse(auto.panel_is_running(name))

    def test_window_detection_keeps_minimized_and_ignores_closed_window(self):
        # Only this test process is accepted; the user's Codex is never manipulated.
        root = tk.Tk()
        root.title("Codex")
        root.geometry("240x70+60+60")
        try:
            root.update()
            with patch.object(auto, "desktop_pid_is_alive", side_effect=lambda pid: pid == os.getpid()):
                identity = auto.codex_window()
                self.assertIsNotNone(identity)
                root.iconify()
                root.update()
                self.assertEqual(root.state(), "iconic")
                self.assertEqual(auto.codex_window(identity), identity)
                root.withdraw()
                root.update()
                self.assertIsNone(auto.codex_window(identity))
                root.deiconify()
                root.update()
                self.assertEqual(auto.codex_window(identity), identity)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
