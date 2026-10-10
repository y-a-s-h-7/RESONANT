"""Resonant — recorder (W2.1).

Capture contract (fixes the clipping/race bugs):
  * The hotkey thread allocates a FRESH RecordingBuffer and passes it to
    `Recorder.start()` — no shared mutable state with a previous recording.
  * `start()` opens the mic stream BEFORE any logging/blips/state emits, so the
    first word is never clipped.
  * After `release()`, the stream keeps capturing for `release_tail_ms` before
    closing, so trailing plosives survive.
  * `max_record_seconds` auto-stops with a double-blip signal.
  * Optional silence auto-stop (`silence_autostop_ms`): once speech has been
    heard, trailing quiet for that long ends the recording through the SAME
    release-tail path, so nothing is clipped. Energy detector is swappable
    (see `SpeechDetector`).
  * Level meter (0..1) updated from the audio callback.

The sounddevice stream is injected so tests use a fake; nothing here imports
tkinter or touches the keyboard.
"""
import threading
import time

import numpy as np

from config import SILENCE_OFF

# ------------------------------------------------------------------ constants
POLL_S = 0.02                  # capture-loop sleep
# Note: a failed device open is recovered on the system default (device=None);
# `RecordingBuffer.device_fallback` reports that, `device_error` the failure.
RELEASE_TAIL_DEFAULT_MS = 250
MAX_RECORD_DEFAULT_S = 120
TRIM_PAD_S = 0.2               # padding kept each side after adaptive trim

# ------------------------------------------- silence auto-stop (energy VAD)
AUTOSTOP_FLOOR_FACTOR = 2.5    # quiet threshold = max(noise_floor * factor, min_peak)
AUTOSTOP_NOISE_ALPHA = 0.1     # EMA weight of the adaptive noise floor
AUTOSTOP_MIN_PEAK = 0.01       # threshold can never fall below this
# `SILENCE_OFF` (0) and the valid 500-5000 ms range live in config.py, which
# settings.py validates against.


class SpeechDetector:
    """End-of-speech decisions for ONE recording.

    Deliberately a small interface so a real VAD (webrtcvad, silero, …) can
    replace the energy implementation later without touching the recorder:

        detector = SpeechDetector(autostop_ms, min_peak)  # or any object with:
        detector.reset()                                  # fresh recording
        detector.observe(rms, seconds) -> bool            # True = speech ended
        detector.speech_seen -> bool

    Energy rules:
      * the noise floor is an EMA of frames already below the threshold, so it
        tracks the room (adaptive) and can never chase speech — speech frames
        are never fed to it;
      * the threshold is `max(noise_floor * factor, min_peak)`, so it is never
        lower than the engine's own quiet gate;
      * speech counts as detected only at or above the threshold;
      * end-of-speech fires only AFTER the first speech frame and after the
        configured quiet has accumulated — silence before the first word can
        never stop a recording.
    """

    def __init__(self, autostop_ms, min_peak=AUTOSTOP_MIN_PEAK):
        self.autostop_ms = int(autostop_ms or SILENCE_OFF)
        self.min_peak = float(min_peak)
        self._floor_factor = AUTOSTOP_FLOOR_FACTOR
        self._alpha = AUTOSTOP_NOISE_ALPHA
        self.reset()

    def reset(self):
        """Start of a recording: no speech heard yet, quiet never counts."""
        self._noise_floor = self.min_peak
        self._quiet_s = 0.0
        self._speech_seen = False
        self._fired = False

    @property
    def speech_seen(self):
        return self._speech_seen

    @property
    def noise_floor(self):
        return self._noise_floor

    @property
    def threshold(self):
        """Current quiet gate: adaptive, but never below `min_peak`."""
        return max(self._noise_floor * self._floor_factor, self.min_peak)

    def observe(self, rms, seconds):
        """Feed one short window. Returns True exactly once, at end of speech."""
        if self.autostop_ms <= SILENCE_OFF or self._fired:
            return False
        if rms >= self.threshold:
            self._speech_seen = True
            self._quiet_s = 0.0        # any loud frame resumes speech
            return False
        # Quiet frame: adapt the floor (calibrates before speech too) …
        self._noise_floor += self._alpha * (rms - self._noise_floor)
        if not self._speech_seen:
            return False               # … but never stop before the first word
        # … then accumulate quiet time, only after speech.
        self._quiet_s += max(0.0, seconds)
        # 1e-6 ms of slack: 10 x 0.05 s sums to 499.99999999999994 ms in
        # binary floating point, which must still count as 500 ms.
        if self._quiet_s * 1000.0 >= self.autostop_ms - 1e-6:
            self._fired = True
            return True
        return False


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
        self.silence_hit = False        # set when silence auto-stop fired
        self._detector = None           # SpeechDetector | None (off when 0 ms)
        self._on_silence = None
        self._stop_notified = False     # report the stop cause exactly once

    @staticmethod
    def _sd_stream_factory(callback, samplerate, device):
        import sounddevice as sd
        return sd.InputStream(samplerate=samplerate, channels=1,
                              dtype="float32", callback=callback,
                              device=device)

    # ------------------------------------------------------------------ API
    def start(self, buffer=None, device=None, max_seconds=None,
              release_tail_ms=RELEASE_TAIL_DEFAULT_MS, on_max=None,
              silence_autostop_ms=SILENCE_OFF, min_peak=AUTOSTOP_MIN_PEAK,
              on_silence=None):
        """Open the stream FIRST (call before any blips/logging).

        `silence_autostop_ms` > 0 arms energy-based end-of-speech; `on_silence`
        is invoked once (from the capture thread) when it fires.
        """
        self.buffer = buffer or RecordingBuffer()
        self.max_seconds = max_seconds or MAX_RECORD_DEFAULT_S
        self.max_hit = False
        self.silence_hit = False
        self._on_max = on_max
        self._on_silence = on_silence
        self._stop_notified = False
        self._detector = (SpeechDetector(silence_autostop_ms, min_peak)
                          if silence_autostop_ms and silence_autostop_ms > SILENCE_OFF
                          else None)
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
                # stop requested (key-up, max length OR silence auto-stop):
                # report the cause once, then capture the release tail so the
                # last word is never clipped.
                if not self._stop_notified:
                    self._stop_notified = True
                    if self.silence_hit and self._on_silence:
                        try:
                            self._on_silence()
                        except Exception:
                            pass
                if tail_done:
                    break
                time.sleep(self._release_tail_s)
                tail_done = True

    def _silence_stop(self):
        """End-of-speech: reuse the release-tail path (`release()` does this)."""
        if self.silence_hit:
            return
        self.silence_hit = True
        self._stop_event.set()

    def _callback(self, indata, frames, time_info, status):
        self.buffer.add(indata)
        detector = self._detector
        if detector is None or self.silence_hit:
            return
        block = np.asarray(indata).reshape(-1)
        if not block.size:
            return
        # Short-window RMS: one callback block IS the window (~30-50 ms).
        rms = float(np.sqrt(np.mean(block * block)))
        if detector.observe(rms, block.size / float(self.sample_rate)):
            self._silence_stop()


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
