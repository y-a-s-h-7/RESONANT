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
