"""Resonant — hotkey backends (W2.2) + emergency exit chord (W2.3).

Primary: WH_KEYBOARD_LL hook on a dedicated thread that owns a message loop
(the hook only delivers events while its installing thread pumps messages).
The callback does almost nothing (Windows silently removes slow hooks): it
validates the event, suppresses auto-repeat, posts a tuple to a queue and
returns 1 to swallow the key while listening. All real work happens on a
dispatcher thread that drains the queue.

Fallback: GetAsyncKeyState polling (the previous behaviour), used when the hook
cannot be installed (logged, notice once, cannot swallow).

Callbacks (all optional, invoked on the dispatcher thread):
    on_press()        configured key went down (fresh press, no auto-repeat)
    on_release()      key came back up
    on_emergency()    Ctrl+Shift+Scroll Lock chord detected

Known limitation (documented in README): while an ELEVATED window has focus the
hook neither sees nor swallows keys, because a low-integrity process cannot
hook a high-integrity one.
"""
import ctypes
import threading
import time

from platform_win import (
    HC_ACTION,
    LLKHF_INJECTED,
    VK_CONTROL,
    VK_SCROLL,
    VK_SHIFT,
    WM_KEYDOWN,
    WM_SYSKEYDOWN,
)

# ------------------------------------------------------------------ key map
VK_MAP = {f"f{i}": 0x70 + i - 1 for i in range(1, 25)}   # f1..f24
VK_MAP.update({"pause": 0x13, "capslock": 0x14})

ALLOWED_KEYS = sorted(VK_MAP)

