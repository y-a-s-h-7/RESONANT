"""Resonant — recorder (W2.1).

Capture contract (fixes the clipping/race bugs):
  * The hotkey thread allocates a FRESH RecordingBuffer and passes it to
    `Recorder.start()` — no shared mutable state with a previous recording.
  * `start()` opens the mic stream BEFORE any logging/blips/state emits, so the
    first word is never clipped.
  * After `release()`, the stream keeps capturing for `release_tail_ms` before
    closing, so trailing plosives survive.
  * `max_record_seconds` auto-stops with a double-blip signal.
  * Level meter (0..1) updated from the audio callback.

The sounddevice stream is injected so tests use a fake; nothing here imports
tkinter or touches the keyboard.
"""
import threading
import time

import numpy as np

# ------------------------------------------------------------------ constants
POLL_S = 0.02                  # capture-loop sleep
# Note: a failed device open is recovered on the system default (device=None);
# `RecordingBuffer.device_fallback` reports that, `device_error` the failure.
RELEASE_TAIL_DEFAULT_MS = 250
MAX_RECORD_DEFAULT_S = 120
TRIM_PAD_S = 0.2               # padding kept each side after adaptive trim


class RecordingBuffer:
    """One recording's private buffer. Fresh object per recording (W2.1)."""

    def __init__(self):
        self.chunks = []
        self.level = 0.0
        self.device_error = None
        self.device_fallback = False   # recovered on the system default device
        self.lock = threading.Lock()

    def add(self, indata):
        with self.lock:
            self.chunks.append(indata.copy())
            self.level = float(np.sqrt(np.mean(indata ** 2)) * 4)

    def snapshot(self):
        """Private numpy snapshot for processing; the buffer can then be reused."""
        with self.lock:
            if not self.chunks:
                return None, 0.0
            return np.concatenate(self.chunks, axis=0).flatten(), self.level


class Recorder:
    """Opens an InputStream per recording. `stream_factory` is injectable:

        stream_factory(callback, samplerate, device) -> context manager
    """

    def __init__(self, sample_rate=16000, stream_factory=None):
        self.sample_rate = sample_rate
        self._stream_factory = stream_factory or self._sd_stream_factory
        self._stop_event = threading.Event()
        self._thread = None
        self.buffer = None
        self.max_seconds = MAX_RECORD_DEFAULT_S
        self.max_hit = False            # set when auto-stop fired

    @staticmethod
    def _sd_stream_factory(callback, samplerate, device):
        import sounddevice as sd
        return sd.InputStream(samplerate=samplerate, channels=1,
                              dtype="float32", callback=callback,
                              device=device)

    # ------------------------------------------------------------------ API
    def start(self, buffer=None, device=None, max_seconds=None,
              release_tail_ms=RELEASE_TAIL_DEFAULT_MS, on_max=None):
        """Open the stream FIRST (call before any blips/logging)."""
        self.buffer = buffer or RecordingBuffer()
        self.max_seconds = max_seconds or MAX_RECORD_DEFAULT_S
        self.max_hit = False
        self._on_max = on_max
        self._release_tail_s = release_tail_ms / 1000.0
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, args=(device,), daemon=True, name="recorder")
        self._thread.start()
        return self.buffer

    def release(self):
        """Key-up: signal the loop to capture the release tail, then close."""
        self._stop_event.set()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)
        snap = self.buffer.snapshot() if self.buffer else (None, 0.0)
        return snap                       # (audio or None, last_level)

    # ---------------------------------------------------------------- loop
    def _capture_loop(self, device):
        buf = self.buffer
        try:
            stream = self._stream_factory(self._callback, self.sample_rate, device)
        except Exception as e:
            buf.device_error = str(e)
            if device is None:
                return
            # A configured device that has gone away (unplugged headset, USB
            # indexes shifting) must not end dictation: fall back to the system
            # default once and tell the caller, which surfaces a notice.
            try:
                stream = self._stream_factory(self._callback, self.sample_rate, None)
            except Exception as e2:
                buf.device_error = f"{e}; default device also failed ({e2})"
                return
            buf.device_fallback = True
        with stream:
            start_t = time.monotonic()
            tail_done = False
            while True:
                if not self._stop_event.is_set():
                    if time.monotonic() - start_t >= self.max_seconds:
                        self.max_hit = True
                        self._stop_event.set()
                        if self._on_max:
                            try:
                                self._on_max()
                            except Exception:
                                pass
                    else:
                        time.sleep(POLL_S)
                        continue
                # stop requested: capture the release tail, then exit
                if tail_done:
                    break
                time.sleep(self._release_tail_s)
                tail_done = True

    def _callback(self, indata, frames, time_info, status):
        self.buffer.add(indata)


# ------------------------------------------------------------------ trimming
def trim_silence(audio, sample_rate=16000, pad_s=TRIM_PAD_S,
                 floor_factor=2.5, absolute_min=0.008):
    """Adaptive, noise-floor-aware trim (W2.1).

    Noise floor = median |amplitude|. Speech must exceed floor * floor_factor
    (and never below `absolute_min`). Keeps ~pad_s each side; zero-padding is
    never added. Falls back to the input when nothing crosses the threshold.
    """
    if audio is None or len(audio) == 0:
        return audio
    mag = np.abs(audio)
    # Low percentile (not median): robust both when silence dominates AND when
    # speech fills most of a short clip (median would sit inside the speech).
    floor = float(np.percentile(mag, 10))
    threshold = max(floor * floor_factor, absolute_min)
    idx = np.where(mag > threshold)[0]
    if len(idx) == 0:
        return audio
    start = max(0, int(idx[0] - pad_s * sample_rate))
    end = min(len(audio), int(idx[-1] + 1 + pad_s * sample_rate))
    return audio[start:end]
