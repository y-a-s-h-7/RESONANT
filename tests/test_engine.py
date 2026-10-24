"""ResonantEngine facade tests (W1.6, W2.4, W4.5) — fake transcriber/injector.

No microphone, no torch, no Ollama, no keyboard: the engine is constructed with
injected collaborators and the pipeline is driven through `_enqueue`.
"""
import threading
import time

import numpy as np
import pytest

import config
import recorder as recorder_mod
import resonant_engine


class FakeModel:
    def __init__(self, text="hello world"):
        self.text = text
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        return {"text": self.text,
                "segments": [{"no_speech_prob": 0.0, "avg_logprob": -0.2,
                              "compression_ratio": 1.2, "text": self.text}]}


class FakePlatform:
    """Enough of the Win32 surface for the foreground guard to work offline."""

    def __init__(self):
        self.user32 = self._FakeUser32()

    class _FakeUser32:
        def __init__(self):
            self.batches = []
        def SendInput(self, count, ptr, size):
            import ctypes

            import injector as _inj

            arr = ctypes.cast(ptr, ctypes.POINTER(_inj.INPUT))
            self.batches.append([(arr[i].u.ki.wVk, arr[i].u.ki.wScan, arr[i].u.ki.dwFlags) for i in range(count)])
            return count
        def GetAsyncKeyState(self, vk):
            return 0

    def get_async_key_state(self, vk):
        return False

    def get_foreground_window(self):
        return 4242

    def get_window_thread_process_id(self, hwnd):
        return 99

    def process_image_name(self, pid):
        return "notepad.exe"


class FakeRecorder:
    """Records start/release calls; hands back pre-cooked audio."""

    def __init__(self, audio=None, device_error=None, fallback=False):
        self.audio = audio
        self.started = False
        self.released = False
        self.start_kwargs = None          # captured for wiring assertions
        self.buffer = recorder_mod.RecordingBuffer()
        self.buffer.device_error = device_error
        self.buffer.device_fallback = fallback

    def start(self, **kwargs):
        self.started = True
        self.start_kwargs = kwargs
        return self.buffer

    def release(self):
        self.released = True

    def wait(self, timeout=None):
        return self.audio, 0.0


class FakeInjector:
    def __init__(self, status="typed", detail="typed"):
        self.status = status
        self.detail = detail
        self.calls = []

    def inject(self, text, mode="auto", append_trailing_space=True, guard=None):
        self.calls.append(text)
        return self.status, self.detail


def make_engine(text="hello world", status="typed"):
    settings = dict(config.DEFAULT_SETTINGS)
    settings["ai_cleanup_mode"] = "off"       # no network in unit tests
    settings["sound_feedback"] = False
    settings["min_peak"] = 0.01
    settings["stt_fp16"] = False              # no torch probe in unit tests
    engine = resonant_engine.ResonantEngine(settings=settings,
                                            platform=FakePlatform(),
                                            injector=FakeInjector(status=status))
    engine.models_ready = True
    engine.stt_model = FakeModel(text)
    return engine


def loud_audio(seconds=0.8, value=0.4):
    return (np.ones(int(16000 * seconds), dtype=np.float32) * value)


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestPipeline:
    def test_dictation_types_and_records_status(self):
        engine = make_engine()
        engine._enqueue(loud_audio(), None)
        assert wait_for(lambda: engine.get_history())
        entry = engine.get_history()[-1]
        assert entry["status"] == "typed"
        assert entry["raw"] == "hello world"
        assert entry["final"]        # deterministic tidy gives usable text
        assert engine.last_status == "typed"

    def test_copied_status_preserved(self):
        engine = make_engine(status="copied")
        engine._enqueue(loud_audio(), None)
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"] == "copied"

    def test_failed_status_preserved(self):
        engine = make_engine(status="failed")
        engine._enqueue(loud_audio(), None)
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"].startswith("failed")

    def test_quiet_audio_is_dropped_with_notice(self):
        engine = make_engine()
        engine._enqueue(np.zeros(16000, dtype=np.float32), None)
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"].startswith("dropped")
        assert "quiet" in engine.notice.lower()

    def test_short_audio_is_dropped(self):
        engine = make_engine()
        engine._enqueue(np.ones(1000, dtype=np.float32) * 0.5, None)
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"] == "dropped(too-short)"

    def test_missing_model_does_not_type_anything(self):
        engine = make_engine()
        engine.models_ready = False
        engine.stt_model = None
        engine._enqueue(loud_audio(), None)
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"] == "dropped(model-loading)"

    def test_long_dictation_uses_editor_chunking_path(self):
        """A 300-word transcript must still come out whole (W1.1/W1.5)."""
        engine = make_engine(text="word " * 300)
        engine._enqueue(loud_audio(2.0), None)
        assert wait_for(lambda: engine.get_history())
        assert len(engine.last_raw.split()) >= 300


