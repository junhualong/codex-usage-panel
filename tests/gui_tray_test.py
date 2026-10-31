"""Exercise the native tray window without restarting Explorer or touching Codex."""
import time
import unittest
from unittest.mock import patch
import win32gui

from test_usage import panel  # Adds the project import path.
from panel_tray import TrayIcon


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.actions = []
        self.tray = TrayIcon(self.actions.append)
        self.assertTrue(self.tray.present, self.tray.error)
        self.assertTrue(win32gui.IsWindow(self.tray.hwnd))

    def tearDown(self):
        self.tray.close()
        self.assertFalse(self.tray.thread.is_alive())
        self.assertIsNone(self.tray.hwnd)
        self.assertFalse(self.tray.present)

    def until(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.02)
        self.fail(self.tray.error or "Native tray did not reach expected state")

    def test_select_callback_and_closed_codex_menu(self):
        self.tray.set_state({"codex_open": False, "panel_running": False, "hidden": False})
        self.assertFalse(self.tray.menu_items()[0][2])
        win32gui.PostMessage(self.tray.hwnd, self.tray.CALLBACK, 0, (1 << 16) | 0x401)
        self.until(lambda: self.actions == ["settings"])

    def test_open_and_hidden_panel_menu(self):
        self.tray.set_state({"codex_open": True, "panel_running": True, "hidden": True})
        self.assertTrue(self.tray.menu_items()[0][2])
        self.assertFalse(self.tray.menu_items()[1][2])
        win32gui.PostMessage(self.tray.hwnd, self.tray.CALLBACK, 0, (1 << 16) | 0x400)
        self.until(lambda: self.actions == ["show"])

    def test_taskbar_recreated_notification_readds_icon(self):
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.tray.hwnd, 1))
        self.tray.present = False
        win32gui.PostMessage(self.tray.hwnd, self.tray.taskbar_created, 0, 0)
        self.until(lambda: self.tray.present)
        self.assertFalse(self.tray.error)

    def test_unavailable_shell_at_login_can_recover(self):
        self.tray.close()
        original = win32gui.Shell_NotifyIcon
        failed = []
        def once(message, data):
            if message == win32gui.NIM_ADD and not failed:
                failed.append(True)
                raise RuntimeError("Explorer is still starting")
            return original(message, data)
        with patch.object(win32gui, "Shell_NotifyIcon", once):
            self.tray = TrayIcon(self.actions.append)
            self.assertTrue(self.tray.thread.is_alive())
            self.assertFalse(self.tray.present)
            self.tray.set_state({"codex_open": False})
            self.until(lambda: self.tray.present)
        self.assertFalse(self.tray.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)

