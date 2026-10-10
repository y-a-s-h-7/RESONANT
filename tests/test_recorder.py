"""Unit tests for recorder.py (W2.1) — fake stream, no audio hardware."""
import threading
import time

import numpy as np
import pytest

import recorder as rec


class FakeStream:
    """Feeds scripted chunks on a timer; records that it was entered/closed."""
    instances = []

    def __init__(self, callback, samplerate, device, chunks=None, chunk_interval=0.01):
        self.cb = callback
        self.samplerate = samplerate
        self.device = device
        self.chunks = chunks or []
        self.interval = chunk_interval
        self.closed = False
        self.entered = False
        FakeStream.instances.append(self)

    def __enter__(self):
        self.entered = True
        self._t = threading.Thread(target=self._feed, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a):
        self.closed = True

    def _feed(self):
        for c in self.chunks:
            if self.closed:
                return
            self.cb(c, len(c), None, None)
            time.sleep(self.interval)
        # keep the stream "open" until closed (like a real mic)
        while not self.closed:
            time.sleep(0.005)


def make_factory(chunks):
    def factory(cb, sr, device):
        return FakeStream(cb, sr, device, chunks)
    return factory


def chunk(seconds=0.05, amplitude=0.3, sr=16000):
    return (amplitude * np.sin(2 * np.pi * 200 *
            np.linspace(0, seconds, int(sr * seconds), endpoint=False))
            ).astype(np.float32).reshape(-1, 1)


class TestRecorder:
    def test_fresh_buffer_and_capture(self):
        r = rec.Recorder(stream_factory=make_factory([chunk(), chunk(), chunk()]))
        buf = r.start()                       # default fresh buffer
        assert buf is not None and buf is r.buffer
        out, level = r.wait(timeout=5)
        assert out is not None and len(out) > 0

    def test_release_tail_captured(self):
        # chunks keep flowing; after release() the tail chunk must still land
        stop_soon = [chunk() for _ in range(3)]
        tail = [chunk(amplitude=0.5)]
        r = rec.Recorder(stream_factory=make_factory(stop_soon + tail))
        r.start(release_tail_ms=120)
        time.sleep(0.08)                      # let the head chunks arrive
        r.release()
        out, _ = r.wait(timeout=5)
        assert out is not None
        # with the tail, we should have more than the 3 head chunks' worth
        assert out.shape[0] >= int(0.05 * 16000) * 3

    def test_max_duration_autostop(self):
        class EndlessStream(FakeStream):
            def _feed(self):
                while self.closed is False:
                    self.cb(chunk(), 800, None, None)
                    time.sleep(0.005)
        def fac(cb, sr, device):
            return EndlessStream(cb, sr, device, [])
        r = rec.Recorder(stream_factory=fac)
        hit = []
        r.start(max_seconds=0.2, on_max=lambda: hit.append(1))
        out, _ = r.wait(timeout=5)
        assert r.max_hit is True and hit == [1]
        assert out is not None

    def test_device_error_recorded(self):
        def bad_factory(cb, sr, device):
            raise OSError("mic busy")
        r = rec.Recorder(stream_factory=bad_factory)
        buf = r.start()
        out, _ = r.wait(timeout=2)
        assert out is None
        assert "mic busy" in buf.device_error

    def test_configured_device_failure_falls_back_to_default(self):
        tried = []

        def factory(cb, sr, device):
            tried.append(device)
            if device is not None:
                raise OSError("device 7 not found")
            return FakeStream(cb, sr, device, [chunk(), chunk()])

        r = rec.Recorder(stream_factory=factory)
        buf = r.start(device=7, release_tail_ms=10)
        time.sleep(0.06)
        r.release()
        out, _ = r.wait(timeout=5)
        assert tried == [7, None]                    # retried once on the default
        assert buf.device_fallback is True
        assert out is not None and len(out) > 0      # dictation still works
        assert "not found" in buf.device_error       # and the user is told why

    def test_default_device_failure_is_not_retried(self):
        tries = []

        def factory(cb, sr, device):
            tries.append(device)
            raise OSError("no mic at all")

        r = rec.Recorder(stream_factory=factory)
        buf = r.start()
        out, _ = r.wait(timeout=2)
        assert tries == [None]                       # no pointless second attempt
        assert out is None
        assert buf.device_fallback is False
        assert "no mic at all" in buf.device_error

    def test_snapshot_is_private_copy(self):
        r = rec.Recorder(stream_factory=make_factory([chunk(), chunk()]))
        buf = r.start()
        out, _ = r.wait(timeout=5)
        first = out.copy()
        buf.add(chunk(amplitude=9.0))         # late-arriving audio
        out2, _ = buf.snapshot()
        assert not np.array_equal(first, out2)  # snapshot() reflects new adds
        assert first.shape[0] < out2.shape[0]