class TestQueue:
    def test_queue_full_drops_with_notice(self, monkeypatch):
        engine = make_engine()
        release = threading.Event()

        def blocking_process(audio, guard, mic_error=None):
            release.wait(5)

        monkeypatch.setattr(engine, "_process", blocking_process)
        for _ in range(resonant_engine.QUEUE_MAX + 3):
            engine._enqueue(loud_audio(0.4), None)
        assert engine.queue_depth <= resonant_engine.QUEUE_MAX
        assert any("queue-full" in (entry["status"] or "")
                   for entry in engine.get_history())
        assert "busy" in engine.notice.lower()
        release.set()

    def test_history_never_exceeds_cap(self):
        engine = make_engine()
        for _ in range(resonant_engine.HISTORY_MAX + 5):
            engine._record_history("raw", "final", "typed")
        assert len(engine.get_history()) == resonant_engine.HISTORY_MAX


class TestModelSwitchSafety:
    def test_load_failure_keeps_previous_model_and_setting(self, monkeypatch):
        import sys

        engine = make_engine()
        previous = engine.settings["stt_model"]
        engine.stt_model = FakeModel("still here")

        def boom(name, *args, **kwargs):
            raise RuntimeError("offline")

        stt = sys.modules.get("whisper")
        monkeypatch.setattr(stt, "load_model", boom, raising=False)
        assert engine.load_models("small.en") is False
        assert engine.settings["stt_model"] == previous
        assert engine.stt_model is not None          # app keeps working
        assert engine.model_error == "offline"

    def test_superseded_load_is_discarded(self, monkeypatch):
        import sys
        engine = make_engine()
        engine.stt_model = FakeModel("current")

        def slow_load(name, *args, **kwargs):
            engine._load_gen += 1          # a newer request arrives meanwhile
            return FakeModel("stale-" + name)

        monkeypatch.setattr(sys.modules["whisper"], "load_model", slow_load,
                            raising=False)
        assert engine.load_models("tiny.en") is False


class TestSettingsIntegration:
    def test_apply_settings_validates_and_persists(self, monkeypatch, tmp_path):
        path = tmp_path / "settings.json"
        monkeypatch.setattr(resonant_engine.settings_mod, "SETTINGS_PATH",
                            str(path), raising=False)
        engine = make_engine()
        assert engine.apply_settings({"sound_feedback": True})
        assert engine.settings["sound_feedback"] is True
        assert not engine.apply_settings({"nonsense": 1})

    def test_bad_value_sets_notice_and_keeps_default(self):
        engine = make_engine()
        engine.apply_settings({"ai_cleanup_mode": "sometimes"})
        assert engine.settings["ai_cleanup_mode"] == "off"   # unchanged
        assert "ai_cleanup_mode" in engine.notice

    def test_replacements_are_whole_word(self):
        engine = make_engine()
        engine.settings["replacements"] = {"resonant": "Resonant"}
        assert engine._apply_replacements("resonant rocks") == "Resonant rocks"
        assert engine._apply_replacements("resonantly") == "resonantly"

    def test_replacements_ignore_case_and_stay_literal(self):
        """Every casing of a misheard word is fixed; the target stays literal."""
        engine = make_engine()
        engine.settings["replacements"] = {
            "kubernetes": "Kubernetes", "c drive": "C:\\drive"}
        assert engine._apply_replacements("KUBERNETES rocks") == "Kubernetes rocks"
        assert engine._apply_replacements("kubernetesian") == "kubernetesian"
        assert engine._apply_replacements("open c drive") == "open C:\\drive"


class TestHotkeyModes:
    def test_hold_mode_starts_on_press_stops_on_release(self):
        engine = make_engine()
        engine._recorder = FakeRecorder(loud_audio())
        engine.set_listening(True)
        engine._on_press()
        assert engine.is_recording is True
        engine._on_release()
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"] == "typed"

    def test_toggle_mode_ignores_release_and_stops_on_second_press(self):
        engine = make_engine()
        engine.settings["hotkey_mode"] = "toggle"
        engine._recorder = FakeRecorder(loud_audio())
        engine.set_listening(True)
        engine._on_press()
        assert engine.is_recording is True
        engine._on_release()                 # key up must NOT stop recording
        assert engine.is_recording is True
        engine._on_press()                   # second tap finishes it
        assert engine.is_recording is False
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"] == "typed"

    def test_toggle_mode_does_not_restart_while_processing(self):
        engine = make_engine()
        engine.settings["hotkey_mode"] = "toggle"
        engine.set_listening(True)
        engine.processing = True             # pretending the previous job runs
        engine._on_press()
        assert engine.is_recording is False
        assert "busy" in engine.notice.lower()


