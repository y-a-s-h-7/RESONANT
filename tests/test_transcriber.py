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


class TestTranscribeOpts:
    def test_greedy_options_no_beam(self):
        captured = {}
        class FakeModel:
            def transcribe(self, audio, **kw):
                captured.update(kw)
                return {"text": "", "segments": []}
        tr.transcribe(FakeModel(), "audio", language="en")
        assert "beam_size" not in captured and "best_of" not in captured
        assert captured["temperature"] == 0
        assert captured["condition_on_previous_text"] is False


if __name__ == "__main__":
    pytest.main([__file__])