class ScriptedStream(FakeStream):
    """Repeats a pattern forever (like a live mic) until the recorder closes."""

    def __init__(self, callback, samplerate, device, pattern, interval=0.01):
        super().__init__(callback, samplerate, device, [], interval)
        self.pattern = pattern

    def _feed(self):
        i = 0
        while not self.closed:
            block = self.pattern[i % len(self.pattern)]
            self.cb(block, len(block), None, None)
            i += 1
            time.sleep(self.interval)


class TestSilenceAutostop:
    """`silence_autostop_ms` (Phase 8) — energy end-of-speech, fake stream.

    Every chunk is 50 ms of audio fed every 10 ms, so 6 quiet chunks = the
    0.3 s mid-sentence breath and 10 quiet chunks = 500 ms of trailing silence.
    """

    QUIET = 0.0005            # rms ~0.00035 — far below min_peak

    @staticmethod
    def factory(pattern, endless=False):
        if endless:
            return lambda cb, sr, d: ScriptedStream(cb, sr, d, pattern)
        return lambda cb, sr, d: FakeStream(cb, sr, d, pattern)

    def test_stops_after_trailing_silence(self):
        pattern = ([chunk() for _ in range(6)]                    # 0.3 s speech
                   + [chunk(amplitude=self.QUIET) for _ in range(60)])
        hits = []
        r = rec.Recorder(stream_factory=self.factory(pattern))
        r.start(silence_autostop_ms=500, release_tail_ms=10,
                on_silence=lambda: hits.append(1))
        out, _ = r.wait(timeout=5)
        assert r.silence_hit is True
        assert hits == [1]                        # reported exactly once
        assert r.max_hit is False                 # and not via the max guard
        assert out is not None and out.shape[0] > 0

    def test_mid_sentence_pause_shorter_than_limit_does_not_stop(self):
        # 0.5 s speech, then a 0.3 s breath — repeated for seconds of audio.
        pattern = ([chunk() for _ in range(10)]
                   + [chunk(amplitude=self.QUIET) for _ in range(6)])
        hits = []
        r = rec.Recorder(stream_factory=self.factory(pattern, endless=True))
        r.start(silence_autostop_ms=500, release_tail_ms=10,
                on_silence=lambda: hits.append(1))
        time.sleep(1.0)                            # ~6 pause cycles fed
        assert r.silence_hit is False and hits == []
        r.release()
        r.wait(timeout=5)
        assert r.silence_hit is False and hits == []

    def test_never_stops_before_first_speech(self):
        # Seconds of silence with no speech yet: quiet time must not count.
        pattern = [chunk(amplitude=self.QUIET)]
        hits = []
        r = rec.Recorder(stream_factory=self.factory(pattern, endless=True))
        r.start(silence_autostop_ms=500, release_tail_ms=10,
                on_silence=lambda: hits.append(1))
        time.sleep(1.2)                            # ~120 quiet frames fed
        assert r.silence_hit is False and hits == []
        r.release()
        r.wait(timeout=5)
        assert r.silence_hit is False and hits == []
        assert r._detector.speech_seen is False

    def test_off_by_default_never_stops(self):
        pattern = ([chunk() for _ in range(6)]
                   + [chunk(amplitude=self.QUIET) for _ in range(60)])
        hits = []
        r = rec.Recorder(stream_factory=self.factory(pattern))
        r.start(release_tail_ms=10, on_silence=lambda: hits.append(1))
        time.sleep(1.0)                            # all 66 chunks fed
        assert r._detector is None                 # 0 = off (default)
        assert r.silence_hit is False and hits == []
        r.release()
        r.wait(timeout=5)
        assert r.silence_hit is False

    def test_noise_floor_adapts_to_the_room(self):
        # Background at rms ~0.014 sits BELOW the initial 0.025 gate, so the
        # floor climbs and the gate with it (~0.035). A later rms ~0.028 is
        # louder than the *initial* gate (0.025) but quieter than the adapted
        # one — only an adaptive threshold recognises it as trailing silence.
        background = 0.02                          # sine amplitude -> rms 0.014
        pattern = ([chunk(amplitude=background) for _ in range(60)]   # 3 s calibrate
                   + [chunk() for _ in range(6)]                      # speech
                   + [chunk(amplitude=0.04) for _ in range(30)])      # rms 0.028
        hits = []
        r = rec.Recorder(stream_factory=self.factory(pattern))
        r.start(silence_autostop_ms=500, release_tail_ms=10,
                on_silence=lambda: hits.append(1))
        out, _ = r.wait(timeout=5)
        assert r.silence_hit is True and hits == [1]
        assert r._detector.speech_seen is True
        assert r._detector.threshold > 0.03       # adapted up from 0.025
        assert out is not None

    def test_max_length_still_wins_over_silence(self):
        pattern = [chunk()] * 400                  # continuous speech, no quiet
        hits = []
        r = rec.Recorder(stream_factory=self.factory(pattern))
        r.start(max_seconds=0.3, release_tail_ms=10, silence_autostop_ms=500,
                on_silence=lambda: hits.append(1))
        r.wait(timeout=5)
        assert r.max_hit is True                   # max_record_seconds respected
        assert r.silence_hit is False and hits == []

    def test_detector_threshold_never_below_min_peak(self):
        det = rec.SpeechDetector(500, min_peak=0.01)
        assert det.threshold == pytest.approx(0.025)      # min_peak * factor
        for _ in range(60):                               # calibrate on the room
            assert det.observe(0.0141, 0.05) is False
        assert det.speech_seen is False
        assert det.threshold > 0.03                       # adapted upwards
        assert det.observe(0.21, 0.05) is False           # speech, not a stop
        assert det.speech_seen is True
        stopped = False
        for _ in range(10):                               # 10 x 50 ms = 500 ms
            stopped = det.observe(0.028, 0.05) or stopped
        assert stopped is True
        assert det.observe(0.028, 0.05) is False          # fires once


