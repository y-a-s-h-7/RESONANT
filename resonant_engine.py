"""Resonant — the dictation engine facade.

Pipeline: hotkey -> recorder -> STT -> Ollama cleanup -> text injection.

The engine is deliberately UI-agnostic: it never imports tkinter or pystray and
reports everything through thread-safe callback lists (events below), so the
dashboard, the pill and the tray can all attach to it.

Public API (unchanged, brief §2):
    ResonantEngine(...), on(event, fn), get_state(), set_listening(bool),
    apply_settings(dict), start(), shutdown(), get_history()

Events:  "state" | "dictation" | "level" | "log" | "notice"  (last two additive)
State keys are the old ones plus `stage`, `queue_depth`, `ollama_ok`, `notice`.
"""
import io
import os
import re
import sys
import threading
import time
from collections import deque

import numpy as np

import editor as editor_mod
import hotkey as hotkey_mod
import injector as injector_mod
import platform_win
import recorder as recorder_mod
import settings as settings_mod
import transcriber as transcriber_mod
from config import (
    APP_NAME,
    BLIP_DURATION_S,
    BLIP_FREQ,
    BLIP_VOLUME,
    BUSY_CUE_FREQ,
    DEFAULT_GROQ_API_KEY,
    DEFAULT_SETTINGS,
    LANGUAGE,
    OLLAMA_CHAT_URL,
    OLLAMA_TAGS_URL,
    SAMPLE_RATE,
)
from logging_setup import log, log_transcript

QUEUE_MAX = 2                       # W2.4: bounded FIFO; full -> "Busy" cue
HISTORY_MAX = 50
OLLAMA_HEALTH_TIMEOUT_S = 2
WARMUP_IDLE_FRACTION = 0.8          # warm up when 80% of keep_alive elapsed
HEALTH_RECHECK_S = 30


def _keep_alive_seconds(value):
    """'30m' / '1h' / '600' -> seconds (default 30 min)."""
    try:
        text = str(value).strip().lower()
        if text.endswith("m"):
            return float(text[:-1]) * 60
        if text.endswith("h"):
            return float(text[:-1]) * 3600
        if text.endswith("s"):
            return float(text[:-1])
        return float(text)
    except Exception:
        return 30 * 60


