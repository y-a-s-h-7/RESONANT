# Resonant — roadmap

## Shipped in this pass

* Phase 0–7 of the hardening brief: modular engine, guards, injection, startup,
  settings, logging, UI fixes, tests, docs.
* Phase 8 items that proved cheap and independent:
  **custom vocabulary** (`custom_vocabulary` → STT `initial_prompt`),
  **whole-word replacements** (`replacements`, applied after cleanup),
  **toggle mode** (`hotkey_mode="toggle"`), a **hotkey picker** and a
  **microphone picker with default-device fallback** — hold-to-talk excludes
  people with RSI or motor impairments, so toggle mode is a real accessibility
  win (W5.8).
* **Silence auto-stop** (`silence_autostop_ms`, energy-based end of speech):
  the recorder watches a short-window RMS against an adaptive noise floor and
  ends a dictation by itself after the chosen window of trailing silence — the
  last piece of old item 1. Off by default (`0`); pairs with toggle mode.
* **Undo last dictation** (`undo_chord`, tray item + optional chord):
  the Injector records `{method, length, text, ts, hwnd}` for the last injection
  in memory only and removes it on request — Ctrl+Z for a paste, Backspace ×
  length (counting UTF-16 code units, so surrogate pairs are handled correctly)
  for typed text — but only while the foreground window is unchanged and within
  30 s; otherwise the tray item explains why not.

## Optional features (Phase 8) — remaining, smallest effort first

1. **Spoken punctuation & formatting.** Deterministic pre-pass before the LLM,
   off by default: "new line", "new paragraph", "comma", "period", "question
   mark", "open/close quote". Ambiguous words like "period" are why it is opt-in.
3. **Level test** in the dashboard (live meter + "say something" before
   recording for real) and a device-list refresh when hardware is plugged in;
   the selector and default-device fallback now exist.
4. **History export** (opt-in, warns that it writes plaintext) and per-row
   delete.
5. **Faster ASR backend** behind the `Transcriber` interface, selectable
   via `asr_backend`, plus `bench_asr.py` for user-supplied WAV files. Do not
   change the default without measurements.
6. **Ollama structured output** (`format` JSON schema) to remove the
   quote-stripping hack. Keep plain output unless the bench shows a clear win.

## Backlog — not planned, deliberately not implemented

* **Selection-rewrite "command mode":** select text, hold a second hotkey, say
  "make this formal" / "fix the grammar" / "translate".
* **Per-app profiles:** build on the injection foreground snapshot — no LLM in
  terminals/IDEs, per-app trailing space and paste method.
* **Live partial-text preview:** would require a streaming ASR backend; current
  design is batch-after-release on purpose.
* **Multilingual / auto-detect:** `LANGUAGE=None`, a multilingual model and a
  per-language prompt.
* **Installer, first-run wizard, code signing:** needed before distribution;
  signing is also the answer to antivirus heuristics around keyboard hooks.
* **Speaker diarization:** not recommended for single-speaker dictation.

## Known limitations (documented, not bugs)

* Keys are neither seen nor swallowed while an elevated window has focus
  (Windows UIPI). No elevation is used by design.
* Corrections that span a chunk boundary (>150-word dictations are chunked) may
  be missed; each chunk keeps its own raw text if its guard rejects the rewrite.
* `keep_mic_open` is not implemented: an always-open stream keeps Windows'
  microphone-in-use indicator lit, which looked like a privacy problem.
* Tk has limited screen-reader support; audio cues, the pill and tray toasts
  are the accessible alternatives for state changes.