HOOK_READY_TIMEOUT_S = 1.0
DISPATCH_POLL_S = 0.01
QUEUE_MAX = 64


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.c_ulong),
                ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class Hotkey:
    """Owns key interpretation. `swallow_active` is a callable -> bool so the
    decision stays current (we swallow only while listening + ready)."""

    def __init__(self, key="f9", swallow_active=lambda: False,
                 on_press=None, on_release=None, on_emergency=None,
                 platform=None):
        self.key = (key or "f9").lower()
        self.vk = VK_MAP.get(self.key)
        self.swallow_active = swallow_active
        self.on_press = on_press
        self.on_release = on_release
        self.on_emergency = on_emergency
        self.platform = platform
        self.backend = None                 # "hook" | "polling" | "invalid-key"
        self._queue = []
        self._queue_lock = threading.Lock()
        self._dispatch_thread = None
        self._hook = None
        self._hook_thread = None
        self._hook_ready = threading.Event()
        self._hook_failed = threading.Event()
        self._proc_ref = None               # keeps the callback object alive
        self._stop = threading.Event()
        self._physically_down = False       # auto-repeat suppression (hook)
        self._swallow_last_press = False    # release must match the press
        self._prev_poll_down = False        # auto-repeat suppression (poll)

    # ------------------------------------------------------------ dispatcher
    def _post(self, is_down, swallow):
        with self._queue_lock:
            if len(self._queue) >= QUEUE_MAX:          # bounded; hooks never block
                self._queue.pop(0)
            self._queue.append((is_down, swallow))
        if self._dispatch_thread is None or not self._dispatch_thread.is_alive():
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop, daemon=True, name="hotkey-dispatch")
            self._dispatch_thread.start()

    def _dispatch_loop(self):
        while not self._stop.is_set():
            item = None
            with self._queue_lock:
                if self._queue:
                    item = self._queue.pop(0)
            if item is None:
                time.sleep(DISPATCH_POLL_S)
                continue
            is_down, _swallow = item
            try:
                if is_down:
                    if self.on_press:
                        self.on_press()
                else:
                    if self.on_release:
                        self.on_release()
            except Exception:
                pass

    # ------------------------------------------------------------ LL hook
    def _ll_callback(self):
        """Returns the hook proc. Deliberately tiny: no I/O, no logging."""
        user32 = self.platform.user32

        def proc(nCode, wParam, lParam):
            try:
                if nCode == HC_ACTION:
                    kb = KBDLLHOOKSTRUCT.from_address(lParam)
                    if kb.vkCode == self.vk and not (kb.flags & LLKHF_INJECTED):
                        is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        if is_down:
                            if self._physically_down:      # auto-repeat: ignore
                                return 1 if self._swallow_last_press else 0
                            self._physically_down = True
                            swallow = bool(self.swallow_active())
                            self._swallow_last_press = swallow
                            self._post(True, swallow)
                            if swallow:
                                return 1
                        else:
                            self._physically_down = False
                            swallow = self._swallow_last_press
                            self._swallow_last_press = False
                            self._post(False, swallow)
                            if swallow:
                                return 1
            except Exception:
                pass                                   # never raise inside a hook
            return user32.CallNextHookEx(None, nCode, wParam, lParam)
        return proc

    # ------------------------------------------------------------ lifecycle
    def start(self):
        if self.vk is None:
            self.backend = "invalid-key"
            return self.backend
        self._ensure_dispatcher()
        threading.Thread(target=self._hook_thread_main, daemon=True,
                         name="hotkey-hook").start()
        self._hook_ready.wait(HOOK_READY_TIMEOUT_S)
        if self._hook is None:
            self.backend = "polling"
            threading.Thread(target=self._poll_loop, daemon=True,
                             name="hotkey-poll").start()
        else:
            self.backend = "hook"
        threading.Thread(target=self._emergency_loop, daemon=True,
                         name="hotkey-emergency").start()
        return self.backend

    def _ensure_dispatcher(self):
        if self._dispatch_thread is None or not self._dispatch_thread.is_alive():
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop, daemon=True, name="hotkey-dispatch")
            self._dispatch_thread.start()

    def _hook_thread_main(self):
        """Install the hook and pump messages — the hook only fires on the
        thread that installed it, and only while that thread pumps."""
        import ctypes.wintypes as wintypes
        try:
            proc = self._ll_callback()
            self._proc_ref = proc                # reference for process lifetime
            self._hook = self.platform.set_low_keyboard_hook(proc)
        except Exception:
            self._hook = None
        if not self._hook:
            self._hook_failed.set()
            self._hook_ready.set()
            return
        self._hook_ready.set()
        msg = wintypes.MSG()
        user32 = self.platform.user32
        while not self._stop.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if not got or got == -1:             # 0 == WM_QUIT
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        try:
            self.platform.unhook_keyboard(self._hook)
        except Exception:
            pass
        self._hook = None

    def stop(self):
        self._stop.set()
        try:
            if self._hook and self.platform is not None:
                self.platform.unhook_keyboard(self._hook)
                self._hook = None
        except Exception:
            pass

    # ------------------------------------------------------------ fallback poll
    def _poll_loop(self):
        user32 = self.platform.user32 if self.platform else None
        while not self._stop.is_set():
            try:
                if user32 is not None:
                    down = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
                    if down and not self._prev_poll_down:
                        self._post(True, False)
                    elif not down and self._prev_poll_down:
                        self._post(False, False)
                    self._prev_poll_down = down
                else:
                    time.sleep(0.05)             # tests: no platform
            except Exception:
                pass
            time.sleep(0.025)

    # ------------------------------------------------------------ emergency
    def _emergency_loop(self):
        """Ctrl+Shift+Scroll Lock (W2.3). Scroll Lock ALONE must do nothing."""
        user32 = self.platform.user32 if self.platform else None
        if user32 is None:
            return
        prev = False
        while not self._stop.is_set():
            try:
                scroll = bool(user32.GetAsyncKeyState(VK_SCROLL) & 0x8000)
                ctrl = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                shift = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                chord = scroll and ctrl and shift
                if chord and not prev and self.on_emergency:
                    self.on_emergency()
                prev = chord
            except Exception:
                pass
            time.sleep(0.05)


def validate_key(name):
    """Return the canonical key or None. Restricted per brief W2.2."""
    if not name:
        return None
    canonical = str(name).lower()
    return canonical if canonical in VK_MAP else None