class TestMicrophoneFailures:
    def test_dead_microphone_is_reported_as_such(self):
        engine = make_engine()
        engine._enqueue(None, None, "device 3 not found")
        assert wait_for(lambda: engine.get_history())
        assert engine.get_history()[-1]["status"] == "dropped(no-mic)"
        assert "microphone unavailable" in engine.notice.lower()

    def test_falling_back_to_the_default_device_is_announced(self):
        engine = make_engine()
        engine._recorder = FakeRecorder(audio=None, device_error="busy",
                                        fallback=True)
        engine._recorder.audio = loud_audio()
        engine.set_listening(True)
        engine._on_press()
        engine._on_release()
        assert wait_for(lambda: "system default" in engine.notice.lower())


class TestFp16Wiring:
    def test_engine_forwards_stt_fp16_setting_to_transcriber(self):
        engine = make_engine()
        engine.settings["stt_fp16"] = False        # no torch probe needed
        captured = {}

        class CapturingModel:
            def transcribe(self, audio, **kwargs):
                captured.update(kwargs)
                return {"text": "", "segments": []}

        engine.stt_model = CapturingModel()
        engine._transcribe(loud_audio(0.5))
        assert captured["fp16"] is False

    def test_engine_passes_language_and_vocabulary_alongside_fp16(self, monkeypatch):
        engine = make_engine()
        engine.settings["custom_vocabulary"] = ["Kubernetes"]
        monkeypatch.setattr(resonant_engine.transcriber_mod, "_use_fp16",
                            lambda *a, **k: False)
        captured = {}

        class CapturingModel:
            def transcribe(self, audio, **kwargs):
                captured.update(kwargs)
                return {"text": "", "segments": []}

        engine.stt_model = CapturingModel()
        engine._transcribe(loud_audio(0.5))
        assert captured["language"] == "en"
        assert captured["initial_prompt"] == "Kubernetes"
        assert captured["fp16"] is False

    def test_engine_forwards_vad_filter_and_prompt_for_faster_backend(self):
        """vad_filter and initial_prompt reach the model for the faster backend."""
        engine = make_engine()
        engine.settings["stt_fp16"] = "auto"
        engine.settings["asr_backend"] = "faster"
        engine.settings["custom_vocabulary"] = ["Kubernetes"]
        engine.stt_model = engine.stt_model or None
        captured = {}

        class CapturingFasterModel:
            def transcribe(self, audio, **kwargs):
                captured.update(kwargs)
                return {"text": "", "segments": []}

        engine.stt_model = CapturingFasterModel()
        engine._transcribe(loud_audio(0.5))
        assert captured.get("vad_filter") is None
        assert captured.get("initial_prompt") == "Kubernetes"
        assert captured.get("language") == "en"
        assert captured.get("fp16") is True             # auto -> True for faster

    def test_engine_keeps_fp16_true_when_forced_for_faster_backend(self):
        engine = make_engine()
        engine.settings["asr_backend"] = "faster"
        engine.settings["stt_fp16"] = True
        captured = {}

        class CapturingFasterModel:
            def transcribe(self, audio, **kwargs):
                captured.update(kwargs)
                return {"text": "", "segments": []}

        engine.stt_model = CapturingFasterModel()
        engine._transcribe(loud_audio(0.5))
        assert captured.get("fp16") is True
        assert captured.get("vad_filter") is None


class TestSmartDecisionState:
    def test_state_exposes_text_free_smart_decision_counts(self):
        engine = make_engine()
        counts = engine.get_state()["smart_decisions"]
        assert set(counts) == {"smart_skips", "smart_llm_calls"}
        # Telemetry only — no transcript content anywhere in the payload.
        assert all(isinstance(v, int) for v in counts.values())


class TestSilenceAutostopWiring:
    """silence_autostop_ms reaches the recorder; the callback ends the take."""

    def _start(self, engine):
        engine._recorder = FakeRecorder(loud_audio())
        engine.set_listening(True)
        engine._on_press()
        assert engine.is_recording is True
        return engine._recorder.start_kwargs

    def test_setting_and_callback_are_forwarded_to_recorder(self):
        engine = make_engine()
        engine.settings["silence_autostop_ms"] = 2000
        kw = self._start(engine)
        assert kw["silence_autostop_ms"] == 2000
        assert kw["on_silence"] == engine._on_silence_stop

    def test_silence_autostop_is_off_by_default(self):
        engine = make_engine()                    # config default: 0
        kw = self._start(engine)
        assert kw["silence_autostop_ms"] == 0

    def test_junk_setting_never_arms_the_detector(self):
        engine = make_engine()
        engine.settings["silence_autostop_ms"] = "soon"
        assert engine._silence_autostop_ms() == 0

    def test_silence_callback_finishes_recording_before_noticing(self):
        """Pill shows a notice only when idle: finish FIRST, then notice."""
        engine = make_engine()
        engine._recorder = FakeRecorder(loud_audio())
        engine.set_listening(True)
        engine._on_press()
        seen = []
        engine.on("notice", lambda message, toast=True:
                  seen.append((message, engine.is_recording))
                  if message == "Stopped (silence)" else None)
        engine._on_silence_stop()
        assert engine.is_recording is False
        assert seen == [("Stopped (silence)", False)]           # finish FIRST
        assert engine.notice == "Stopped (silence)"
        assert wait_for(lambda: engine.get_history())   # pipeline still runs

    def test_silence_callback_is_inert_when_not_recording(self):
        engine = make_engine()
        engine._on_silence_stop()                  # e.g. stale callback
        assert engine.notice == ""
        assert engine.is_recording is False


