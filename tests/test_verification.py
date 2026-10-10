"""verification.py tests — checklist parser, result file, and the safe
automated checks driven through fakes (no real hardware or clipboard)."""
import ctypes
import sys

import pytest

import verification as v

SAMPLE_MD = """
# Checklist

Some intro text.

Tick marks: `[ ]` = not yet verified, `[x]` = verified.

## Capture & hotkey

- [ ] Hold the hotkey in **Excel**: no side effects,
      no key repeat while held.
- [x] Say a sentence starting with a hard consonant (W2.1).

## Injection & clipboard

- [ ] Win+V history does not contain the dictation (W3.1).
"""


# ------------------------------------------------------------------- parser
class TestParser:
    def test_parses_sections_items_and_wrapped_lines(self):
        items = v.parse_items(SAMPLE_MD)
        assert [i["section"] for i in items] == [
            "Capture & hotkey", "Capture & hotkey", "Injection & clipboard"]
        assert items[0]["text"] == ("Hold the hotkey in **Excel**: no side "
                                    "effects, no key repeat while held.")
        assert items[1]["checked"] is True
        assert items[2]["checked"] is False

    def test_ids_are_unique_and_stable(self):
        first = v.parse_items(SAMPLE_MD)
        second = v.parse_items(SAMPLE_MD)
        assert [i["id"] for i in first] == [i["id"] for i in second]
        assert len({i["id"] for i in first}) == len(first)
        assert all(i["kind"] == "manual" for i in first)

    def test_ignores_non_checkbox_lines(self):
        assert v.parse_items("no items here\nTick marks: [ ] nothing") == []
        assert v.parse_items("") == []

    def test_real_manual_file_parses_every_checkbox(self):
        items = v.read_items()
        assert len(items) >= 40
        assert all(i["text"] for i in items)
        assert len({i["id"] for i in items}) == len(items)
        sections = {i["section"] for i in items}
        assert "Capture & hotkey" in sections
        assert "Injection & clipboard" in sections


# -------------------------------------------------------------- result file
class TestResults:
    def test_missing_file_is_empty(self, tmp_path):
        data = v.load_results(str(tmp_path / "nope.json"))
        assert data["results"] == {}
        assert data["os"]
        assert data["python"]

    def test_save_then_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "v.json")
        data = v.load_results(path)
        v.set_result(data, "01-x", v.PASS, "looks good")
        assert v.save_results(data, path)
        again = v.load_results(path)
        assert again["results"]["01-x"]["status"] == "pass"
        assert "looks good" in again["results"]["01-x"]["detail"]

    def test_corrupt_file_degrades_to_empty(self, tmp_path):
        path = tmp_path / "v.json"
        path.write_text("{not json", encoding="utf-8")
        assert v.load_results(str(path))["results"] == {}

    def test_rejects_unknown_status(self):
        with pytest.raises(ValueError):
            v.set_result({}, "01-x", "maybe")

    def test_saved_file_has_no_transcript_or_audio(self, tmp_path):
        result_file = tmp_path / "v.json"
        data = v.load_results(str(result_file))
        v.set_result(data, "01-x", v.PASS, "typed into a temporary window")
        assert v.save_results(data, str(result_file))
        raw = result_file.read_text(encoding="utf-8").lower()
        assert "transcript" not in raw
        assert "audio" not in raw

    def test_summarize_counts(self):
        data = v.load_results("definitely-missing.json")
        v.set_result(data, "a", v.PASS)
        v.set_result(data, "b", v.FAIL)
        v.set_result(data, "c", v.SKIP)
        v.set_result(data, "d", v.PASS)
        assert v.summarize(data) == (2, 1, 1)


# ----------------------------------------------------------- fakes
class FakeClipboard:
    def __init__(self, text="original"):
        self.text = text
        self.seq = 1
        self.sets = []

    def clipboard_get_text(self):
        return self.text

    def clipboard_set_text(self, text, exclude_history=True):
        self.text = text
        self.seq += 1
        self.sets.append((text, exclude_history))
        return True

    def clipboard_sequence_number(self):
        return self.seq


class RacingClipboard(FakeClipboard):
    """Simulates the user copying something else during the check."""

    def __init__(self):
        super().__init__("old")
        self.calls = 0

    def clipboard_sequence_number(self):
        self.calls += 1
        if self.calls == 3:        # the guard read, after our read-back
            self.text = "external copy"
            self.seq += 1
        return self.seq


class TestClipboardCheck:
    def test_roundtrip_restores_previous_text(self):
        plat = FakeClipboard("keep me")
        ok, detail = v.check_clipboard_roundtrip(platform=plat)
        assert ok and plat.text == "keep me"
        assert "guarded" in detail
        # The marker was written with the Win+V opt-out flag.
        assert all(flag for _text, flag in plat.sets)

    def test_guarded_restore_never_clobbers_an_external_copy(self):
        plat = RacingClipboard()
        ok, detail = v.check_clipboard_roundtrip(platform=plat)
        assert ok and plat.text == "external copy"
        assert "guarded" in detail

    def test_busy_clipboard_reports_failure(self):
        plat = FakeClipboard()
        plat.clipboard_set_text = lambda *a, **k: False
        ok, detail = v.check_clipboard_roundtrip(platform=plat)
        assert not ok and "clipboard" in detail