class ResonantEngine:
    def __init__(self, settings=None, platform=None, recorder=None,
                 injector=None, session_factory=None):
        self.settings = settings if settings is not None else settings_mod.load_settings()
        self._platform = platform
        self._injector = injector or injector_mod.Injector()
        self._session_factory = session_factory
        self._session = None
        self._lock = threading.RLock()
        self._listeners = {}

        # --- state (existing keys) ---
        self.listening = False
        self.is_recording = False
        self.processing = False
        self.models_ready = False
        self.model_error = None
        self.stt_model = None
        self._history = []
        self._last_level = 0.0

        # --- additive state keys (brief §2 / Phase 2) ---
        self.stage = ""
        self.notice_seq = 0
        self.queue_depth = 0
        self.ollama_ok = True
        self.notice = ""
        self.loading_model = None
        self.hotkey_backend = None
        self.last_raw = ""
        self.last_final = ""
        self.last_status = ""

        # --- internals ---
        self._model_lock = threading.Lock()      # STT transcribe is not thread-safe
        self._load_gen = 0
        self._recorder = recorder
        self._hotkey = None
        self._rec_buffer = None
        self._jobs = deque()
        self._job_lock = threading.Lock()
        self._worker = None
        self._guard = None
        self._started = False
        self._warm_lock = threading.Lock()
        self._last_warmup = 0.0
        self._ollama_model_used = None
        self.on_quit = None                      # set by the app entry point

        for notice in settings_mod.LAST_NOTICES:
            # Settings problems must be visible AND in the log: they explain
            # surprising behaviour later (W4.4).
            self._notice(notice)

    # ---------------------------------------------------------------- events
    def on(self, event, fn):
        with self._lock:
            self._listeners.setdefault(event, []).append(fn)
        return self

    def _emit(self, event, *args):
        with self._lock:
            listeners = list(self._listeners.get(event, []))
        for fn in listeners:
            try:
                fn(*args)
            except Exception as e:               # W4.1: never swallow silently
                log(f"listener error on '{event}': {e}")

    def _emit_state(self, **overrides):
        self._emit("state", self.get_state(overrides))

    def _notice(self, message, quiet=False, toast=None):
        """User-visible message (pill/toast) — additive event, kept in state.

        `quiet` keeps it out of the log. `toast` (default: the opposite of
        `quiet`) lets the tray decide whether Windows should raise a balloon —
        so "AI model ready" no longer pops a notification on every launch,
        while real failures still do. `notice_seq` lets the UI show a notice
        exactly once instead of replaying a stale one when the window returns.
        """
        self.notice = message or ""
        self.notice_seq += 1
        if not quiet:
            log(message)
        self._emit("notice", message, (not quiet) if toast is None else bool(toast))
        self._emit_state()

    def set_stage(self, stage):
        self.stage = stage or ""
        self._emit_state()

    def set_notice(self, message):
        """Public notice entry point (used by the UI for its own messages)."""
        self._notice(message)

    def get_state(self, overrides=None):
        s = {
            "app": APP_NAME,
            "listening": self.listening,
            "recording": self.is_recording,
            "processing": self.processing,
            "models_ready": self.models_ready,
            "model_error": self.model_error,
            "loading_model": self.loading_model,
            "level": self._last_level,
            "history": list(self._history),
            "settings": dict(self.settings),
            "stage": self.stage,
            "queue_depth": self.queue_depth,
            "ollama_ok": self.ollama_ok,
            "notice": self.notice,
            "notice_seq": self.notice_seq,
            "last_raw": self.last_raw,
            "last_final": self.last_final,
            "last_status": self.last_status,
            "hotkey_backend": self.hotkey_backend,
            # Smart-mode skip vs LLM-decision counters (text-free telemetry).
            "smart_decisions": editor_mod.decision_counts(),
        }
        if overrides:
            s.update(overrides)
        return s

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self.load_models, daemon=True, name="models").start()
        self._start_hotkey()
        threading.Thread(target=self._ollama_health_loop, daemon=True,
                         name="ollama-health").start()
        if self.settings.get("start_listening_on_launch"):
            self.set_listening(True)

    def shutdown(self):
        self._started = False
        try:
            self.set_listening(False)
        except Exception:
            pass
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
        try:
            if self._recorder is not None:
                self._recorder.release()
        except Exception:
            pass
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass

    # --------------------------------------------------------------- hotkey
    def _start_hotkey(self):
        key = self.settings.get("hotkey", "f9")
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                pass
        platform = self._platform
        if platform is None:
            try:
                platform = platform_win.real_platform()
            except Exception as e:
                self._notice(f"Keyboard unavailable on this platform ({e})")
                return
        self._platform = platform
        self._hotkey = hotkey_mod.Hotkey(
            key=key,
            swallow_active=lambda: bool(self.listening and self.models_ready
                                       and self.settings.get("swallow_hotkey")),
            on_press=self._on_press,
            on_release=self._on_release,
            on_emergency=self._emergency_quit,
            undo_chord=self.settings.get("undo_chord", "off"),
            on_undo=self._on_undo,
            platform=platform,
        )
        self.hotkey_backend = self._hotkey.start()
        if self.hotkey_backend == "invalid-key":
            self._notice(f"Unsupported hotkey '{key}' — falling back to f9")
            self._hotkey = hotkey_mod.Hotkey(key="f9", platform=platform,
                                             on_press=self._on_press,
                                             on_release=self._on_release,
                                             on_emergency=self._emergency_quit,
                                             swallow_active=lambda: bool(
                                                 self.listening and self.models_ready
                                                 and self.settings.get("swallow_hotkey")))
            self.hotkey_backend = self._hotkey.start()
        elif self.hotkey_backend == "polling":
            self._notice("Keyboard hook unavailable — using polling "
                         "(the hotkey can no longer be swallowed).")
        log(f"Hotkey backend: {self.hotkey_backend}")
        self._emit_state()

    def _emergency_quit(self):
        log("Emergency chord (Ctrl+Shift+Scroll Lock) — exiting.")
        if self.on_quit:
            try:
                self.on_quit()
                return
            except Exception as e:
                log(f"Emergency quit failed: {e}")

    def restart_hotkey(self):
        self._start_hotkey()

    # --------------------------------------------------------------- undo (Phase 8)
    def undo_last_dictation(self):
        """Public entry point: tray item, chord, or any other caller.

        Routes through the Injector so the same hwnd/age/modifier guards apply.
        A successful undo clears the record so a second tap can never delete
        text the user typed themselves; a refusal keeps the record for retry.
        """
        platform = self._platform_obj()
        ok, code = self._injector.undo_last(platform)
        if ok:
            self._notice("Last dictation undone.")
        elif code == "nothing":
            self._notice("Nothing to undo.")
        elif code == "window":
            self._notice("Undo skipped — the window changed since you dictated.\n"
                         "Alt-Tab back and try again (within 30 s).")
        elif code == "stale":
            self._notice("Undo skipped — more than 30 s have passed.")
        elif code == "held":
            self._notice("Undo skipped — a modifier key is still held "
                         "(Backspace would delete whole words).")
        elif code == "send":
            self._notice("Undo skipped — SendInput refused.")
        else:
            self._notice("Undo skipped.")
        return ok

    def _on_undo(self):
        """Hotkey chord fired. Only while the app is alive."""
        if self._started:
            self.undo_last_dictation()

    # --------------------------------------------------------------- models
    def load_models(self, target=None, persist=True):
        """Load an STT model. Commits the setting only on success (W4.5)."""
        target = target or self.settings.get("stt_model")
        backend = self.settings.get("asr_backend", "default")
        with self._lock:
            self._load_gen += 1
            generation = self._load_gen
        self.loading_model = target
        self.set_stage(f"Loading {target}…")
        self._emit_state()
        try:
            if sys.stdout is None or not hasattr(sys.stdout, "write"):
                try:
                    sys.stdout = open(os.devnull, "w", encoding="utf-8")
                except Exception:
                    class _SafeStream(io.StringIO):
                        def write(self, s): pass
                        def flush(self): pass
                        def isatty(self): return False
                    sys.stdout = _SafeStream()
            if sys.stderr is None or not hasattr(sys.stderr, "write"):
                try:
                    sys.stderr = open(os.devnull, "w", encoding="utf-8")
                except Exception:
                    class _SafeStream(io.StringIO):
                        def write(self, s): pass
                        def flush(self): pass
                        def isatty(self): return False
                    sys.stderr = _SafeStream()

            log(f"Loading STT '{target}' (backend {backend})…")
            if backend == "faster":
                # Faster-whisper (CTranslate2) backend — lazy import so the dev
                # venv that stays on the default whisper backend never touches it.
                # `target` is forwarded to faster_whisper.load_model (same model
                # identifiers the user already knows: base.en, small.en, …).
                import faster_stt
                model = faster_stt.load_model(target)
            else:
                import whisper
                model = whisper.load_model(target)
        except Exception as e:
            if generation == self._load_gen:
                self.loading_model = None
                self.model_error = str(e)
                self.set_stage("")
                # The setting was never committed, so the previous model stays
                # active and the next launch still uses it (W4.5).
                self._notice(f"Could not load '{target}' ({e}) — "
                             + ("keeping the current model." if self.stt_model
                                else "dictation is unavailable."))
                self._emit_state()
            log(f"MODEL LOAD FAILED ({target}): {e}")
            return False
        if generation != self._load_gen:
            log(f"Discarding superseded model '{target}'.")
            return False
        with self._model_lock:
            self.stt_model = model
        self.models_ready = True
        self.model_error = None
        self.loading_model = None
        if persist and target != self.settings.get("stt_model"):
            self.settings["stt_model"] = target
            settings_mod.save_settings(self.settings)
        self.set_stage("")
        self._notice(f"AI model ready ({target})", quiet=True)
        log(f"STT model loaded ({target}).")
        threading.Thread(target=self.warmup_ollama, daemon=True,
                         name="ollama-warmup").start()
        return True

    # ---------------------------------------------------------------- audio
    def play_blip(self, freq=BLIP_FREQ, duration=BLIP_DURATION_S,
                  volume=BLIP_VOLUME, repeats=1):
        """Soft faded sine blip (no click). Same sound on press and release."""
        if not self.settings.get("sound_feedback"):
            return

        def _play():
            try:
                import sounddevice as sd
                for i in range(repeats):
                    t = np.linspace(0, duration, int(SAMPLE_RATE * duration),
                                    endpoint=False)
                    wave = volume * np.sin(2 * np.pi * freq * t)
                    fade = max(1, int(SAMPLE_RATE * 0.012))
                    env = np.ones_like(wave)
                    env[:fade] = np.linspace(0, 1, fade)
                    env[-fade:] = np.linspace(1, 0, fade)
                    sd.play((wave * env).astype(np.float32).reshape(-1, 1), SAMPLE_RATE)
                    time.sleep(duration)
                    if i + 1 < repeats:
                        time.sleep(0.06)
            except Exception:
                pass
        threading.Thread(target=_play, daemon=True, name="blip").start()

    # ------------------------------------------------------------- recorder
    def _ensure_recorder(self):
        if self._recorder is None:
            self._recorder = recorder_mod.Recorder(sample_rate=SAMPLE_RATE)
        return self._recorder

    def _platform_obj(self):
        if self._platform is None:
            self._platform = platform_win.real_platform()
        return self._platform

    def _on_press(self):
        """Hotkey down. Order matters (W2.1): open the stream FIRST."""
        if not self.listening or not self.ready_for_recording():
            return
        if self.settings.get("hotkey_mode") == "toggle" and self.is_recording:
            # Toggle mode: this press is the stop signal (W2.2b).
            self._finish_recording()
            return
        if self.is_recording or self._busy():
            if self._busy():
                self.play_blip(freq=BUSY_CUE_FREQ, volume=0.05)
                self._notice("Busy — still finishing the previous dictation.")
            return
        if not self.models_ready:
            self._notice("Still loading the AI model — one moment.")
            return
        self.is_recording = True
        self._rec_buffer = recorder_mod.RecordingBuffer()
        recorder = self._ensure_recorder()
        try:
            recorder.start(buffer=self._rec_buffer,
                           device=self.settings.get("input_device"),
                           max_seconds=self.settings.get("max_record_seconds"),
                           release_tail_ms=self.settings.get("release_tail_ms", 250),
                           on_max=self._on_max_duration,
                           silence_autostop_ms=self._silence_autostop_ms(),
                           min_peak=self.settings.get("min_peak", 0.01),
                           on_silence=self._on_silence_stop)
        except Exception as e:
            self.is_recording = False
            self._notice(f"Microphone unavailable ({e}).")
            return
        self._warm_ollama_if_stale()
        self._guard = None
        try:
            self._guard = injector_mod.ForegroundGuard(self._platform_obj())
            self._guard.snapshot()
        except Exception:
            self._guard = None
        # Everything below happens after the mic is live: blip, log, UI state.
        log("Hotkey down — recording.")
        self.play_blip()
        self.set_stage("Listening…")
        self._emit_state()

    def ready_for_recording(self):
        return True

    def _on_release(self):
        """Hotkey up. In toggle mode a release is NOT a stop signal (W2.2b)."""
        if self.settings.get("hotkey_mode") == "toggle":
            return
        self._finish_recording()

    def _finish_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False
        recorder = self._recorder
        log("Recording finished — processing.")
        self.play_blip(freq=660)
        self.set_stage("Transcribing…")
        self._emit("level", 0.0)
        if recorder is None:
            self._enqueue(None, self._guard)
            return

        def _collect():
            audio, level = recorder.wait(timeout=5)
            self._last_level = 0.0
            self._emit("level", 0.0)
            buf = getattr(recorder, "buffer", None)
            mic_error = None
            if buf is not None:
                if buf.device_fallback and audio is not None:
                    self._notice("Selected microphone was unavailable — "
                                 "used the system default.")
                elif audio is None and buf.device_error:
                    mic_error = buf.device_error
            self._enqueue(audio, self._guard, mic_error)

        threading.Thread(target=_collect, daemon=True, name="collect").start()

    def _on_max_duration(self):
        self._notice("Maximum recording length reached — processing now.")
        self.play_blip(repeats=2, volume=0.05)

    def _silence_autostop_ms(self):
        """Effective silence auto-stop window for the NEXT recording.

        `0` (the default) is off. The feature applies in toggle mode — a long
        dictation should end by itself once you stop talking — and in hold mode
        only when the user has explicitly enabled it, i.e. only when this same
        setting is non-zero: hold-to-talk is the classic behaviour and must
        never start auto-stopping on its own.
        """
        try:
            value = int(self.settings.get("silence_autostop_ms", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    def _on_silence_stop(self):
        """Recorder hit the silence window: finish the dictation by itself.

        Finish FIRST so the state is (not recording, not processing) when the
        notice lands — that is the only window in which the pill shows it.
        """
        if not self.is_recording:
            return
        self._finish_recording()
        self._notice("Stopped (silence)")

    # ------------------------------------------------------------ job queue
    def _busy(self):
        return self.processing or self.queue_depth > 0

    def _enqueue(self, audio, guard, mic_error=None):
        with self._job_lock:
            if len(self._jobs) >= QUEUE_MAX:
                full = True
            else:
                self._jobs.append((audio, guard, mic_error))
                self.queue_depth = len(self._jobs)
                full = False
            if not full and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._worker_loop,
                                                daemon=True, name="worker")
                self._worker.start()
        if full:
            self.play_blip(freq=BUSY_CUE_FREQ, volume=0.05)
            self._notice("Busy — dictation dropped (queue full).")
            self._record_history("", "", "dropped(queue-full)")
        self._emit_state()

    def _worker_loop(self):
        while True:
            with self._job_lock:
                if not self._jobs:
                    self.queue_depth = 0
                    self._worker = None
                    self._emit_state()
                    return
                audio, guard, mic_error = self._jobs.popleft()
                self.queue_depth = len(self._jobs)
            self._process(audio, guard, mic_error)

    # ------------------------------------------------------------- pipeline
    def _process(self, audio, guard, mic_error=None):
        self.processing = True
        self._emit_state()
        timings = {}
        status = "dropped(no-audio)"
        raw_text = ""
        final_text = ""
        try:
            if audio is None or len(audio) == 0:
                if mic_error:
                    # Never blame the user for a dead/hijacked microphone.
                    self._notice(f"Microphone unavailable ({mic_error}).")
                    status = "dropped(no-mic)"
                else:
                    self._notice("No audio recorded (released too fast?).")
                return
            audio_ms = len(audio) / SAMPLE_RATE * 1000
            timings["audio_ms"] = int(audio_ms)
            if audio_ms < 300:
                self._notice("Recording too short.")
                status = "dropped(too-short)"
                return

            audio = recorder_mod.trim_silence(audio, SAMPLE_RATE)
            stats = transcriber_mod.compute_stats(audio, SAMPLE_RATE)
            if stats.peak < self.settings.get("min_peak", 0.01):
                self._notice("No speech (too quiet).")
                status = "dropped(quiet)"
                return
            if not self.models_ready or self.stt_model is None:
                self._notice("Model still loading — nothing was typed.")
                status = "dropped(model-loading)"
                return

            self._warm_ollama_if_stale()
            t0 = time.monotonic()
            result = self._transcribe(audio)
            timings["transcribe_ms"] = int((time.monotonic() - t0) * 1000)

            raw_text = (result.get("text") or "").strip()
            segments = result.get("segments") or []
            gated, reason = transcriber_mod.gate(
                raw_text, stats, segments,
                min_peak=self.settings.get("min_peak", 0.01))
            if reason:
                self._notice(reason)
                status = "dropped(gate)"
                return
            raw_text = gated
            log_transcript("Heard", raw_text,
                           self.settings.get("log_transcripts"))

            raw_text = self._apply_replacements(raw_text)

            if self.settings.get("ai_cleanup_mode", "smart") != "off":
                self.set_stage("Polishing…")

            t0 = time.monotonic()
            final_text, clean_status = self._clean(raw_text)
            timings["clean_ms"] = int((time.monotonic() - t0) * 1000)
            final_text = self._apply_replacements(final_text)

            t0 = time.monotonic()
            status = self._inject(final_text, guard)
            timings["inject_ms"] = int((time.monotonic() - t0) * 1000)

            self.last_raw = raw_text
            self.last_final = final_text
            self.last_status = status
            log_transcript("Final", final_text, self.settings.get("log_transcripts"))
            log("timings " + ", ".join(f"{k}={v}" for k, v in timings.items())
                + f", status={status}, clean={clean_status}")
            if not status.startswith("typed") and not status.startswith("copied"):
                self._notice("Dictation failed — see the log.")
            elif status.startswith("copied"):
                self._notice("Text is on your clipboard.")
            self._emit("dictation", raw_text, final_text)
        except Exception as e:                       # degrades, never crashes
            log(f"PROCESS ERROR: {e}")
            self._notice(f"Processing error: {e}")
            status = "failed(error)"
        finally:
            self._record_history(raw_text, final_text, status)
            self.processing = False
            self.set_stage("")
            self._emit_state()

    def _transcribe(self, audio):
        initial_prompt = None
        vocab = self.settings.get("custom_vocabulary") or []
        if vocab:
            initial_prompt = ", ".join(vocab)[:1000]
        else:
            initial_prompt = "Dictation with punctuation. Shopping list: apples, bananas, oranges."
        backend = self.settings.get("asr_backend", "default")
        fp16 = self.settings.get("stt_fp16", "auto")
        if backend == "faster" and (fp16 == "auto" or fp16 is True):
            fp16 = True
        extra = {}
        if backend == "faster":
            extra["vad_filter"] = None           # forwarded through to faster-whisper
        with self._model_lock:
            return transcriber_mod.transcribe(
                self.stt_model, audio,
                language=LANGUAGE,
                initial_prompt=initial_prompt,
                fp16=fp16,
                **extra,
            )

    def _clean(self, raw_text):
        mode = self.settings.get("ai_cleanup_mode", "smart")
        provider = self.settings.get("llm_provider", "groq")
        if provider == "groq":
            api_key = self.settings.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "") or DEFAULT_GROQ_API_KEY
            model = self.settings.get("groq_model", "qwen/qwen3.8-27b")
            timeout_s = self.settings.get("groq_timeout_s", 10)
        else:
            api_key = ""
            model = self.settings.get("ollama_model", "llama3.2")
            timeout_s = self.settings.get("ollama_timeout_s", 10)

        final, status = editor_mod.clean(
            raw_text,
            model=model,
            mode=mode,
            min_words=self.settings.get("smart_min_words", 40),
            timeout_s=timeout_s,
            keep_alive=self.settings.get("ollama_keep_alive", "30m"),
            session=self._get_session(),
            provider=provider,
            api_key=api_key,
        )
        if status != "cleaned" and mode != "off" and status != "raw:skipped-llm":
            pname = "Groq" if provider == "groq" else "Ollama"
            self._notice(f"AI cleanup unavailable ({pname}) — typed the raw transcript.")
        return final, status

    def _apply_replacements(self, text):
        """Whole-word, case-insensitive fixes for recurring ASR mistakes.

        Case-insensitive so "kubernetes" -> "Kubernetes" also fixes the
        sentence-initial/uppercase variants the ASR engine emits. The replacement goes
        through a callable so a target containing `\1` or a Windows path is
        inserted literally instead of being read as a regex backreference.
        """
        replacements = self.settings.get("replacements") or {}
        if not replacements or not text:
            return text
        for source, target in replacements.items():
            if source:
                text = re.sub(rf"\b{re.escape(source)}\b",
                              lambda _m, replacement=target: replacement, text,
                              flags=re.IGNORECASE)
        return text

    def _inject(self, text, guard):
        injector = self._injector
        try:
            status, detail = injector.inject(
                text,
                mode=self.settings.get("inject_mode", "auto"),
                append_trailing_space=self.settings.get("append_trailing_space", True),
                guard=guard,
            )
        except Exception as e:
            return f"failed({e})"
        if status == "typed":
            return "typed"
        if status == "copied":
            return "copied"
        if status == "dropped":
            return "dropped(empty)"
        return f"failed({detail})"

    def _record_history(self, raw, final, status):
        if status == "dropped(no-audio)":
            return                     # nothing happened: no history row, no noise
        self._history.append({
            "ts": time.strftime("%H:%M:%S"),
            "raw": raw,
            "final": final,
            "status": status,
        })
        del self._history[:-HISTORY_MAX]
        self._emit_state()

    # ------------------------------------------------------------- ollama
    def _get_session(self):
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                import requests
                self._session = requests.Session()
        return self._session

    def check_ollama(self):
        """Health check (GET /api/tags). Updates `ollama_ok`."""
        try:
            session = self._get_session()
            resp = session.get(OLLAMA_TAGS_URL, timeout=OLLAMA_HEALTH_TIMEOUT_S)
            ok = resp.status_code == 200
        except Exception:
            ok = False
        if ok != self.ollama_ok:
            self.ollama_ok = ok
            self._emit_state()
        return ok

    def _ollama_health_loop(self):
        backoff = 5
        while self._started:
            if self.settings.get("llm_provider", "groq") != "ollama":
                time.sleep(HEALTH_RECHECK_S)
                continue
            if not self.check_ollama():
                time.sleep(backoff)
                backoff = min(backoff * 2, HEALTH_RECHECK_S)
                continue
            backoff = 5
            if self.settings.get("ai_cleanup_mode", "smart") != "off":
                self.warmup_ollama()
            time.sleep(HEALTH_RECHECK_S)

    def _warm_ollama_if_stale(self):
        """Overlap model cold-load with the user's speech (W4.7)."""
        if self.settings.get("llm_provider", "groq") != "ollama":
            return
        if self.settings.get("ai_cleanup_mode", "smart") == "off":
            return
        keep_alive = _keep_alive_seconds(self.settings.get("ollama_keep_alive", "30m"))
        if time.monotonic() - self._last_warmup < keep_alive * WARMUP_IDLE_FRACTION:
            return
        threading.Thread(target=self.warmup_ollama, daemon=True,
                         name="ollama-warmup").start()

    def warmup_ollama(self, force=False):
        if self.settings.get("llm_provider", "groq") != "ollama":
            return True
        if self.settings.get("ai_cleanup_mode", "smart") == "off":
            return False
        model = self.settings.get("ollama_model", "llama3.2")
        with self._warm_lock:
            if not force and model == self._ollama_model_used and \
                    time.monotonic() - self._last_warmup < 60:
                return True
            try:
                session = self._get_session()
                session.post(OLLAMA_CHAT_URL, json={
                    "model": model,
                    "messages": [{"role": "user", "content": "OK"}],
                    "stream": False,
                    "keep_alive": self.settings.get("ollama_keep_alive", "30m"),
                    "options": {"num_predict": 1},
                }, timeout=120)
                self._last_warmup = time.monotonic()
                self._ollama_model_used = model
                if self.ollama_ok is not True:
                    self.ollama_ok = True
                    self._emit_state()
                log("AI editor warmed up.")
                return True
            except Exception:
                if self.ollama_ok is not False:
                    self.ollama_ok = False
                    self._emit_state()
                log("Ollama not reachable — raw transcripts until it returns.")
                return False

    # --------------------------------------------------------------- control
    def set_listening(self, enabled: bool):
        self.listening = bool(enabled)
        if not enabled:
            self.is_recording = False
        self._emit_state()
        log("Listening started." if enabled else "Listening paused.")

    def apply_settings(self, new_settings: dict):
        """Validate + persist + apply. Hotkey changes restart the hook."""
        changed = {}
        for key, value in (new_settings or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            ok, coerced, reason = settings_mod.validate(key, value)
            if ok:
                changed[key] = coerced
            elif reason:
                self._notice(reason)
        if not changed:
            return False
        with self._lock:
            self.settings.update(changed)
            settings_mod.save_settings(self.settings)
        if "hotkey" in changed or "swallow_hotkey" in changed:
            self.restart_hotkey()
        if "ollama_model" in changed:
            self._last_warmup = 0.0
            threading.Thread(target=self.warmup_ollama, daemon=True,
                             name="ollama-warmup").start()
        self._emit_state()
        self._emit("log", "Settings saved.")
        return True

    def change_model(self, model, persist=True):
        """Ask for a model switch; last request wins (W4.5)."""
        return self.load_models(target=model, persist=persist)

    def clear_history(self):
        self._history = []
        self.last_raw = self.last_final = ""
        self.last_status = ""
        self._emit_state()

    def get_history(self):
        return list(self._history)


# Smoke test: python resonant_engine.py --smoke
if __name__ == "__main__":
    import sys

    eng = ResonantEngine()
    if "--smoke" in sys.argv:
        print("engine smoke OK")
        raise SystemExit(0)
    eng.on("state", lambda s: print("STATE:", {k: s[k] for k in
             ("listening", "recording", "processing", "models_ready")}))
    eng.on("log", print)
    eng.on("dictation", lambda raw, final: print(f"DICTATION:\n  raw:   {raw}\n  final: {final}"))
    eng.start()
    print(f"{APP_NAME} engine running — hold F9 to dictate.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.shutdown()