class FakeInjectorWithUndo:
    """FakeInjector monkeypatched with an undo_last that routes to the real logic."""

    def __init__(self, status="typed", detail="typed"):
        from injector import Injector
        real = Injector(platform=FakePlatform(), sleep=lambda _s: None)
        self._real = real
        self.status, self.detail = status, detail
        self.calls = []

    def inject(self, text, mode="auto", append_trailing_space=True, guard=None):
        self.calls.append(text)
        return self.status, self.detail

    def undo_last(self, platform=None):
        return self._real.undo_last(platform)


class TestUndoChordWiring:
    """The chord setting reaches the hotkey and the endpoint routes through Injector."""

    def _engine(self, undo_chord="off", monkeypatch=None):
        engine = make_engine()
        if monkeypatch is not None:
            captured = {}

            def capture_start_hotkey():
                engine._hotkey = resonant_engine.hotkey_mod.Hotkey(
                    key=engine.settings.get("hotkey", "f9"),
                    swallow_active=lambda: bool(engine.listening
                                               and engine.models_ready
                                               and engine.settings.get("swallow_hotkey")),
                    on_press=engine._on_press,
                    on_release=engine._on_release,
                    on_emergency=engine._emergency_quit,
                    undo_chord=undo_chord,
                    on_undo=engine._on_undo,
                    platform=FakePlatform(),
                )
                captured["vks"] = engine._hotkey._undo_vks
                engine.hotkey_backend = "polling"
            monkeypatch.setattr(engine, "_start_hotkey", capture_start_hotkey)
        engine.set_listening(True)
        if monkeypatch is not None:
            engine._start_hotkey()
        return engine, captured if monkeypatch is not None else None

    def test_undo_chord_off_by_default(self, monkeypatch):
        engine, cap = self._engine(monkeypatch=monkeypatch)
        assert cap["vks"] is None

    def test_undo_chord_setting_is_forwarded_to_hotkey(self, monkeypatch):
        engine, cap = self._engine(undo_chord="ctrl+alt+scroll_lock",
                                   monkeypatch=monkeypatch)
        # VK_CONTROL=0x11, VK_MENU=0x12, VK_SCROLL=0x91.
        assert cap["vks"] == (0x11, 0x12, 0x91)

    def test_undo_endpoint_clears_the_record_and_notices(self, monkeypatch):
        engine, _cap = self._engine(monkeypatch=monkeypatch)
        engine._injector = FakeInjectorWithUndo()
        engine._injector._real._last = {"method": "type", "length": 2,
                                        "text": "hi", "ts": time.time(),
                                        "hwnd": FakePlatform().get_foreground_window()}
        ok = engine.undo_last_dictation()
        assert ok
        assert engine._injector._real._last is None
        assert "undone" in engine.notice.lower()

    def test_undo_endpoint_reports_window_changed(self, monkeypatch):
        engine, _cap = self._engine(monkeypatch=monkeypatch)
        engine._injector = FakeInjectorWithUndo()
        engine._injector._real._last = {"method": "type", "length": 2,
                                        "text": "hi", "ts": time.time(),
                                        "hwnd": 1234}
        ok = engine.undo_last_dictation()
        assert not ok
        assert "window changed" in engine.notice.lower()

    def test_undo_endpoint_reports_nothing_to_undo(self, monkeypatch):
        engine, _cap = self._engine(monkeypatch=monkeypatch)
        engine._injector = FakeInjectorWithUndo()
        ok = engine.undo_last_dictation()
        assert not ok
        assert "nothing to undo" in engine.notice.lower()


class TestNotices:
    def test_seq_advances_and_quiet_notices_do_not_toast(self):
        engine = make_engine()
        events = []
        engine.on("notice", lambda message, toast=True: events.append((message, toast)))
        start = engine.notice_seq
        engine._notice("Something failed")
        engine._notice("AI model ready", quiet=True)
        assert engine.notice_seq == start + 2
        assert events == [("Something failed", True), ("AI model ready", False)]


if __name__ == "__main__":
    pytest.main([__file__])
