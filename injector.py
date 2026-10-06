"""Resonant — text injection (W3.1, W3.2, W3.3).

Replaces pyautogui + pyperclip, which silently dropped non-ASCII characters
(é, ’, —, emoji), raised `FailSafeException` when the mouse sat in a screen
corner, and slept 0.1 s per character.

    type   Unicode SendInput (KEYEVENTF_UNICODE), surrogate pairs handled
    paste  guarded clipboard paste: retry OpenClipboard, snapshot + restore
           only if the clipboard is still ours, and mark the content as
           "do not sync/history" so dictations stay out of Win+V
    auto   type for short single-line text, paste for multiline/long text

All Win32 access goes through an injected platform object, so tests never touch
the real clipboard or keyboard.
"""
import ctypes
import time

import platform_win as pw

# --------------------------------------------------------------------- structs
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTunion)]


VK_V = 0x56
MODIFIERS = (pw.VK_SHIFT, pw.VK_CONTROL, pw.VK_MENU, pw.VK_LWIN, pw.VK_RWIN)

PASTE_SETTLE_S = 0.18        # time for the target app to consume the paste
PASTE_RESTORE_WAIT_S = 0.3
MODIFIER_WAIT_S = 0.6
TYPE_BATCH = 32
MULTILINE_THRESHOLD = 120    # chars; also switches to paste when text has \n


def code_units(text):
    """UTF-16 code units for `text` (astral chars become surrogate pairs)."""
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _key_inputs(units):
    """One KEYEVENTF_UNICODE down+up pair per code unit (W3.1)."""
    out = []
    for unit in units:
        for flags in (pw.KEYEVENTF_UNICODE,
                      pw.KEYEVENTF_UNICODE | pw.KEYEVENTF_KEYUP):
            item = INPUT()
            item.type = pw.INPUT_KEYBOARD
            item.u.ki = KEYBDINPUT(0, unit, flags, 0, 0)
            out.append(item)
    return out


def _vk_inputs(vk):
    out = []
    for flags in (0, pw.KEYEVENTF_KEYUP):
        item = INPUT()
        item.type = pw.INPUT_KEYBOARD
        item.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        out.append(item)
    return out


def _send(platform, inputs):
    """Send a list of INPUTs. Returns True only if Windows accepted them all."""
    if not inputs:
        return False
    try:
        arr = (INPUT * len(inputs))(*inputs)
        sent = platform.user32.SendInput(len(inputs), ctypes.byref(arr),
                                         ctypes.sizeof(INPUT))
        return int(sent) == len(inputs)
    except Exception:
        return False


def type_text(platform, text, append_trailing_space=True):
    """Unicode typing. Non-ASCII survives; no per-character sleep."""
    body = text + (" " if append_trailing_space else "")
    units = code_units(body)
    ok = True
    for i in range(0, len(units), TYPE_BATCH):
        ok = _send(platform, _key_inputs(units[i:i + TYPE_BATCH])) and ok
    return ok


def paste_text(platform, text, append_trailing_space=True, sleep=time.sleep):
    """Guarded clipboard paste. Returns (ok, detail)."""
    body = text + (" " if append_trailing_space else "")
    previous = None
    try:
        previous = platform.clipboard_get_text()
    except Exception:
        previous = None
    if not platform.clipboard_set_text(body, exclude_history=True):
        return False, "clipboard busy"
    ours = None
    try:
        ours = platform.clipboard_sequence_number()
    except Exception:
        ours = None
    ctrl = _vk_inputs(pw.VK_CONTROL)
    _send(platform, ctrl[:1] + _vk_inputs(VK_V) + ctrl[1:])
    sleep(PASTE_SETTLE_S)
    # Restore only if nothing else copied in the meantime (never clobber).
    try:
        if previous is not None and (ours is None
                                     or platform.clipboard_sequence_number() == ours):
            platform.clipboard_set_text(previous, exclude_history=True)
    except Exception:
        pass
    return True, "pasted"


def wait_for_modifiers_released(platform, timeout=MODIFIER_WAIT_S, sleep=time.sleep):
    """Shift/Alt/Win held during a paste would turn Ctrl+V into another shortcut."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not any(platform.get_async_key_state(vk) for vk in MODIFIERS):
                return True
        except Exception:
            return True
        sleep(0.02)
    return False


class ForegroundGuard:
    """Snapshot the focused window at key-down; refuse to inject elsewhere (W3.2)."""

    def __init__(self, platform, own_hwnd=None):
        self.platform = platform
        self.own_hwnd = own_hwnd
        self.hwnd = None
        self.image = ""

    def snapshot(self):
        try:
            self.hwnd = self.platform.get_foreground_window()
            pid = self.platform.get_window_thread_process_id(self.hwnd)
            self.image = self.platform.process_image_name(pid) or ""
        except Exception:
            self.hwnd, self.image = None, ""
        return self.hwnd

    def still_valid(self):
        if self.hwnd is None:
            return True          # nothing to compare against: don't block the user
        try:
            now = self.platform.get_foreground_window()
        except Exception:
            return True
        if self.own_hwnd and now == self.own_hwnd:
            return True
        return now == self.hwnd


class Injector:
    """Decides type vs paste vs clipboard-only, and reports what happened."""

    def __init__(self, platform=None, sleep=time.sleep):
        self.platform = platform
        self.sleep = sleep

    def _plat(self):
        if self.platform is None:
            self.platform = pw.real_platform()
        return self.platform

    def inject(self, text, mode="auto", append_trailing_space=True, guard=None):
        """Returns (status, detail): 'typed' | 'copied' | 'failed'."""
        text = text or ""
        if not text.strip():
            return "dropped", "empty text"
        platform = self._plat()

        if guard is not None and not guard.still_valid():
            status, detail = self._clipboard_only(platform, text, append_trailing_space)
            if status == "copied":
                return "copied", f"window changed — {detail}"
            return status, detail

        wait_for_modifiers_released(platform, sleep=self.sleep)

        if mode == "paste" or (mode == "auto" and self._needs_paste(text)):
            ok, detail = paste_text(platform, text, append_trailing_space, self.sleep)
            if ok:
                return "typed", detail
            if mode == "paste":
                return self._clipboard_only(platform, text, append_trailing_space)
            # auto: a busy clipboard is not fatal — fall through to typing

        if mode != "paste":
            try:
                if type_text(platform, text, append_trailing_space):
                    return "typed", "typed"
            except Exception as e:
                detail = str(e)
                if mode == "type":
                    ok, _ = paste_text(platform, text, append_trailing_space, self.sleep)
                    if not ok:
                        return self._clipboard_only(platform, text,
                                                    append_trailing_space)
                    return "typed", f"pasted after type error ({detail})"
        return self._clipboard_only(platform, text, append_trailing_space)

    @staticmethod
    def _needs_paste(text):
        body = text.strip()
        return "\n" in body or len(body) > MULTILINE_THRESHOLD

    def _clipboard_only(self, platform, text, append_trailing_space):
        """Last-resort path: return (status, detail) — never lose the text."""
        body = text + (" " if append_trailing_space else "")
        try:
            if platform.clipboard_set_text(body, exclude_history=True):
                return "copied", "text is on your clipboard"
        except Exception as e:
            return "failed", f"clipboard failed ({e})"
        return "failed", "could not reach the clipboard"
