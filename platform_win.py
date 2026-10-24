"""Resonant — thin, injectable Win32 platform layer (W0.3).

Every Windows API the app touches goes through this module so tests can inject
fakes (brief §1 rule 5: tests must run on any OS and never touch the real
keyboard, clipboard, microphone or registry).

Design:
    * `WinPlatform` wraps user32/kernel32/winreg/msvcrt passed in the
      constructor. On Windows, `PLATFORM` (module singleton) binds the real
      DLLs; tests construct `WinPlatform(user32=fake, ...)` or pass a full
      fake object instead.
    * `real_platform()` returns the singleton; on non-Windows it raises so a
      test can never accidentally hit real APIs.

Only what the app needs is wrapped — deliberately no general-purpose helpers.
"""
import os

IS_WINDOWS = os.name == "nt"

# Win32 constants used by the app (kept here so hotkey.py/injector.py don't
# each define their own copy).
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
HC_ACTION = 0
LLKHF_INJECTED = 0x10
WH_KEYBOARD_LL = 13
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12          # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_PAUSE = 0x13
VK_SCROLL = 0x91
VK_F9 = 0x78

_CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Clipboard format properties that keep dictations out of Win+V history and
# cloud sync (_verify_ per brief W3.1 — behaviour must be checked manually).
CLIPBOARD_EXCLUDE_PROPS = [
    ("CanIncludeInClipboardHistory", 0),
    ("CanUploadToCloudClipboard", 0),
    ("ExcludeClipboardContentFromMonitorProcessing", 1),
]


