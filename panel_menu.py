"""Theme-colored popup surfaces for existing Tk menu models, without native white frames."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
import tkinter as tk


def menu_border(theme):
    # One quiet edge, rather than Windows' bright three-dimensional menu frame.
    return "#" + "".join(f"{round(int(theme.surface[i:i+2], 16)*.6 + int(theme.border[i:i+2], 16)*.4):02x}"
                          for i in (1, 3, 5))


def menu_position(x, y, width, height, area, owner, gap, parent=None):
    """Keep cascades connected and avoid the panel when either vertical side fits."""
    left, top, right, bottom = area
    if parent and x + width > right:
        x = parent[0] - width + 1
    x, y = max(left, min(x, right-width)), max(top, min(y, bottom-height))
    ox1, oy1, ox2, oy2 = owner
    if x + width <= ox1 or x >= ox2 or y >= oy2 + gap or y + height <= oy1 - gap:
        return x, y
    above, below = oy1 - gap - height, oy2 + gap
    if parent:
        # Stay on the same side as the parent; jumping across the panel would
        # leave a gap that the pointer cannot cross without closing the cascade.
        if parent[3] <= oy1 and above >= top:
            y = min(y, above)
        elif parent[1] >= oy2 and below + height <= bottom:
            y = max(y, below)
    elif below + height <= bottom:
        y = below
    elif above >= top:
        y = above
    # If neither side fits, retain the bounded popup and its existing scrolling.
    # Native ownership still keeps every visible menu row above the panel.
    return x, y


@lru_cache(maxsize=1)
def _window_api():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    return user


def _frame(window):
    return int(str(window.tk.call("wm", "frame", window)), 0)


def _bounds(window):
    x, y = window.winfo_x(), window.winfo_y()
    return x, y, x + window.winfo_width(), y + window.winfo_height()


class ThemedMenu:
    def __init__(self, owner, model, theme, font, scale, area):
        self.owner, self.model, self.theme, self.font = owner, model, theme, font
        self.scale, self.area = scale, area
        self.windows = []
        self.pending = None
        self.focus_job = None
        self.stack_job = None

    def show(self, x, y):
        previous_focus = self.owner.focus_get()
        try:
            first = self.open(self.model, x, y)
            first.window.grab_set()
            first.window.focus_force()
            self.owner.wait_window(first.window)
        finally:
            self.dismiss()
        # Destroy menu windows and release the grab before commands can change
        # theme, open a dialog, or close the owning panel.
        try:
            owner_exists = self.owner.winfo_exists()
        except tk.TclError:
            return  # Tk itself is gone after an external panel shutdown.
        if owner_exists:
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()
            if self.pending:
                model, index = self.pending
                model.invoke(index)

    def open(self, model, x, y, parent=None):
        node = _MenuWindow(self, model, parent)
        x, y = menu_position(x, y, node.width, node.height, self.area, _bounds(self.owner),
                             max(4, round(8*self.scale)), _bounds(parent.window) if parent else None)
        # Explicit '+' keeps negative desktop coordinates absolute, as on the panel.
        node.window.geometry(f"{node.width}x{node.height}+{x}+{y}")
        self.windows.append(node)
        node.window.deiconify()
        node.window.update_idletasks()
        if os.name == "nt":
            # Tk's master is not a native owner for override-redirect popups.
            # Set GWLP_HWNDPARENT on the real WS_POPUP wrapper, preserving its
            # style and the panel's pin setting (transient() changes the style).
            ctypes.set_last_error(0)
            previous = _window_api().SetWindowLongPtrW(
                _frame(node.window), -8, _frame(parent.window if parent else self.owner))
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        self.restack()
        return node

    def restack(self):
        if self.stack_job is not None:
            self.owner.after_cancel(self.stack_job)
        self.stack_job = None
        for node in self.windows:
            if os.name == "nt":
                # Raise within the existing topmost/non-topmost band, without
                # activation or moving the owner's position in the stack.
                if not _window_api().SetWindowPos(_frame(node.window), 0, 0, 0, 0, 0, 0x213):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                node.window.lift()

    def schedule_restack(self, _event=None):
        if self.stack_job is None and self.windows:
            self.stack_job = self.owner.after_idle(self.restack)

    def trim(self, keep):
        while len(self.windows) > keep:
            node = self.windows.pop()
            try:
                node.window.destroy()
            except tk.TclError:
                pass  # The owner may have been closed while the menu was posted.
        self.restack()

    def dismiss(self):
        if self.stack_job is not None:
            try:
                self.owner.after_cancel(self.stack_job)
            except tk.TclError:
                pass
            self.stack_job = None
        if self.focus_job is not None:
            try:
                self.owner.after_cancel(self.focus_job)
            except tk.TclError:
                pass
            self.focus_job = None
        if self.windows:
            try:
                self.windows[0].window.grab_release()
            except tk.TclError:
                pass
        self.trim(0)

    def outside(self, event):
        if not any(node.contains(event.x_root, event.y_root) for node in self.windows):
            self.dismiss()
            return "break"

    def focus_changed(self, _event):
        if self.focus_job is None and self.windows:
            self.focus_job = self.owner.after_idle(self.check_focus)

    def check_focus(self):
        self.focus_job = None
        if self.windows:
            focused = self.owner.focus_displayof()
            if focused is None or all(focused.winfo_toplevel() != node.window for node in self.windows):
                self.dismiss()


class _MenuWindow:
    def __init__(self, popup, model, parent):
        self.popup, self.model, self.parent = popup, model, parent
        self.depth = len(popup.windows)
        self.active = None
        self.padding = max(4, round(5*popup.scale))
        self.row_height = max(popup.font.metrics("linespace") + 10, round(26*popup.scale))
        last = model.index("end")
        self.rows = []
        position = self.padding
        labels = []
        for index in range(last + 1 if last is not None else 0):
            kind = model.type(index)
            height = max(7, round(9*popup.scale)) if kind == "separator" else self.row_height
            label = "" if kind == "separator" else model.entrycget(index, "label")
            enabled = kind != "separator" and model.entrycget(index, "state") != "disabled"
            selected = (kind == "radiobutton" and
                        str(model.getvar(model.entrycget(index, "variable"))) == model.entrycget(index, "value"))
            self.rows.append((position, position+height, kind, label, enabled, selected))
            labels.append(label)
            position += height
        self.content_height = position + self.padding
        left, top, right, bottom = popup.area
        self.gutter = max(24, round(27*popup.scale))
        self.width = min(right-left, max(128, max((popup.font.measure(s) for s in labels), default=0) + 2*self.gutter + 2))
        self.height = min(bottom-top, self.content_height + 2)
        self.window = tk.Toplevel(parent.window if parent else popup.owner)
        self.window.withdraw()
        self.window.overrideredirect(True)
        # Keep a real popup; native ownership is attached after Tk maps its
        # wrapper. transient() with override-redirect produces WS_CHILD here.
        self.window.attributes("-topmost", popup.owner.attributes("-topmost"))
        self.window.configure(bg=menu_border(popup.theme), bd=0, highlightthickness=0)
        self.window.title("用量面板选项")
        self.canvas = tk.Canvas(self.window, width=self.width-2, height=self.height-2,
                                bg=popup.theme.surface, bd=0, highlightthickness=0,
                                yscrollincrement=self.row_height,
                                scrollregion=(0, 0, self.width-2, self.content_height))
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)
        self.canvas.bind("<Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.click)
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.window.bind("<ButtonPress-1>", popup.outside)
        self.window.bind("<ButtonPress-3>", popup.outside)
        self.window.bind("<KeyPress>", self.key)
        self.window.bind("<FocusOut>", popup.focus_changed)
        self.window.bind("<FocusIn>", popup.schedule_restack)
        self.draw()

    def contains(self, x, y):
        return (self.window.winfo_x() <= x < self.window.winfo_x()+self.width and
                self.window.winfo_y() <= y < self.window.winfo_y()+self.height)

    def draw(self):
        c, theme = self.canvas, self.popup.theme
        c.delete("all")
        for index, (top, bottom, kind, label, enabled, selected) in enumerate(self.rows):
            middle = (top+bottom)/2
            if kind == "separator":
                c.create_line(self.padding+4, middle, self.width-self.padding-6, middle,
                              fill=menu_border(theme))
                continue
            if index == self.active and enabled:
                c.create_rectangle(self.padding, top, self.width-self.padding-2, bottom,
                                   fill=theme.hover, outline="")
            elif selected:
                c.create_rectangle(self.padding, top, self.width-self.padding-2, bottom,
                                   fill=theme.selected, outline="")
            available = self.width-2*self.gutter
            while label and self.popup.font.measure(label) > available:
                label = label[:-2] + "…" if len(label) > 1 else ""
            c.create_text(self.gutter, middle, text=label, anchor="w", font=self.popup.font,
                          fill=theme.text if enabled else theme.muted)
            if selected:
                x = self.gutter/2
                c.create_line(x-3, middle, x-1, middle+2, x+4, middle-3,
                              fill=theme.accent, width=max(1, self.popup.scale), capstyle="round", joinstyle="round")
            if kind == "cascade":
                x = self.width-self.gutter/2-2
                c.create_line(x-2, middle-3, x+1, middle, x-2, middle+3,
                              fill=theme.muted, width=max(1, self.popup.scale))

    def hit(self, event):
        if not 0 <= event.x < self.width-2 or not 0 <= event.y < self.height-2:
            return None
        y = self.canvas.canvasy(event.y)
        return next((i for i, row in enumerate(self.rows) if row[0] <= y < row[1] and row[4]), None)

    def activate(self, index, cascade=False):
        if index != self.active:
            had_child = len(self.popup.windows) > self.depth+1
            if had_child:
                # Transfer focus before destroying a focused submenu; on Windows
                # destroying it first can temporarily deactivate the whole popup.
                self.window.focus_force()
            self.popup.trim(self.depth+1)
            self.active = index
            self.draw()
        if cascade and index is not None and self.rows[index][2] == "cascade":
            self.submenu()

    def motion(self, event):
        self.activate(self.hit(event), cascade=True)

    def click(self, event):
        self.activate(self.hit(event))
        self.choose()
        return "break"

    def submenu(self):
        if self.active is None or len(self.popup.windows) > self.depth+1:
            return
        model = self.model.nametowidget(self.model.entrycget(self.active, "menu"))
        self.popup.open(model, self.window.winfo_x()+self.width-1,
                        round(self.window.winfo_y()+self.rows[self.active][0]-self.canvas.canvasy(0)), self)

    def choose(self):
        if self.active is None:
            return
        if self.rows[self.active][2] == "cascade":
            self.submenu()
            child = self.popup.windows[-1]
            child.window.focus_set()
            child.step(1)
        else:
            self.popup.pending = (self.model, self.active)
            self.popup.dismiss()

    def step(self, amount):
        choices = [i for i, row in enumerate(self.rows) if row[4]]
        if not choices:
            return
        current = choices.index(self.active) if self.active in choices else (-1 if amount > 0 else 0)
        self.activate(choices[(current+amount) % len(choices)])
        top, bottom = self.rows[self.active][:2]
        visible = self.height-2
        offset = self.canvas.canvasy(0)
        if top < offset or bottom > offset+visible:
            self.canvas.yview_moveto(max(0, top-self.padding)/self.content_height)

    def key(self, event):
        if event.keysym in ("Up", "Down"):
            self.step(1 if event.keysym == "Down" else -1)
        elif event.keysym in ("Home", "End"):
            self.active = None
            self.step(1 if event.keysym == "Home" else -1)
        elif event.keysym in ("Return", "KP_Enter", "space"):
            self.choose()
        elif event.keysym == "Right" and self.active is not None and self.rows[self.active][2] == "cascade":
            self.choose()
        elif event.keysym in ("Escape", "Left") and self.parent:
            self.parent.window.focus_force()
            self.popup.trim(self.depth)
        elif event.keysym in ("Escape", "Tab"):
            self.popup.dismiss()
        return "break"

    def wheel(self, event):
        self.popup.trim(self.depth+1)
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"
