"""Installation-scoped, fixed-action Windows events; no arbitrary command payloads."""
from app_runtime import instance_suffix


ACTIONS = ("hide", "show", "recenter", "rediscover", "reload", "retry_save")


def event_name(action, suffix=None):
    if action not in ACTIONS:
        raise ValueError("Unknown panel action: " + action)
    return "Local\\CodexUsageControl-" + (suffix or instance_suffix()) + "-" + action


def request(action, suffix=None):
    from codex_autostart import request_panel_close
    return request_panel_close(event_name(action, suffix))


class PanelCommands:
    def __init__(self, suffix=None):
        from codex_autostart import kernel_api
        import ctypes
        self.kernel = kernel_api()
        self.handles = {}
        try:
            for action in ACTIONS:
                handle = self.kernel.CreateEventW(None, False, False, event_name(action, suffix))
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles[action] = handle
        except Exception:
            self.close()
            raise

    def pending(self):
        return [action for action, handle in self.handles.items()
                if self.kernel.WaitForSingleObject(handle, 0) == 0]

    def close(self):
        for handle in self.handles.values():
            self.kernel.CloseHandle(handle)
        self.handles.clear()

