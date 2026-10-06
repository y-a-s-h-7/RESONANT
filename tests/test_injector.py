"""injector.py unit tests (W3.1-W3.3) — fake Win32 layer, no real clipboard.

The brief forbids touching the real keyboard/clipboard in tests, so every test
drives `injector` through a fake platform object and inspects the INPUT array
that would have been sent.
"""
import ctypes

import pytest

import injector


class FakeUser32:
    """Records every INPUT batch as (wVk, wScan, dwFlags) triples."""

    def __init__(self):
        self.batches = []

    def SendInput(self, count, ptr, size):
        arr = ctypes.cast(ptr, ctypes.POINTER(injector.INPUT))
        self.batches.append([(arr[i].u.ki.wVk, arr[i].u.ki.wScan,
                              arr[i].u.ki.dwFlags) for i in range(count)])
        return count

    def CallNextHookEx(self, *args):
        return 0


class FakePlatform:
    def __init__(self):
        self.user32 = FakeUser32()
        self.clipboard = [""]
        self.sequence = 0
        self.sets = []

    # clipboard
    def clipboard_get_text(self):
        return self.clipboard[-1] if self.clipboard else None

    def clipboard_set_text(self, text, exclude_history=True):
        self.clipboard.append(text)
        self.sequence += 1
        self.sets.append((text, exclude_history))
        return True

    def clipboard_sequence_number(self):
        return self.sequence

    # input state
    def get_async_key_state(self, vk):
        return False

    def get_foreground_window(self):
        return 1234

    def get_window_thread_process_id(self, hwnd):
        return 42

    def process_image_name(self, pid):
        return r"C:\Windows\notepad.exe"

    def external_copy(self, text):
        """Simulate the user copying something while we paste."""
        self.clipboard.append(text)
        self.sequence += 1


def key_units(platform, index=0):
    """Unicode code units in a batch (key-down events only)."""
    import platform_win as pw
    return [scan for _vk, scan, flags in platform.user32.batches[index]
            if scan and not flags & pw.KEYEVENTF_KEYUP]


def down_vks(platform, index=0):
    """Virtual-key codes in a batch (key-down events only)."""
    import platform_win as pw
    return [vk for vk, _scan, flags in platform.user32.batches[index]
            if vk and not flags & pw.KEYEVENTF_KEYUP]


# ------------------------------------------------------------------ typing
class TestTypeText:
    def test_ascii_typed_as_unicode_units(self):
        platform = FakePlatform()
        assert injector.type_text(platform, "hi", append_trailing_space=False)
        assert key_units(platform) == [ord("h"), ord("i")]

    def test_non_ascii_survives(self):
        platform = FakePlatform()
        text = "caf\u00e9\u2019\u2014"
        injector.type_text(platform, text, append_trailing_space=False)
        assert key_units(platform) == [ord(c) for c in text]

    def test_surrogate_pairs_for_astral_chars(self):
        platform = FakePlatform()
        injector.type_text(platform, "\U0001f642", append_trailing_space=False)
        assert key_units(platform) == [0xD83D, 0xDE42]

    def test_trailing_space_appended(self):
        platform = FakePlatform()
        injector.type_text(platform, "ok", append_trailing_space=True)
        assert key_units(platform)[-1] == ord(" ")

    def test_every_unit_sent_as_down_and_up(self):
        platform = FakePlatform()
        injector.type_text(platform, "ab", append_trailing_space=False)
        assert len(platform.user32.batches[0]) == 4

    def test_long_text_is_batched(self):
        platform = FakePlatform()
        injector.type_text(platform, "x" * (injector.TYPE_BATCH * 3),
                           append_trailing_space=False)
        assert len(platform.user32.batches) >= 3


class TestCodeUnits:
    def test_units_roundtrip_to_text(self):
        text = "café 🙂"
        units = injector.code_units(text)
        raw = b"".join(u.to_bytes(2, "little") for u in units)
        assert raw.decode("utf-16-le") == text


