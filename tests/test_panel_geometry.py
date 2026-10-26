"""Regression test of the actual Tk window geometry, with isolated settings."""
import json
import tkinter as tk
from test_usage import panel, WORK, event, usage, line

base = WORK / "geometry-fixtures"
base.mkdir(parents=True, exist_ok=True)
source = base / "usage.jsonl"
source.write_bytes(line(event(usage())))
config = panel.load_config()
panel.enable_dpi()
root = tk.Tk()
errors = []
root.report_callback_exception = lambda *args: errors.append(str(args))
app = panel.UsagePanel(root, config, base / "state", str(source))
results = []


def position():
    root.update_idletasks()
    return root.winfo_x(), root.winfo_y()


def place(x, y):
    app.set_expanded(False)
    root.geometry(f"{app.px(app.WIDTH)}x{app.px(app.BAR_HEIGHT)}+{x}+{y}")
    root.update()
    return position()


try:
    left, top, right, bottom = panel.work_area(root)
    x = left + 100
    for label, y in (("top", top + 3), ("middle", (top + bottom) // 2),
                     ("bottom", bottom - app.px(app.BAR_HEIGHT) - 3)):
        original = place(x, y)
        for _ in range(8):
            app.set_expanded(True)
            expanded = position()
            assert expanded[1] >= top
            assert expanded[1] + root.winfo_height() <= bottom
            if label == "top":
                assert expanded == original, (expanded, original)
            app.set_expanded(False)
            assert position() == original, (label, position(), original)
        results.append({"case": label, "cycles": 8, "restored": original})

    original = place(x, top + 3)
    app.set_expanded(True)
    app.toggle_mode()
    saved = json.loads((base / "state/settings.json").read_text(encoding="utf-8"))
    assert (saved["x"], saved["y"]) == original
    app.select_session(source)
    saved = json.loads((base / "state/settings.json").read_text(encoding="utf-8"))
    assert (saved["x"], saved["y"]) == original
    results.append({"case": "save while expanded", "saved": original})

    # An intentional move of the expanded window establishes a new compact anchor.
    root.geometry(f"+{x+40}+{top+60}")
    root.update()
    app.drag_origin = (0, 0, x, top + 3)
    app.drag_end(None)
    app.set_expanded(False)
    assert position() == (x + 40, top + 60)
    results.append({"case": "drag expanded then collapse", "restored": position()})

    original = place(x, top + 3)
    app.set_expanded(True)
    app.close()
    saved = json.loads((base / "state/settings.json").read_text(encoding="utf-8"))
    assert (saved["x"], saved["y"]) == original
    results.append({"case": "exit while expanded", "saved": original})
    assert not errors, errors
    print(json.dumps({"geometry": "passed", "cases": results}, ensure_ascii=False))
finally:
    if not app.closed:
        app.close()