class TestTrim:
    def test_trims_with_padding(self):
        sr = 16000
        audio = np.zeros(sr, dtype=np.float32)
        audio[int(0.5 * sr):int(1.0 * sr)] = 0.5
        out = rec.trim_silence(audio, sr, pad_s=0.1)
        # speech 0.5s + 0.1s head pad; tail pad is clipped at array end -> 0.6s
        assert int(0.55 * sr) <= out.shape[0] <= int(0.65 * sr)

    def test_keeps_internal_gap(self):
        sr = 16000
        audio = np.zeros(sr, dtype=np.float32)
        audio[int(0.2 * sr):int(0.4 * sr)] = 0.5
        audio[int(0.7 * sr):int(0.9 * sr)] = 0.5
        out = rec.trim_silence(audio, sr, pad_s=0.05)
        assert int(0.5 * sr) < out.shape[0]               # gap preserved

    def test_all_quiet_returns_input(self):
        audio = np.full(8000, 0.001, dtype=np.float32)
        out = rec.trim_silence(audio)
        assert out.shape[0] == audio.shape[0]

    def test_loud_everything_unchanged(self):
        audio = np.full(8000, 0.5, dtype=np.float32)
        out = rec.trim_silence(audio)
        assert out.shape[0] == audio.shape[0]


if __name__ == "__main__":
    pytest.main([__file__])
