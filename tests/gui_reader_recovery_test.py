"""Fault injection for reader recovery; all logs, threads and windows are isolated."""
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_usage import panel, WORK, event, line, usage
from test_follow import A, B, APP_ID, navigation


BASE = WORK / "reader-recovery" / uuid4().hex[:8]


class ReaderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base = BASE / self._testMethodName
        self.day = self.base / "sessions" / "2026" / "10" / "23"
        self.logs = self.base / "logs" / "2026" / "10" / "23"
        self.day.mkdir(parents=True)
        self.logs.mkdir(parents=True)
        self.route = self.logs / f"codex-desktop-{APP_ID}-47300-t0-i1-072623-0.log"
        self.route.write_text(navigation("/local/" + A), encoding="utf-8")
        self.paths = {}
        for session, total in ((A, usage()), (B, usage(4000, 3600, 400, 100))):
            path = self.day / f"rollout-example-{session}.jsonl"
            path.write_bytes(line({"type": "session_meta", "payload": {"id": session}})
                             + line(event(total, total)))
            self.paths[session] = path
        self.config = {"default_session": str(self.paths[A]), "auto_follow": True,
                       "sessions_root": str(self.base / "sessions"), "refresh_ms": 250,
                       "desktop_logs_root": str(self.base / "logs"), "follow_window_id": 1}
        self.app = self.root = None
        self.errors = []
        self.follow_fault = self.init_fault = self.usage_fault = False
        self.exit_follow = self.exit_usage = False
        self.created_followers = 0
        real_follower, real_poll = panel.DesktopSessionFollower, panel.LogTailer.poll
        owner = self

        class Follower(real_follower):
            def poll(self):
                if owner.exit_follow:
                    owner.exit_follow = False
                    raise SystemExit()  # Simulates a worker that has actually terminated.
                if owner.follow_fault:
                    raise RuntimeError("injected navigation failure")
                return super().poll()

        def create_follower(*args, **kwargs):
            self.created_followers += 1
            if self.init_fault:
                raise RuntimeError("injected follower initialization failure")
            return Follower(*args, **kwargs, alive=lambda pid: pid == 47300, foreground=lambda: 47300)

        def poll_usage(reader):
            if self.exit_usage:
                self.exit_usage = False
                raise SystemExit()
            result = real_poll(reader)
            if self.usage_fault:
                raise RuntimeError("injected failure after consuming a usage batch")
            return result

        self.patches = [patch.object(panel, "DesktopSessionFollower", side_effect=create_follower),
                        patch.object(panel.LogTailer, "poll", poll_usage)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        if self.app:
            workers = (self.app.worker, self.app.follow_worker)
            self.app.close()
            for worker in workers:
                if worker:
                    worker.join(2)
                    self.assertFalse(worker.is_alive(), "test reader did not stop")
        for item in reversed(self.patches):
            item.stop()
        self.assertFalse(self.errors)

    def start(self):
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *args: self.errors.append(str(args))
        self.app = panel.UsagePanel(self.root, self.config, self.base / "state")

    def until(self, condition, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(0.02)
        self.fail("Did not reach expected state: " + repr(self.app.snapshot))

    def ready(self, session=A, total=1100):
        self.until(lambda: self.app.snapshot.session_id == session
                   and self.app.snapshot.total.total_tokens == total and not self.app.snapshot.error)

    def navigate(self, route, second=2):
        with self.route.open("a", encoding="utf-8") as stream:
            stream.write(navigation(route, second))

    def error_log(self):
        path = self.app.state_dir / "panel-error.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_navigation_failure_recovers_and_switches(self):
        self.start()
        self.ready()
        self.follow_fault = True
        self.until(lambda: self.app.state()[0] == "读取失败")
        self.assertIn("injected navigation failure", self.app.snapshot.error)
        self.assertIsNone(self.app.snapshot.total.total_tokens)
        self.navigate("/local/" + B)
        self.follow_fault = False
        self.ready(B, 4400)
        self.assertIn("[自动跟随] 已恢复", self.error_log())

    def test_follower_initialization_failure_is_retried(self):
        self.init_fault = True
        self.start()
        self.until(lambda: "initialization failure" in self.app.snapshot.error)
        self.init_fault = False
        self.ready()
        self.assertGreaterEqual(self.created_followers, 2)

    def test_usage_batch_failure_replays_without_losing_counts(self):
        self.usage_fault = True
        self.start()
        self.until(lambda: "consuming a usage batch" in self.app.snapshot.error)
        self.assertEqual(self.app.state()[0], "读取失败")
        self.assertEqual(self.app.snapshot.session_id, A)
        self.usage_fault = False
        self.ready()
        with self.paths[A].open("ab") as stream:
            stream.write(line(event(usage(5000, 4500, 600, 100))))
        self.ready(A, 5600)
        self.assertIn("[用量读取] 已恢复", self.error_log())

    def test_dead_navigation_worker_restarts_and_follows_new_chat(self):
        self.start()
        self.ready()
        previous = self.app.follow_worker
        self.exit_follow = True
        self.navigate("/local/" + B)
        self.ready(B, 4400)
        self.assertIsNot(self.app.follow_worker, previous)
        self.assertFalse(previous.is_alive())
        self.assertIn("自动跟随线程意外停止", self.error_log())
        self.assertIn("[自动跟随] 已恢复", self.error_log())

    def test_dead_usage_worker_restarts_and_rejects_old_generation(self):
        self.start()
        self.ready()
        previous, generation = self.app.worker, self.app.generation
        self.exit_usage = True
        self.until(lambda: self.app.worker is not previous)
        self.ready()
        self.app.queue.put_nowait((generation, panel.Snapshot(session_id=B, total=panel.Usage.parse(usage(99)))))
        self.until(lambda: self.app.queue.empty())
        self.assertEqual(self.app.snapshot.session_id, A)
        self.assertEqual(self.app.snapshot.total.total_tokens, 1100)
        self.assertIn("用量读取线程意外停止", self.error_log())

    def test_refresh_failure_does_not_cancel_future_ticks(self):
        self.start()
        self.ready()
        with patch.object(self.app, "ensure_visible", side_effect=RuntimeError("injected UI failure")):
            self.until(lambda: "injected UI failure" in self.error_log())
        self.navigate("/local/" + B)
        self.ready(B, 4400)
        self.assertIn("[界面刷新] 已恢复", self.error_log())

    def test_old_data_and_no_local_session_do_not_trigger_restarts(self):
        self.start()
        self.ready()
        follower, reader, generation = self.app.follow_worker, self.app.worker, self.app.generation
        for _ in range(4):
            self.app.refresh_state()
        self.assertIs(self.app.follow_worker, follower)
        self.assertIs(self.app.worker, reader)
        self.assertEqual(self.app.generation, generation)
        self.navigate("/remote/host/" + B)
        self.until(lambda: self.app.state()[0] == "等待会话")
        self.until(lambda: not reader.is_alive())
        for _ in range(4):
            self.app.refresh_state()
        self.assertIs(self.app.worker, reader)
        self.assertFalse(self.error_log())
        self.navigate("/local/" + A, 3)
        self.ready()

    def test_identical_failure_logs_are_throttled(self):
        self.start()
        self.ready()
        error = RuntimeError("repeated test fault")
        for _ in range(20):
            self.app.report_reader_error("诊断测试", error)
        self.app.report_reader_recovered("诊断测试")
        self.app.report_reader_recovered("诊断测试")
        content = self.error_log()
        self.assertEqual(content.count("repeated test fault"), 1)
        self.assertEqual(content.count("[诊断测试] 已恢复"), 1)


if __name__ == "__main__":
    panel.enable_dpi()
    unittest.main(verbosity=2)
