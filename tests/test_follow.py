import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from test_usage import panel, WORK
from active_session import DesktopSessionFollower, RouteLog, SessionResolver, classify_route, parse_route_event

A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
APP_ID = "00000000-0000-4000-8000-000000000003"


def navigation(route, second=1, window=1):
    return (f"2026-10-23T08:00:{second:02d}.000Z info [electron-message-handler] "
            "IAB_LIFECYCLE received browser sidebar owner sync "
            f"browserTabId=null conversationId=client-new-thread:any originWebContentsId=1 ownerRoutePath={route} windowId={window}\n")


class FakeResolver:
    def __init__(self):
        self.paths = {A: "A.jsonl", B: "B.jsonl"}

    def resolve(self, session_id):
        return self.paths.get(session_id, "")


class FollowTests(unittest.TestCase):
    def setUp(self):
        # Keep revised synthetic log identities separate from older test runs.
        self.base = WORK / "follow-fixtures" / APP_ID / self._testMethodName
        self.day = self.base / "logs" / "2026" / "10" / "23"
        self.day.mkdir(parents=True, exist_ok=True)
        self.path = self.day / f"codex-desktop-{APP_ID}-47300-t0-i1-072623-0.log"
        self.path.write_text("", encoding="utf-8")
        self.alive = {47300}
        self.resolver = FakeResolver()
        self.follower = DesktopSessionFollower(self.base / "logs", self.base / "sessions",
                       alive=lambda pid: pid in self.alive, foreground=lambda: 47300, resolver=self.resolver)

    def append(self, text):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(text)

    def test_switches_between_already_loaded_chats(self):
        self.append(navigation("/local/" + A))
        self.assertEqual(self.follower.poll().session_id, A)
        self.append(navigation("/local/" + B, 2))
        self.assertEqual(self.follower.poll().session_id, B)
        self.append(navigation("/local/" + A, 3))
        self.assertEqual(self.follower.poll().session_id, A)

    def test_background_usage_resume_and_reply_do_not_switch(self):
        self.append(navigation("/local/" + A))
        self.follower.poll()
        self.append(f"2026-10-23T08:00:02.000Z info [AppServerConnection] response_routed conversationId={B} method=thread/resume\n")
        self.append(f'2026-10-23T08:00:03.000Z info [electron-message-handler] Reasoning summary item completed threadId={B}\n')
        self.assertEqual(self.follower.poll().session_id, A)

    def test_embedded_route_text_in_message_is_not_navigation(self):
        self.assertIsNone(parse_route_event('2026-10-23T08:00:02Z info [message] text="' + navigation("/local/" + B) + '"'))

    def test_auxiliary_window_does_not_change_main_selection(self):
        self.append(navigation("/local/" + A))
        self.append(navigation("/local/" + B, 2, window=2))
        self.assertEqual(self.follower.poll().session_id, A)

    def test_no_chat_and_remote_do_not_keep_old_session(self):
        self.append(navigation("/local/" + A))
        self.follower.poll()
        self.append(navigation("/", 2))
        self.assertFalse(self.follower.poll().session_id)
        self.append(navigation("/remote/host/" + B, 3))
        current = self.follower.poll()
        self.assertFalse(current.path)
        self.assertIn("远程", current.message)

    def test_waits_for_complete_navigation_line(self):
        row = navigation("/local/" + A)
        self.append(row[:-1])
        self.assertFalse(self.follower.poll().session_id)
        self.append("\n")
        self.assertEqual(self.follower.poll().session_id, A)

    def test_new_session_log_can_appear_later(self):
        self.resolver.paths.pop(A)
        self.append(navigation("/local/" + A))
        self.assertFalse(self.follower.poll().path)
        self.resolver.paths[A] = "created-later.jsonl"
        self.assertEqual(self.follower.poll().path, "created-later.jsonl")

    def test_reading_and_io_failure_are_distinct_from_waiting(self):
        self.assertFalse(self.follower.poll().error)
        with patch.object(RouteLog, "poll", return_value=(None, True, "")):
            reading = self.follower.poll()
        self.assertTrue(reading.loading)
        self.assertFalse(reading.error)
        with patch.object(RouteLog, "poll", return_value=(None, False, "导航日志读取失败：拒绝访问")):
            failure = self.follower.poll()
        self.assertFalse(failure.loading)
        self.assertIn("拒绝访问", failure.error)
        self.append(navigation("/local/" + A))
        recovered = self.follower.poll()
        self.assertEqual(recovered.session_id, A)
        self.assertFalse(recovered.error)

    def test_route_reader_keeps_os_error_reason(self):
        reader = RouteLog(self.path, 1)
        with patch.object(Path, "open", side_effect=PermissionError(13, "拒绝访问")):
            self.assertIn("拒绝访问", reader.poll()[2])

    def test_process_exit_clears_route(self):
        self.append(navigation("/local/" + A))
        self.follower.poll()
        self.alive.clear()
        self.follower.last_discovery = -100000
        self.assertFalse(self.follower.poll().session_id)

    def test_rotation_uses_newest_route_timestamp(self):
        self.append(navigation("/local/" + A))
        other = self.day / f"codex-desktop-{APP_ID}-47300-t0-i1-072623-1.log"
        other.write_text(navigation("/local/" + B, 3), encoding="utf-8")
        self.assertEqual(self.follower.poll().session_id, B)

    def test_truncation_drops_previous_route(self):
        self.append(navigation("/local/" + A) * 2)
        self.follower.poll()
        self.path.write_text(navigation("/local/" + B, 3), encoding="utf-8")
        self.assertEqual(self.follower.poll().session_id, B)

    def test_query_strings_and_quoted_paths(self):
        self.assertEqual(classify_route("/local/" + A + "?view=review")[0], A)
        parsed = parse_route_event(navigation(json.dumps("/local/" + B)))
        self.assertEqual(classify_route(parsed[1])[0], B)

    def test_old_log_of_dead_process_is_ignored(self):
        self.append(navigation("/local/" + A))
        old = self.day / f"codex-desktop-{APP_ID}-99999-t0-i1-235959-0.log"
        old.write_text(navigation("/local/" + B, 59), encoding="utf-8")
        self.assertEqual(self.follower.poll().session_id, A)

    def test_database_resolves_old_sessions_read_only(self):
        root = self.base / "data" / "sessions"
        day = root / "2026" / "10" / "23"
        day.mkdir(parents=True, exist_ok=True)
        log = day / ("rollout-example-" + A + ".jsonl")
        log.write_text("", encoding="utf-8")
        db = root.parent / "state_5.sqlite"
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, rollout_path TEXT)")
            connection.execute("INSERT OR REPLACE INTO threads VALUES(?, ?)", (A, str(log)))
        before = db.read_bytes()
        self.assertEqual(SessionResolver(root).resolve(A), str(log))
        self.assertEqual(db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
