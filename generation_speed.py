"""Estimate a completed model response's rate from local output timing records."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime


def nonnegative_integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def timestamp_ms(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return round(parsed.timestamp() * 1000) if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class SpeedSample:
    output_tokens: int | None = None
    duration_ms: int | None = None
    timestamp: str = ""
    turn_id: str = ""
    response_id: str = ""

    @property
    def tokens_per_second(self):
        if self.output_tokens is None or not self.duration_ms or self.duration_ms < 0:
            return None
        return self.output_tokens * 1000.0 / self.duration_ms


class SpeedTracker:
    """Use generated output only; never time tool execution or count input tokens."""
    def __init__(self):
        self.sample = SpeedSample()
        self.turn_id = ""
        self.session_id = ""
        self.start_ms = None
        self.end_ms = None
        self.boundary_ms = None
        self.seen = deque(maxlen=512)

    def clear_window(self):
        self.start_ms = self.end_ms = None

    def accept(self, record):
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            return
        payload = record["payload"]
        kind = record.get("type")
        event = payload.get("type")
        at_ms = timestamp_ms(record.get("timestamp"))
        if kind == "session_meta":
            self.session_id = str(payload.get("id", ""))
            return
        if kind == "event_msg" and event == "task_started":
            turn_id = str(payload.get("turn_id", ""))
            if turn_id and turn_id == self.turn_id:
                return
            self.turn_id = turn_id
            self.sample = SpeedSample()
            self.boundary_ms = at_ms
            self.clear_window()
            self.seen.clear()
            return
        if payload.get("thread_id") and self.session_id and payload["thread_id"] != self.session_id:
            return
        if payload.get("turn_id") and self.turn_id and payload["turn_id"] != self.turn_id:
            return
        if kind == "event_msg" and event == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") not in ("Reasoning", "AgentMessage"):
                return
            start = nonnegative_integer(payload.get("started_at_ms"))
            end = nonnegative_integer(payload.get("completed_at_ms"))
            if start is None or end is None or end <= start:
                return
            if self.boundary_ms is not None and start < self.boundary_ms:
                return
            self.start_ms = start if self.start_ms is None else min(self.start_ms, start)
            self.end_ms = end if self.end_ms is None else max(self.end_ms, end)
        elif kind == "response_item":
            generated = event in ("reasoning", "function_call", "custom_tool_call")
            generated |= event == "message" and payload.get("role") == "assistant"
            if generated and at_ms is not None and (self.boundary_ms is None or at_ms >= self.boundary_ms):
                self.end_ms = at_ms if self.end_ms is None else max(self.end_ms, at_ms)
        elif kind == "token_usage_record":
            identifier = payload.get("response_id") or str(record.get("timestamp", ""))
            if identifier in self.seen:
                return
            self.seen.append(identifier)
            usage = payload.get("usage")
            output = nonnegative_integer(usage.get("output_tokens")) if isinstance(usage, dict) else None
            duration = None
            if (at_ms is not None and self.start_ms is not None and self.end_ms is not None
                    and self.start_ms < self.end_ms <= at_ms):
                duration = self.end_ms - self.start_ms
            # Missing timing makes this sample unknown, not the prior call's rate.
            self.sample = SpeedSample(output, duration, str(record.get("timestamp", "")),
                                      str(payload.get("turn_id", self.turn_id)), str(identifier))
            self.boundary_ms = at_ms
            self.clear_window()


def speed_label(sample):
    rate = sample.tokens_per_second
    if rate is None:
        return "—"
    if rate >= 1000:
        return f"≈{rate / 1000:.1f}K"
    return f"≈{rate:.1f}"
