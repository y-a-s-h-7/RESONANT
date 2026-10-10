"""Characterisation tests, retargeted after the Phase 0-4 refactor (W0.2).

The old suite pinned behaviour of functions that have deliberately moved:

    ResonantEngine._trim_silence   -> recorder.trim_silence   (now adaptive)
    ResonantEngine._clean          -> editor.clean + guards   (W1.1-W1.3)
    ResonantEngine.VK_MAP          -> hotkey.VK_MAP           (letters removed, W2.2)
    config.load_settings           -> settings.load_settings  (overrides only, W4.4)

Each test below records the NEW intended behaviour of those pieces. Anything
that changes again must change these tests in the same commit.
"""
import json
import sys
import types

import numpy as np
import pytest

import hotkey
import recorder
import settings


# ------------------------------------------------------- recorder.trim_silence
class TestTrimSilence:
    """Adaptive trim (W2.1): noise-floor aware, padding, never zero-pads."""

    def test_trims_leading_and_trailing_silence(self):
        audio = np.zeros(100, dtype=np.float32)
        audio[30:60] = 0.5
        out = recorder.trim_silence(audio, 16000, pad_s=0)
        assert out.shape[0] == 30
        assert float(np.abs(out).max()) == 0.5

    def test_all_silence_returned_unchanged(self):
        audio = np.zeros(50, dtype=np.float32)
        out = recorder.trim_silence(audio, 16000, pad_s=0)
        assert out.shape[0] == 50

    def test_internal_silence_is_kept(self):
        audio = np.zeros(100, dtype=np.float32)
        audio[10:30] = 0.4
        audio[50:70] = 0.4
        out = recorder.trim_silence(audio, 16000, pad_s=0)
        assert out.shape[0] == 60             # 10..70, gap preserved

    def test_low_noise_floor_is_below_quiet_speech(self):
        audio = np.full(20, 0.005, dtype=np.float32)   # below absolute_min
        audio[5:15] = 0.5
        out = recorder.trim_silence(audio, 16000, pad_s=0)
        assert out.shape[0] == 10

    def test_padding_is_applied_but_never_added(self):
        audio = np.zeros(200, dtype=np.float32)
        audio[100:120] = 0.6
        out = recorder.trim_silence(audio, 1000, pad_s=0.05)   # 50 samples
        assert out.shape[0] == 20 + 100
        assert out.shape[0] <= audio.shape[0]


# ------------------------------------------------- quotation / tag stripping
class TestStripTagsAndQuotes:
    """Quote stripping moved from ResonantEngine._clean to editor (W1.1/W1.3)."""

    def _strip(self, reply):
        import editor
        return editor.strip_tags_and_quotes(reply)

    def test_wrapping_double_quotes_stripped(self):
        assert self._strip('"Hello world."') == "Hello world."

    def test_wrapping_single_quotes_stripped(self):
        assert self._strip("'Hello world.'") == "Hello world."

    def test_unmatched_quote_not_stripped(self):
        assert self._strip('"It\'s fine') == '"It\'s fine'

    def test_inner_quotes_untouched(self):
        assert self._strip('He said "hi" to me') == 'He said "hi" to me'

    def test_transcript_tags_stripped(self):
        assert self._strip("<transcript>Hello.</transcript>") == "Hello."


