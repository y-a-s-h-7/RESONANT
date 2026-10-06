"""Unit tests for hotkey.py (W2.2/W2.3) — fake platform, no real hooks."""
import time

import pytest

import hotkey as hk


class TestKeyValidation:
    def test_f_keys_allowed(self):
        assert hk.validate_key("f9") == "f9"
        assert hk.validate_key("F12") == "f12"

    def test_pause_and_capslock_allowed(self):
        assert hk.validate_key("pause") == "pause"
        assert hk.validate_key("capslock") == "capslock"

    def test_letters_digits_REJECTED(self):
        assert hk.validate_key("a") is None
        assert hk.validate_key("1") is None

    def test_f9_in_default_map(self):
        assert hk.VK_MAP["f9"] == 0x78


class TestDispatcher:
    def test_press_and_release_dispatched(self):
        events = []
        h = hk.Hotkey(on_press=lambda: events.append("down"),
                      on_release=lambda: events.append("up"))
        h._post(True, False)
        h._post(False, False)
        deadline = time.time() + 2
        while len(events) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert events == ["down", "up"]

    def test_bounded_queue_drops_oldest(self):
        h = hk.Hotkey()
        for i in range(100):
            h._post(i is None, False)
        assert len(h._queue) <= 65        # bounded, never unbounded growth


class TestEmergencyChord:
    def test_chord_logic_in_platform_poll(self):
        # emergency loop reads ctrl+shift+scroll via platform; with a fake
        # platform reporting all three held, on_emergency must fire.
        class FakeUser32:
            def __init__(self, state):
                self.state = state
            def GetAsyncKeyState(self, vk):
                return 0x8000 if vk in self.state else 0
        from platform_win import VK_CONTROL, VK_SCROLL, VK_SHIFT
        fired = []
        h = hk.Hotkey(on_emergency=lambda: fired.append(1))
        h.platform = type("P", (), {"user32": FakeUser32(
            {VK_CONTROL, VK_SHIFT, VK_SCROLL})})()
        h._stop = hk.threading.Event()
        t = hk.threading.Thread(target=h._emergency_loop, daemon=True)
        t.start()
        deadline = time.time() + 2
        while not fired and time.time() < deadline:
            time.sleep(0.01)
        h._stop.set()
        assert fired == [1]

    def test_scroll_alone_does_nothing(self):
        class FakeUser32:
            def __init__(self):
                self.vk_scroll = hk.VK_SCROLL
            def GetAsyncKeyState(self, vk):
                return 0x8000 if vk == self.vk_scroll else 0
        fired = []
        h = hk.Hotkey(on_emergency=lambda: fired.append(1))
        h.platform = type("P", (), {"user32": FakeUser32()})()
        h._stop = hk.threading.Event()
        t = hk.threading.Thread(target=h._emergency_loop, daemon=True)
        t.start()
        time.sleep(0.3)
        h._stop.set()
        assert fired == []          # Scroll Lock alone must NOT exit (W2.3)


class TestLLCallback:
    def _make_kbstruct(self, vk, flags=0):
        s = hk.KBDLLHOOKSTRUCT()
        s.vkCode = vk
        s.scanCode = 0
        s.flags = flags
        s.time = 0
        s.dwExtraInfo = None
        return s

    def test_injected_events_not_posted(self):
        from platform_win import LLKHF_INJECTED, VK_F9
        h = hk.Hotkey(key="f9")
        h.platform = type("P", (), {"user32": type("U", (), {
            "CallNextHookEx": staticmethod(lambda *a: 0)})()})()
        proc = h._ll_callback()
        kb = self._make_kbstruct(VK_F9, flags=LLKHF_INJECTED)
        ret = proc(0, 0x0100, ctypes_addressof(kb))
        assert h._queue == []        # injected: ignored (our own SendInput)
        assert ret == 0

    def test_own_key_down_posted_and_swallowed_when_active(self):
        from platform_win import VK_F9
        events = []
        h = hk.Hotkey(key="f9", swallow_active=lambda: True,
                      on_press=lambda: events.append("down"))
        h.platform = type("P", (), {"user32": type("U", (), {
            "CallNextHookEx": staticmethod(lambda *a: 0)})()})()
        proc = h._ll_callback()
        kb = self._make_kbstruct(VK_F9)
        ret = proc(0, 0x0100, ctypes_addressof(kb))
        assert ret == 1              # swallowed while listening
        deadline = time.time() + 2
        while not events and time.time() < deadline:
            time.sleep(0.01)
        assert events == ["down"]    # delivered via dispatcher

    def test_other_keys_passthrough(self):
        h = hk.Hotkey(key="f9", swallow_active=lambda: True)
        h.platform = type("P", (), {"user32": type("U", (), {
            "CallNextHookEx": staticmethod(lambda *a: 0)})()})()
        proc = h._ll_callback()
        kb = self._make_kbstruct(0x41)   # 'A'
        ret = proc(0, 0x0100, ctypes_addressof(kb))
        assert h._queue == [] and ret == 0


def ctypes_addressof(struct):
    import ctypes
    return ctypes.addressof(struct)


if __name__ == "__main__":
    pytest.main([__file__])
