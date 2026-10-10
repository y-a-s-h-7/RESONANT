"""Unit tests for transcriber.py gate (W1.4) — pure, numpy only."""
import numpy as np
import pytest

import transcriber as tr


def stats_from(amplitude=0.5, seconds=3.0, sr=16000):
    audio = amplitude * np.sin(2 * np.pi * 220 *
                               np.linspace(0, seconds, int(sr * seconds), endpoint=False))
    audio = audio.astype(np.float32)
    return tr.compute_stats(audio, sr), audio


class TestComputeStats:
    def test_peak_rms_reasonable(self):
        s, _ = stats_from(amplitude=0.5)
        assert 0.4 < s.peak <= 0.6
        assert 0.2 < s.rms < 0.5

    def test_duration(self):
        s, _ = stats_from(seconds=2.5)
        assert abs(s.duration_s - 2.5) < 0.01

    def test_silence_peak_below_min(self):
        s, _ = stats_from(amplitude=0.0005)
        assert s.peak < tr.MIN_PEAK_DEFAULT


class TestGate:
    def test_silence_dropped_early(self):
        s, _ = stats_from(amplitude=0.0005)
        text, why = tr.gate("Hello there.", s, [])
        assert text == "" and "too quiet" in why

    def test_real_speech_passes(self):
        s, _ = stats_from(amplitude=0.4)
        text, why = tr.gate("Hello there, my name is Akshay.", s, [])
        assert text == "Hello there, my name is Akshay." and why is None

    def test_sound_tags_only_dropped(self):
        s, _ = stats_from(amplitude=0.4)
        text, why = tr.gate("[BLANK_AUDIO]", s, [])
        assert text == "" and why is not None

    def test_inline_sound_tag_stripped(self):
        s, _ = stats_from(amplitude=0.4)
        text, why = tr.gate("[Music] hello there [Music]", s, [])
        assert text == "hello there" and why is None

    def test_thank_you_at_normal_volume_is_KEPT(self):
        s, _ = stats_from(amplitude=0.4)
        text, why = tr.gate("Thank you.", s, [])
        assert text == "Thank you." and why is None

    def test_thank_you_quiet_and_short_dropped(self):
        s, _ = stats_from(amplitude=0.008, seconds=1.0)
        text, why = tr.gate("Thank you.", s, [])
        assert text == "" and why is not None

    def test_thank_you_with_high_nospeech_dropped(self):
        s, _ = stats_from(amplitude=0.4)
        segs = [{"no_speech_prob": 0.8, "avg_logprob": -0.2, "compression_ratio": 1.1}]
        text, why = tr.gate("Thank you.", s, segs)
        assert text == "" and why is not None

    def test_tap_noise_short_quiet_no_text(self):
        s, _ = stats_from(amplitude=0.02, seconds=0.4)
        text, why = tr.gate("", s, [])
        assert text == ""


class TestSegments:
    def test_silent_segment_dropped(self):
        segs = [{"text": " hello", "no_speech_prob": 0.9,
                 "avg_logprob": -1.4, "compression_ratio": 1.2}]
        assert tr.filter_segments(segs) == []

    def test_looping_segment_dropped(self):
        segs = [{"text": " the the the", "no_speech_prob": 0.1,
                 "avg_logprob": -0.3, "compression_ratio": 3.0}]
        assert tr.filter_segments(segs) == []

    def test_good_segment_kept(self):
        segs = [{"text": " real words", "no_speech_prob": 0.05,
                 "avg_logprob": -0.4, "compression_ratio": 1.4}]
        assert len(tr.filter_segments(segs)) == 1


class FakeRunner:
    """Captures the kwargs transcriber.transcribe hands to the STT model."""

    def __init__(self):
        self.kwargs = {}

    def transcribe(self, audio, **kw):
        self.kwargs = kw
        return {"text": "", "segments": []}


class TestTranscribeOpts:
    def test_greedy_options_no_beam(self, monkeypatch):
        monkeypatch.setattr(tr, "_use_fp16", lambda *a, **k: False)
        model = FakeRunner()
        tr.transcribe(model, "audio", language="en")
        assert "beam_size" not in model.kwargs and "best_of" not in model.kwargs
        assert model.kwargs["temperature"] == 0
        assert model.kwargs["condition_on_previous_text"] is False
        assert model.kwargs["fp16"] is False

    def test_fp16_follows_helper_on_cpu_and_gpu(self, monkeypatch):
        """`auto` uses exactly what the (fake) detector reports."""
        model = FakeRunner()
        monkeypatch.setattr(tr, "_use_fp16", lambda *a, **k: False)   # CPU
        tr.transcribe(model, "audio", fp16="auto")
        assert model.kwargs["fp16"] is False
        monkeypatch.setattr(tr, "_use_fp16", lambda *a, **k: True)    # GPU
        tr.transcribe(model, "audio", fp16="auto")
        assert model.kwargs["fp16"] is True

    def test_forced_false_disables_fp16(self):
        """`stt_fp16: false` pins the CPU path without probing torch at all."""
        model = FakeRunner()
        tr.transcribe(model, "audio", fp16=False)
        assert model.kwargs["fp16"] is False

    def test_extra_kwargs_forwarded_to_model(self):
        """Extra kwargs (e.g. vad_filter for the faster backend) forward through."""
        model = FakeRunner()
        tr.transcribe(model, "audio", fp16=False, vad_filter=None,
                       initial_prompt="hello")
        assert model.kwargs["vad_filter"] is None
        assert model.kwargs["initial_prompt"] == "hello"
        assert model.kwargs["fp16"] is False
        assert model.kwargs["temperature"] == 0
        assert model.kwargs["condition_on_previous_text"] is False


class TestUseFp16:
    """The detector itself must never import real torch (brief §1 rule 5)."""

    def _fake_torch(self, monkeypatch, is_available):
        import sys
        import types
        fake = types.ModuleType("torch")
        fake.cuda = types.SimpleNamespace(is_available=lambda: is_available)
        monkeypatch.setitem(sys.modules, "torch", fake)

    def test_true_when_cuda_available(self, monkeypatch):
        self._fake_torch(monkeypatch, True)
        assert tr._use_fp16() is True

    def test_false_when_cuda_unavailable(self, monkeypatch):
        self._fake_torch(monkeypatch, False)
        assert tr._use_fp16() is False

    def test_false_when_torch_absent(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "torch", None)   # import -> ImportError
        assert tr._use_fp16() is False

    def test_false_when_cuda_probe_raises(self, monkeypatch):
        import sys

        class Broken:
            @property
            def cuda(self):
                raise RuntimeError("cuda exploded")

        monkeypatch.setitem(sys.modules, "torch", Broken())
        assert tr._use_fp16() is False

    def test_explicit_false_never_touches_torch(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "torch", None)
        assert tr._use_fp16(False) is False


if __name__ == "__main__":
    pytest.main([__file__])