# ------------------------------------------------------------------ paste
class TestPasteText:
    def test_paste_sets_clipboard_sends_ctrl_v_and_restores(self):
        platform = FakePlatform()
        platform.external_copy("original")
        ok, detail = injector.paste_text(platform, "dictated",
                                        append_trailing_space=False,
                                        sleep=lambda _s: None)
        assert ok
        assert platform.sets[0][0] == "dictated"
        assert platform.sets[0][1] is True            # excluded from Win+V
        assert platform.clipboard[-1] == "original"   # restored
        assert injector.VK_V in down_vks(platform)

    def test_restore_skipped_when_user_copied_something_else(self):
        platform = FakePlatform()
        platform.external_copy("original")
        # While Ctrl+V is being delivered the user copies something else: the
        # sequence number changes, so the restore must be skipped (W3.1).
        injector.paste_text(platform, "dictated", append_trailing_space=False,
                            sleep=lambda _s: platform.external_copy("something else"))
        assert platform.clipboard[-1] == "something else"

    def test_busy_clipboard_reports_failure(self):
        platform = FakePlatform()
        platform.clipboard_set_text = lambda *a, **k: False
        ok, detail = injector.paste_text(platform, "x", sleep=lambda _s: None)
        assert not ok and "busy" in detail


# ------------------------------------------------------------------ inject
class TestInjectorMode:
    def _injector(self):
        return injector.Injector(platform=FakePlatform(), sleep=lambda _s: None)

    def test_short_text_types(self):
        inj = self._injector()
        status, detail = inj.inject("hello", mode="auto")
        assert status == "typed" and detail == "typed"

    def test_multiline_uses_paste(self):
        inj = self._injector()
        status, _ = inj.inject("line one\nline two", mode="auto")
        assert status == "typed"
        assert inj.platform.sets                      # clipboard path used
        assert key_units(inj.platform) == []          # nothing typed per char

    def test_paste_mode_never_types(self):
        inj = self._injector()
        inj.inject("short", mode="paste")
        assert inj.platform.sets
        assert key_units(inj.platform) == []          # only Ctrl+V, no characters

    def test_type_failure_falls_back_to_clipboard(self):
        class BadPlatform(FakePlatform):
            def __init__(self):
                super().__init__()
                self.user32 = FakeUser32()

                def boom(*args):
                    raise RuntimeError("no input")
                self.user32.SendInput = boom
                self.clipboard_set_text = lambda *a, **k: False
        inj = injector.Injector(platform=BadPlatform(), sleep=lambda _s: None)
        status, detail = inj.inject("hello", mode="type")
        assert status == "failed"
        assert "clipboard" in detail

    def test_empty_text_is_dropped(self):
        inj = self._injector()
        assert inj.inject("   ")[0] == "dropped"


class TestForegroundGuard:
    def test_valid_when_window_unchanged(self):
        platform = FakePlatform()
        guard = injector.ForegroundGuard(platform)
        guard.snapshot()
        assert guard.still_valid()

    def test_invalid_after_window_change(self):
        platform = FakePlatform()
        guard = injector.ForegroundGuard(platform)
        guard.snapshot()
        platform.get_foreground_window = lambda: 999
        assert not guard.still_valid()

    def test_changed_window_copies_instead_of_typing(self):
        platform = FakePlatform()
        guard = injector.ForegroundGuard(platform)
        guard.snapshot()
        platform.get_foreground_window = lambda: 999
        inj = injector.Injector(platform=platform, sleep=lambda _s: None)
        status, detail = inj.inject("hello", mode="auto", guard=guard)
        assert status == "copied"
        assert "window changed" in detail


class TestModifierWait:
    def test_returns_true_when_nothing_held(self):
        assert injector.wait_for_modifiers_released(FakePlatform(),
                                                    sleep=lambda _s: None)


if __name__ == "__main__":
    pytest.main([__file__])