# ------------------------------------------------------------- settings merge
class TestSettingsMerge:
    """W4.4: defaults live in config.py; the file holds ONLY overrides."""

    def _load(self, tmp_path, payload=None, monkeypatch=None):
        path = tmp_path / "settings.json"
        if payload is not None:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return settings.load_settings(str(path)), path

    def test_defaults_used_when_file_missing(self, tmp_path):
        s, _ = self._load(tmp_path)
        for key in ("hotkey", "stt_model", "ollama_model", "ai_cleanup_mode"):
            assert key in s
        assert s["ai_cleanup_mode"] == "smart"

    def test_saved_values_override_defaults_and_unknown_keys_ignored(self, tmp_path):
        s, _ = self._load(tmp_path, {"hotkey": "f5", "bogus_key": 1})
        assert s["hotkey"] == "f5"
        assert "bogus_key" not in s

    def test_letters_rejected_as_hotkey(self, tmp_path):
        s, _ = self._load(tmp_path, {"hotkey": "a"})
        assert s["hotkey"] == "f9"                     # W2.2 restricted key set

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{not json", encoding="utf-8")
        s = settings.load_settings(str(path))
        assert s["hotkey"] == "f9"

    def test_save_writes_only_overrides(self, tmp_path):
        path = tmp_path / "settings.json"
        s = settings.load_settings(str(path))
        s["hotkey"] = "f7"
        assert settings.save_settings(s, str(path))
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == {"hotkey": "f7"}

    def test_roundtrip_overrides_only(self, tmp_path):
        path = tmp_path / "settings.json"
        s = settings.load_settings(str(path))
        s["append_trailing_space"] = False
        settings.save_settings(s, str(path))
        assert settings.load_settings(str(path))["append_trailing_space"] is False

    def test_bad_value_falls_back_with_notice(self, tmp_path):
        s, _ = self._load(tmp_path, {"smart_min_words": "lots"})
        assert s["smart_min_words"] == 40
        assert any("smart_min_words" in n for n in settings.LAST_NOTICES)

    def test_stt_fp16_defaults_to_auto(self, tmp_path):
        s, _ = self._load(tmp_path)
        assert s["stt_fp16"] == "auto"

    def test_stt_fp16_accepts_auto_true_false(self, tmp_path):
        for value in ("auto", True, False):
            s, _ = self._load(tmp_path, {"stt_fp16": value})
            assert s["stt_fp16"] == value

    def test_stt_fp16_rejects_junk_with_notice(self, tmp_path):
        s, _ = self._load(tmp_path, {"stt_fp16": "sometimes"})
        assert s["stt_fp16"] == "auto"                 # falls back to the default
        assert any("stt_fp16" in n for n in settings.LAST_NOTICES)

    def test_silence_autostop_defaults_to_off(self, tmp_path):
        s, _ = self._load(tmp_path)
        assert s["silence_autostop_ms"] == 0

    def test_silence_autostop_accepts_zero_and_deliberate_windows(self, tmp_path):
        for value in (0, 500, 2000, 5000):
            s, _ = self._load(tmp_path, {"silence_autostop_ms": value})
            assert s["silence_autostop_ms"] == value

    def test_silence_autostop_rejects_out_of_range_and_junk(self, tmp_path):
        for value in (499, 5001, -1, True, "500", None):
            del settings.LAST_NOTICES[:]
            s, _ = self._load(tmp_path, {"silence_autostop_ms": value})
            assert s["silence_autostop_ms"] == 0        # falls back to off
            assert any("silence_autostop_ms" in n
                       for n in settings.LAST_NOTICES), value

    def test_legacy_full_dump_is_migrated_and_backed_up(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({
            "hotkey": "f9",                     # old default: NOT an override
            "ai_cleanup": False,                # override -> "off"
            "paste_mode": True,                 # override -> "paste"
            "run_at_startup": True,             # old default: dropped (opt-in now)
        }), encoding="utf-8")
        s = settings.load_settings(str(path))
        assert s["ai_cleanup_mode"] == "off"      # real override, mapped
        assert s["inject_mode"] == "auto"         # paste_mode=True WAS the old
        assert s["run_at_startup"] is False        # default -> not an override
        assert (tmp_path / "settings.json.bak").exists()

    def test_legacy_paste_mode_false_maps_to_type(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"paste_mode": False}), encoding="utf-8")
        assert settings.load_settings(str(path))["inject_mode"] == "type"


# ------------------------------------------------------------- VK map invariants
class TestVkMap:
    def test_f_keys_present(self):
        assert hotkey.VK_MAP["f9"] == 0x78

    def test_letters_and_digits_REMOVED(self):
        # W2.2 removed letters/digits: typing them would trigger recording.
        assert "a" not in hotkey.VK_MAP and "1" not in hotkey.VK_MAP

    def test_pause_and_capslock_allowed(self):
        assert hotkey.validate_key("pause") == "pause"
        assert hotkey.validate_key("capslock") == "capslock"


# ------------------------------------------------------------ import safety
def test_engine_import_does_not_need_torch():
    """The dev venv has no heavy ML dependencies; importing the engine must still work."""
    assert "torch" not in sys.modules
    import importlib

    import resonant_engine

    assert isinstance(importlib.reload(resonant_engine), types.ModuleType)
    assert "torch" not in sys.modules


if __name__ == "__main__":
    pytest.main([__file__])