class WinPlatform:
    """Wraps the Win32 entry points Resonant uses. All handles are injectable.

    Real bindings are created lazily per-instance via `_bind_real()`; tests
    pass fakes for any subset. Methods are tiny and single-purpose.
    """

    def __init__(self, user32=None, kernel32=None, winreg=None, msvcrt=None):
        if user32 is None or kernel32 is None:
            self._bind_real()
        else:
            self.user32 = user32
            self.kernel32 = kernel32
        self.winreg = winreg or _lazy_winreg()
        self.msvcrt = msvcrt or _lazy_msvcrt()
        self._clipboard_open = False
        self._hook_proc = None        # keeps the CFUNCTYPE callback alive

    # ------------------------------------------------------------ binding
    def _bind_real(self):
        if not IS_WINDOWS:
            raise OSError("WinPlatform requires Windows; inject fakes in tests")
        import ctypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

    # ------------------------------------------------------------ hotkey
    def get_async_key_state(self, vk):
        """True if the key is currently physically down (high bit)."""
        return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)

    def set_low_keyboard_hook(self, hook_proc):
        """Install WH_KEYBOARD_LL. Returns the hook handle or None on failure.

        SetWindowsHookExW wants a real C function pointer: a plain Python
        callable is rejected as c_void_p, so it is wrapped here and kept alive
        on the instance for as long as Windows holds the hook (a collected
        callback would crash the process).
        """
        import ctypes
        if hook_proc is None:
            return None
        self.user32.SetWindowsHookExW.restype = ctypes.c_void_p
        self.user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        self.user32.CallNextHookEx.restype = ctypes.c_void_p
        self.user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        proc = hook_proc
        if not isinstance(hook_proc, ctypes._CFuncPtr):
            proc_type = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_void_p)
            proc = proc_type(hook_proc)
        self._hook_proc = proc            # anchor: GC would free the callback
        handle = self.user32.SetWindowsHookExW(
            13, proc, None, 0)   # 13 == WH_KEYBOARD_LL (hMod=None for global LL hook)
        return handle or None

    def unhook_keyboard(self, handle):
        import ctypes
        if handle:
            self.user32.UnhookWindowsHookEx(ctypes.c_void_p(handle))

    # ------------------------------------------------------- foreground
    def get_foreground_window(self):
        return self.user32.GetForegroundWindow()

    def get_window_thread_process_id(self, hwnd):
        import ctypes
        pid = ctypes.c_uint(0)
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def process_image_name(self, pid):
        """Exe name for a PID, or '' if unavailable (e.g. access denied)."""
        try:
            import ctypes
            import ctypes.wintypes as wt
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = self.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return ""
            buf = ctypes.create_unicode_buffer(260)
            size = wt.DWORD(260)
            ok = self.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size))
            self.kernel32.CloseHandle(h)
            return buf.value if ok else ""
        except Exception:
            return ""

    def process_creation_time(self, pid):
        """Creation time of a process as a float timestamp, or None."""
        try:
            import ctypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_uint),
                            ("dwHighDateTime", ctypes.c_uint)]
            class PINFO(ctypes.Structure):
                _fields_ = [("ftCreation", FILETIME), ("ftExit", FILETIME),
                            ("kernelTime", FILETIME), ("userTime", FILETIME)]
            h = self.kernel32.OpenProcess(0x0400, False, pid)  # QUERY_INFORMATION
            if not h:
                return None
            pi = PINFO()
            ok = self.kernel32.GetProcessTimes(
                h, ctypes.byref(pi.ftCreation), ctypes.byref(pi.ftExit),
                ctypes.byref(pi.kernelTime), ctypes.byref(pi.userTime))
            self.kernel32.CloseHandle(h)
            if not ok:
                return None
            # FILETIME -> unix seconds
            stamp = (pi.ftCreation.dwHighDateTime << 32) | pi.ftCreation.dwLowDateTime
            return stamp / 10_000_000.0 - 11644473600.0
        except Exception:
            return None

    def get_cursor_pos(self):
        import ctypes
        import ctypes.wintypes as wt
        pt = wt.POINT()
        ok = self.user32.GetCursorPos(ctypes.byref(pt))
        return (pt.x, pt.y) if ok else (0, 0)

    # ------------------------------------------------------------ clipboard
    def clipboard_open(self, hwnd=None):
        import time
        for _ in range(10):                    # W3.1: retry ~10 x 25ms
            if self.user32.OpenClipboard(hwnd or None):
                self._clipboard_open = True
                return True
            time.sleep(0.025)
        return False

    def clipboard_close(self):
        if self._clipboard_open:
            self.user32.CloseClipboard()
            self._clipboard_open = False

    def clipboard_sequence_number(self):
        return self.user32.GetClipboardSequenceNumber()

    def _alloc_global(self, data):
        """Copy bytes into an HGLOBAL for SetClipboardData. 64-bit safe."""
        import ctypes
        self.kernel32.GlobalAlloc.restype = ctypes.c_void_p
        self.kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalUnlock.restype = ctypes.c_int
        self.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return None
        ptr = self.kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            self.kernel32.GlobalUnlock(handle)
        return handle

    def clipboard_set_text(self, text, exclude_history=True):
        """Replace the clipboard with CF_UNICODETEXT. Returns True on success.

        `exclude_history` sets the Win+V / cloud-sync opt-out properties so a
        dictation is not remembered in clipboard history (_verify_, W3.1).
        """
        import ctypes
        if not self.clipboard_open():
            return False
        try:
            if not self.user32.EmptyClipboard():
                return False
            payload = (text or "").encode("utf-16-le") + b"\x00\x00"
            handle = self._alloc_global(payload)
            if handle is None:
                return False
            self.user32.SetClipboardData.restype = ctypes.c_void_p
            self.user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
            if not self.user32.SetClipboardData(_CF_UNICODETEXT, handle):
                return False
            if exclude_history:
                self._set_clipboard_exclude_props()
            return True
        except Exception:
            return False
        finally:
            self.clipboard_close()

    def _set_clipboard_exclude_props(self):
        """Best-effort Win+V/cloud history opt-out (_verify_)."""
        import ctypes
        try:
            self.user32.RegisterClipboardFormatW.restype = ctypes.c_uint
            self.user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
            for name, value in CLIPBOARD_EXCLUDE_PROPS:
                fmt = self.user32.RegisterClipboardFormatW(name)
                if not fmt:
                    continue
                handle = self._alloc_global(bytes(ctypes.c_uint(value)))
                if handle is None:
                    continue
                self.user32.SetClipboardData(fmt, handle)
        except Exception:
            pass

    def clipboard_get_text(self):
        """Current CF_UNICODETEXT as str, or None."""
        import ctypes
        if not self.user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return None
        if not self.clipboard_open():
            return None
        try:
            # HGLOBAL is a 64-bit pointer: declare it, or ctypes truncates the
            # handle to a 32-bit int and GlobalLock crashes the process.
            self.user32.GetClipboardData.restype = ctypes.c_void_p
            self.user32.GetClipboardData.argtypes = [ctypes.c_uint]
            self.kernel32.GlobalLock.restype = ctypes.c_void_p
            self.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            self.kernel32.GlobalUnlock.restype = ctypes.c_int
            self.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            h = self.user32.GetClipboardData(_CF_UNICODETEXT)
            if not h:
                return None
            ptr = self.kernel32.GlobalLock(h)
            if not ptr:
                return None
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                self.kernel32.GlobalUnlock(h)
        finally:
            self.clipboard_close()

    # ------------------------------------------------------------ registry
    def registry_get(self, path, name):
        """Value of a HKCU Run-key entry, or None."""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return val
        except OSError:
            return None

    def registry_set(self, path, name, value):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def registry_delete(self, path, name):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def make_or_open_event(self, name):
        EVENT_MODIFY_STATE = 0x0002
        return self.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name) or None

    def create_event(self, name):
        return self.kernel32.CreateEventW(None, False, False, name) or None

    def signal_event(self, handle):
        return bool(self.kernel32.SetEvent(handle))

    def wait_for_event(self, handle, timeout_ms=0xFFFFFFFF):
        return self.kernel32.WaitForSingleObject(handle, timeout_ms)

    def close_handle(self, handle):
        return bool(self.kernel32.CloseHandle(handle))

    def message_box(self, text, title, flags=0x10):
        self.user32.MessageBoxW(None, text, title, flags)


def _lazy_winreg():
    try:
        import winreg
        return winreg
    except ImportError:            # non-Windows test hosts
        return None


def _lazy_msvcrt():
    try:
        import msvcrt
        return msvcrt
    except ImportError:
        return None


_PLATFORM = None


def real_platform():
    """Module-level singleton of the real Win32 binding."""
    global _PLATFORM
    if _PLATFORM is None:
        _PLATFORM = WinPlatform()
    return _PLATFORM
