import json
from datetime import datetime, timedelta, timezone
import unittest
from test_usage import panel, WORK, usage, event, line
from generation_speed import SpeedTracker, speed_label

SESSION = "00000000-0000-4000-8000-000000000001"
TURN = "turn-one"
BASE = datetime(2026, 10, 23, 8, tzinfo=timezone.utc)


def stamp(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record(kind, seconds, payload):
    return {"type": kind, "timestamp": stamp(seconds), "payload": payload}


def started(turn=TURN, seconds=0):
    return record("event_msg", seconds, {"type": "task_started", "turn_id": turn})


def generated(start=1, end=5, kind="AgentMessage", turn=TURN):
    return record("event_msg", end, {"type": "item_completed", "turn_id": turn,
                  "item": {"type": kind, "id": "item-example"},
                  "started_at_ms": round((BASE + timedelta(seconds=start)).timestamp() * 1000),
                  "completed_at_ms": round((BASE + timedelta(seconds=end)).timestamp() * 1000)})


def settled(seconds=5.1, output=1000, response="response-one", turn=TURN):
    return record("token_usage_record", seconds, {"thread_id": SESSION, "turn_id": turn,
                  "response_id": response, "usage": usage(100000, 90000, output, 300)})


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.tracker = SpeedTracker()
        self.tracker.accept(record("session_meta", 0, {"id": SESSION}))
        self.tracker.accept(started())

    def sample(self):
        self.tracker.accept(generated())
        self.tracker.accept(settled())
        return self.tracker.sample

    def test_output_only_and_wait_before_first_item_excluded(self):
        sample = self.sample()
        self.assertEqual(sample.tokens_per_second, 250)
        self.assertEqual(sample.duration_ms, 4000)
        self.assertEqual(speed_label(sample), "≈250.0")

    def test_tool_execution_and_late_token_count_do_not_slow_rate(self):
        self.sample()
        self.tracker.accept(record("response_item", 65, {"type": "custom_tool_call_output"}))
        delayed = event(usage(100000, 90000, 1000, 300))
        delayed["timestamp"] = stamp(65.1)
        self.tracker.accept(delayed)
        self.assertEqual(self.tracker.sample.tokens_per_second, 250)

    def test_tool_arguments_are_timed_until_generation_ends(self):
        self.tracker.accept(generated(1, 3, "Reasoning"))
        self.tracker.accept(record("response_item", 6, {"type": "custom_tool_call"}))
        self.tracker.accept(settled(6.1, 1000))
        self.assertEqual(self.tracker.sample.tokens_per_second, 200)

    def test_new_turn_clears_previous_reply_speed(self):
        self.sample()
        self.tracker.accept(started("turn-two", 10))
        self.assertIsNone(self.tracker.sample.tokens_per_second)

    def test_duplicate_usage_does_not_destroy_next_call_timing(self):
        self.sample()
        self.tracker.accept(generated(8, 10))
        self.tracker.accept(settled())
        self.tracker.accept(settled(10.1, 600, "response-two"))
        self.assertEqual(self.tracker.sample.tokens_per_second, 300)

    def test_missing_timing_does_not_reuse_prior_rate(self):
        self.sample()
        self.tracker.accept(settled(12, 600, "response-two"))
        self.assertIsNone(self.tracker.sample.tokens_per_second)
        self.assertEqual(speed_label(self.tracker.sample), "—")

    def test_invalid_timestamps_or_negative_interval_are_unknown(self):
        for row in (generated(5, 1), generated(1, 1)):
            self.tracker.accept(row)
        row = settled()
        row["timestamp"] = "invalid"
        self.tracker.accept(row)
        self.assertIsNone(self.tracker.sample.tokens_per_second)

    def test_other_thread_and_late_prior_turn_do_not_change_rate(self):
        self.sample()
        self.tracker.accept(generated(8, 10, turn="other-turn"))
        self.tracker.accept(settled(10.1, 600, "response-two", turn="other-turn"))
        foreign = settled(11, 600, "response-three")
        foreign["payload"]["thread_id"] = "different-session"
        self.tracker.accept(foreign)
        self.assertEqual(self.tracker.sample.tokens_per_second, 250)

    def test_stale_item_timestamp_does_not_become_new_generation(self):
        self.sample()
        self.tracker.accept(generated())
        self.tracker.accept(settled(12, 600, "response-two"))
        self.assertIsNone(self.tracker.sample.tokens_per_second)

    def test_tool_item_duration_is_never_generation_time(self):
        self.tracker.accept(generated(1, 61, "CommandExecution"))
        self.tracker.accept(settled(62))
        self.assertIsNone(self.tracker.sample.tokens_per_second)

    def test_missing_output_count_does_not_use_input_total(self):
        self.tracker.accept(generated())
        row = settled()
        del row["payload"]["usage"]["output_tokens"]
        self.tracker.accept(row)
        self.assertIsNone(self.tracker.sample.tokens_per_second)

    def test_incremental_jsonl_waits_for_complete_usage_record_and_resets(self):
        path = WORK / "test-fixtures" / "speed-incremental.jsonl"
        path.write_bytes(line(started()) + line(generated()))
        tailer = panel.LogTailer(path)
        self.assertIsNone(tailer.poll().speed.tokens_per_second)
        partial = line(settled())
        with path.open("ab") as stream:
            stream.write(partial[:-1])
        self.assertIsNone(tailer.poll().speed.tokens_per_second)
        with path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(tailer.poll().speed.tokens_per_second, 250)
        path.write_bytes(line(started("turn-two", 70)))
        self.assertIsNone(tailer.poll().speed.tokens_per_second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