class TestRunAutomated:
    def test_records_results_and_survives_a_crashing_check(self, tmp_path):
        path = str(tmp_path / "r.json")
        data = v.load_results(path)

        def boom():
            raise RuntimeError("no hardware")

        checks = [
            {"id": "auto-a", "name": "A", "fn": lambda: (True, "fine")},
            {"id": "auto-b", "name": "B", "fn": lambda: (False, "broken")},
            {"id": "auto-c", "name": "C", "fn": boom},
        ]
        ran = v.run_automated(data, path, checks=checks, out=lambda *_: None)
        assert [ok for _check, ok, _detail in ran] == [True, False, False]
        saved = v.load_results(path)["results"]
        assert saved["auto-a"]["status"] == "pass"
        assert saved["auto-b"]["status"] == "fail"
        assert saved["auto-c"]["status"] == "fail"

    def test_only_missing_skips_already_passed_checks(self, tmp_path):
        path = str(tmp_path / "r.json")
        data = v.load_results(path)
        v.set_result(data, "auto-a", v.PASS)
        v.save_results(data, path)
        calls = []
        checks = [{"id": "auto-a", "name": "A",
                   "fn": lambda: calls.append("a") or (True, "x")},
                  {"id": "auto-b", "name": "B",
                   "fn": lambda: calls.append("b") or (True, "y")}]
        ran = v.run_automated(data, path, checks=checks, out=lambda *_: None)
        assert calls == ["b"]                    # passed check not re-run
        assert [c["id"] for c, _ok, _d in ran] == ["auto-b"]


# ------------------------------------------------------------ manual walk
class TestRunManual:
    def _items(self):
        return [{"id": "01-a", "section": "S", "text": "first"},
                {"id": "02-b", "section": "S", "text": "second"}]

    def test_quit_saves_progress_and_stops(self, tmp_path):
        path = str(tmp_path / "r.json")
        answers = iter(["p", "q"])
        data = v.load_results(path)
        done = v.run_manual(data, self._items(), path=path,
                            ask=lambda _prompt: next(answers),
                            out=lambda *_: None)
        assert done is False
        saved = v.load_results(path)["results"]
        assert saved["01-a"]["status"] == "pass"
        assert "02-b" not in saved

    def test_fail_and_skip_are_recorded(self, tmp_path):
        path = str(tmp_path / "r.json")
        answers = iter(["f", "s"])
        data = v.load_results(path)
        assert v.run_manual(data, self._items(), path=path,
                            ask=lambda _prompt: next(answers),
                            out=lambda *_: None) is True
        saved = v.load_results(path)["results"]
        assert saved["01-a"]["status"] == "fail"
        assert saved["02-b"]["status"] == "skip"

    def test_only_missing_skips_passed_items(self, tmp_path):
        path = str(tmp_path / "r.json")
        data = v.load_results(path)
        v.set_result(data, "01-a", v.PASS)
        v.save_results(data, path)
        asked = []
        v.run_manual(data, self._items(), path=path,
                     ask=lambda prompt: asked.append(prompt) or "p",
                     out=lambda *_: None)
        assert len(asked) == 1                   # only the unpassed item


# ------------------------------------------------------------ hook check
class _FakeUser32:
    def GetMessageW(self, *args):
        # Real GetMessageW blocks until a message arrives; a fake that returns
        # instantly makes the hook thread exit (and clear _hook) before
        # Hotkey.start() checks it, which looks like a polling fallback.
        import time
        time.sleep(0.5)
        return 0

    def TranslateMessage(self, *args):
        return 0

    def DispatchMessageW(self, *args):
        return 0


class FakeHookPlatform:
    def __init__(self, hook_works=True):
        self.user32 = _FakeUser32()
        self.hook_works = hook_works
        self.unhooks = 0

    def set_low_keyboard_hook(self, proc):
        return 99 if self.hook_works else None

    def unhook_keyboard(self, handle):
        self.unhooks += 1


@pytest.mark.skipif(sys.platform != "win32",
                    reason="ctypes.wintypes is Windows-only")
class TestHookCheck:
    def test_success_path_installs_and_unhooks(self):
        plat = FakeHookPlatform()
        ok, detail = v.check_hook_install(platform=plat)
        assert ok and plat.unhooks >= 1

    def test_polling_fallback_is_reported_as_a_failure(self):
        plat = FakeHookPlatform(hook_works=False)
        ok, detail = v.check_hook_install(platform=plat)
        assert not ok and "polling" in detail


# ------------------------------------------------------- win32 struct guard
def test_input_struct_size_is_win32_correct():
    """SendInput validates cbSize; a wrong union made typing fall back silently."""
    import injector
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(injector.INPUT) == expected
