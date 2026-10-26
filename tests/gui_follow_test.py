"""Exercise route log -> follower -> GUI queue -> usage reader as one chain."""
import json
import time
import tkinter as tk
from test_usage import panel, WORK, event, line, usage
from test_follow import A, B, APP_ID, navigation
from test_speed import generated, settled, started

base = WORK / "gui-follow-fixtures" / APP_ID
day = base / "sessions" / "2026" / "10" / "23"
day.mkdir(parents=True, exist_ok=True)
logs = base / "logs" / "2026" / "10" / "23"
logs.mkdir(parents=True, exist_ok=True)
route = logs / f"codex-desktop-{APP_ID}-47300-t0-i1-072623-0.log"
route.write_text(navigation("/local/" + A), encoding="utf-8")
paths = {}
for session, total in ((A, usage()), (B, usage(4000, 3600, 400, 100))):
    path = day / f"rollout-example-{session}.jsonl"
    speed_usage = settled(output=total["output_tokens"])
    speed_usage["payload"]["thread_id"] = session
    path.write_bytes(line({"type": "session_meta", "payload": {"id": session}}) + line(started())
                     + line(generated()) + line(speed_usage) + line(event(total, total)))
    paths[session] = path

config = {"default_session": str(paths[A]), "auto_follow": True,
          "sessions_root": str(base / "sessions"), "desktop_logs_root": str(base / "logs"),
          "refresh_ms": 250, "follow_window_id": 1}
real_follower = panel.DesktopSessionFollower
panel.DesktopSessionFollower = lambda *args, **kwargs: real_follower(
    *args, **kwargs, alive=lambda pid: pid == 47300, foreground=lambda: 47300)
panel.enable_dpi()
root = tk.Tk()
failures = []
root.report_callback_exception = lambda *args: failures.append(str(args))
app = panel.UsagePanel(root, config, base / "state")


def until(condition, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if condition():
            return
        time.sleep(0.025)
    raise AssertionError("GUI did not reach expected state: " + repr(app.snapshot))


def navigate(path, second):
    with route.open("a", encoding="utf-8") as stream:
        stream.write(navigation(path, second))


try:
    until(lambda: app.snapshot.session_id == A and app.snapshot.total.total_tokens == 1100)
    assert app.snapshot.speed.tokens_per_second == 25
    old_generation = app.generation
    navigate("/local/" + B, 2)
    until(lambda: app.snapshot.session_id == B and app.snapshot.total.total_tokens == 4400)
    assert app.snapshot.total.cache_rate == 90
    assert app.snapshot.speed.tokens_per_second == 100
    # Late output from the prior reader must never overwrite the selected chat.
    app.queue.put_nowait((old_generation, panel.Snapshot(session_id=A, total=panel.Usage.parse(usage()))))
    root.update()
    until(lambda: app.queue.empty())
    assert app.snapshot.session_id == B and app.snapshot.total.total_tokens == 4400
    navigate("/local/" + A, 3)
    until(lambda: app.snapshot.session_id == A and app.snapshot.total.total_tokens == 1100)
    navigate("/remote/host/" + B, 4)
    until(lambda: "远程" in app.snapshot.message)
    assert app.state()[0] == "等待会话" and not app.snapshot.error
    assert app.snapshot.total.total_tokens is None
    assert not app.snapshot.session_id
    assert app.snapshot.speed.tokens_per_second is None
    navigate("/local/" + B, 5)
    until(lambda: app.snapshot.session_id == B and app.snapshot.total.total_tokens == 4400)
    with paths[B].open("ab") as stream:
        next_speed = settled(10.1, 300, "response-two")
        next_speed["payload"]["thread_id"] = B
        stream.write(line(generated(8, 10)) + line(next_speed))
        stream.write(line(event(usage(5000, 4500, 600, 100))))
    until(lambda: app.snapshot.total.total_tokens == 5600)
    assert app.snapshot.speed.tokens_per_second == 150
    assert not failures, failures
    print(json.dumps({"gui_follow": "passed", "route_switch": "A -> B -> A -> remote -> B",
                      "stale_snapshot": "ignored", "unknown_route": "old totals cleared",
                      "incremental_usage": "4400 -> 5600 tokens", "cache_percent": 90,
                      "speed_switch": "25 -> 100 -> unknown -> 100 -> 150 tok/s"}, ensure_ascii=False))
finally:
    app.close()
