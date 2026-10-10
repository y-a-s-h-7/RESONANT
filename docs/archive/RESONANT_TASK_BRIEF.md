# TASK BRIEF — Resonant: hardening, reliability & UX pass

You are a senior Python / Windows-desktop engineer working inside this repository. Execute this brief in **Planning mode**. It comes from a detailed code review. Every item names the file and function so you can find it. Line numbers refer to the original files and will drift as you edit, so search by function name.

---

## 0. What Resonant is

A Windows-only, 100% local push-to-talk dictation app. Hold a hotkey (default `F9`), speak, release. The pipeline is: mic audio → `stt-engine` (batch, after release) → local Ollama LLM cleanup (applies spoken self-corrections like "scratch that", removes fillers, formats lists) → text typed or pasted at the cursor. UI: tkinter dashboard, floating "pill", pystray tray icon. There are no interim/partial transcripts, and that is by design.

| File | Role |
|---|---|
| `resonant.py` | entry point, single-instance lock, tray/UI wiring, startup registration |
| `resonant_engine.py` | hotkey poller, recorder, STT, Ollama cleanup, typing, history, event API |
| `resonant_ui.py` | dashboard, pill, tray |
| `config.py` | defaults, settings load/save, `SYSTEM_PROMPT`, `REFUSAL_MARKERS` |
| `bench_editor.py` | Ollama model/prompt benchmark |
| `run.bat`, `README.md` | launcher, docs |

---

## 1. Ground rules (read before doing anything)

1. **Plan first.** Produce an Implementation Plan artifact and a Task List covering every phase below. Tag each task with its work-item ID (e.g. `W1.2`). Do not write code until the plan exists. Then proceed phase by phase without waiting for approval, unless a **STOP AND ASK** trigger (§4) fires.
2. **After every phase**, run lint and tests, then produce a Walkthrough artifact covering: what changed, files touched, tests added, results, and anything you could not verify on this machine (add those to `docs/MANUAL_VERIFICATION.md`).
3. **Use the project virtualenv.** Create `.venv` if missing. Run every Python/pip command through it (`.\.venv\Scripts\python.exe -m ...`). Never install packages globally. The shell is likely PowerShell, so avoid bash-isms.
4. **DO NOT launch the full app** (`resonant.py` / `run.bat`) for automated verification. It registers a Windows Run-key entry, installs a global keyboard hook, and can `taskkill` the owner's already-running instance. Add a `--no-startup` flag and a `RESONANT_DEV=1` env var that disable startup registration and instance takeover-kill. Add `--smoke` (initialise settings, engine and UI modules without hooks, tray, registry or lock; exit 0). Use `--smoke` and unit tests for verification. Ask before any real launch.
5. **Never touch the real registry, clipboard, keyboard or microphone in tests.** Inject fakes via a thin `platform_win.py` layer (§3). Tests must run on any OS and must not import `torch` or `STT`.
6. **Privacy is a feature.** No telemetry, analytics or new network destinations. The only allowed network calls are `http://localhost:11434` (Ollama) and STT's own model download. Never log, print or commit transcripts, audio, `settings.json` or `resonant.log`. Test fixtures use synthetic text only.
7. **Treat items marked _verify_ as hypotheses** about Windows behaviour. Implement them defensively and add them to the manual checklist. Don't claim they are confirmed.
8. **Small, reviewable diffs.** Don't reformat unrelated code. Keep the flat file layout (new modules go beside the existing ones). Don't rename `resonant.py`, `run.bat` or `config.py`.
9. If a command fails twice, stop, report what you tried, and ask. Don't loop.

---

## 2. Invariants — do NOT break these

- **Engine/UI decoupling.** The engine never imports tkinter or pystray. Public API stays: `ResonantEngine.on / get_state / set_listening / apply_settings / start / shutdown / get_history`, and events `state`, `dictation`, `level`, `log`. You may **add** events (`notice`) and state keys (`stage`, `queue_depth`, `ollama_ok`, `notice`). The UI polls or queues on the Tk main thread and never mutates widgets from other threads.
- **Degrade, don't lose.** Every failure path falls back to raw transcript and/or clipboard rather than dropping a dictation. The user must now also be *told* when a fallback happens (§Phase 5).
- **Local-only.** No cloud, no API keys, audio never written to disk.
- **Non-admin operation.** No elevation. Keep the `run.bat` reasoning about UAC and instance takeover.
- **Instance lock design.** The PID lives in a separate never-locked file, and the lock handle stays module-global so it can't be garbage-collected.
- **Recording snapshot.** `_process` receives a private snapshot, so a new recording can never clobber one in flight.
- **STT serialisation** via `_model_lock`, `condition_on_previous_text=False`, forced language, and lazy `import STT` on a background thread.
- **Prompt style.** Few-shot examples, temperature 0. Small models need explicit rules and examples.
- **Blip design.** Faded sine blips with no clicks.

---

## 3. Architecture changes (enables everything below)

Split `resonant_engine.py` into cohesive modules, keeping the `ResonantEngine` facade and event API unchanged:

| New module | Responsibility |
|---|---|
| `platform_win.py` | Thin wrappers over Win32 (user32/kernel32/winreg/msvcrt): keyboard hook, `SendInput`, clipboard, foreground window, registry, named event. Every function is injectable so tests use fakes. |
| `hotkey.py` | Hotkey backends (LL hook primary, polling fallback), chord/emergency-exit handling |
| `recorder.py` | Mic capture with per-recording buffers, release tail, max duration, level meter |
| `transcriber.py` | `Transcriber` protocol; `OpenAISTTBackend` (current behaviour); silence/hallucination gate |
| `editor.py` | Ollama client + **pure** functions `should_use_llm`, `validate_rewrite`, `chunk_transcript`, `clean` (all guards live here) |
| `injector.py` | Text injection (`SendInput` Unicode, guarded clipboard paste, foreground guard) |
| `startup.py` | All Run-key logic (currently duplicated in 3 places) |
| `settings.py` | Schema, validation, migration, atomic persistence (re-export legacy names from `config.py`) |
| `logging_setup.py` | `logging` config; keep a `log(msg)` shim for compatibility |
| `resonant_cli.py` | Headless harness: `clean "<text>"`, `transcribe file.wav`, `doctor` |

`config.py` must keep exporting `APP_NAME`, path constants, `load_settings`, `save_settings`, `SYSTEM_PROMPT`, `REFUSAL_MARKERS`, `SAMPLE_RATE`, `LANGUAGE` and `OLLAMA_CHAT_URL`.

### Decisions already made (proceed with these; override only if the owner objects)

| Topic | Decision |
|---|---|
| Default hotkey | Stay `f9`, but **swallow** it while listening (LL hook); pass through when paused. Allowed keys: `f1–f24`, `pause`, `capslock`. Remove letters and digits (typing them would trigger recording). |
| AI cleanup default | New setting `ai_cleanup_mode`: `smart` (default) / `always` / `off`. `smart` skips the LLM unless cues are present or the utterance is long. |
| Typing method | New `inject_mode`: `auto` (default) / `type` / `paste`. `auto` = Unicode `SendInput` for single-line text, guarded clipboard paste for multi-line or long text. |
| Run at startup | Default **off** until the user opts in via a one-time prompt. Existing Run-key entries are respected. |
| Transcript logging | Off by default (`log_transcripts=false`). Log stage timings and counts only. |
| ASR backend | Keep `stt-engine` as default. `faster-stt` is an optional backend behind the `Transcriber` interface (Phase 8). Don't switch the default without measurements. |
| Mic handling | Open the stream on key-down, **first thing** on the hotkey thread. `keep_mic_open` (always-open ring buffer) is optional and off by default, because it keeps Windows' mic-in-use indicator lit. |

### Settings schema (validated; unknown keys ignored; bad values fall back to default with a `notice`)

| Key | Default | Notes |
|---|---|---|
| `hotkey` | `"f9"` | see allowed keys above |
| `hotkey_mode` | `"hold"` | `"hold"` or `"toggle"` (Phase 8) |
| `swallow_hotkey` | `true` | |
| `stt_model` | `"base.en"` | |
| `ollama_model` | `"llama3.2"` | |
| `ai_cleanup_mode` | `"smart"` | legacy `ai_cleanup`: `true→"always"`, `false→"off"` (only if it was an actual override) |
| `smart_min_words` | `40` | LLM used for long utterances even without cues |
| `inject_mode` | `"auto"` | legacy `paste_mode`: `true→"paste"`, `false→"type"` (overrides only) |
| `append_trailing_space` | `true` | |
| `sound_feedback` | `true` | |
| `start_listening_on_launch` | `true` | |
| `run_at_startup` | `false` | plus `startup_prompted: false`, `legacy_startup_migrated: false` |
| `max_record_seconds` | `120` | |
| `release_tail_ms` | `250` | keep recording after key-up |
| `min_peak` | `0.01` | silence gate (configurable, since quiet mics exist) |
| `ollama_timeout_s` | `10` | |
| `ollama_keep_alive` | `"30m"` | |
| `log_transcripts` | `false` | |
| `input_device` | `null` | default device |
| `keep_mic_open` | `false` | |
| `custom_vocabulary` | `[]` | Phase 8 |
| `replacements` | `{}` | Phase 8 |
| `spoken_commands` | `false` | Phase 8 |
| `silence_autostop_ms` | `0` | Phase 8 |
| `asr_backend` | `"stt-engine"` | Phase 8 |

---

## 4. STOP AND ASK if…

- you need to install `torch` / `stt-engine` (multi-GB). Tests must not need them, so the dev venv shouldn't either.
- a step needs admin/elevation, or would launch the full app.
- a change would alter what the app stores on disk about the user's speech.
- you would change the default hotkey, delete a dependency the owner might rely on, or point Ollama anywhere but localhost.
- a work item conflicts with an invariant in §2.

---

## 5. Work plan

### Phase 0 — Baseline & scaffolding

- **W0.1 Environment & hygiene.** Create `.venv`, `requirements.txt` (runtime), `requirements-dev.txt` (pytest, pytest-mock, ruff, plus the light runtime deps the tests import; **not** torch/STT), and `pyproject.toml` (ruff + pytest config). Add a `.gitignore` covering `.vt.*`, `_vt.*`, `*.log`, `settings.json*`, `.venv`, `__pycache__`, `*.wav`, `bench_results/`. Document the supported Python version range (don't upgrade Python).
- **W0.2 Characterisation tests** for existing pure logic (`_trim_silence`, quote stripping, `load_settings` merging) before refactoring.
- **W0.3 `platform_win.py`** with injectable Win32 wrappers (§3).
- **W0.4 `resonant_cli.py`**: `clean "<text>"` runs the full editor pipeline (guards included) with no mic. `transcribe file.wav` runs ASR + editor without typing. `doctor` prints a health report: Python version, dependencies, mic devices, Ollama reachability and model present, STT model cached, hotkey validity, startup-entry state, settings file validity.
- **W0.5 Cleanup:** remove the unused imports found by lint (`threading` in resonant.py, `sys` in engine, `time` and `CRASH_PATH` in UI), the dead `_dpi_aware` and unused `OLD_STARTUP_NAME` in the UI, and the unused `PROMPT_V2` after the bench rewrite.

### Phase 1 — Silent data loss & wrong output (P0)

- **W1.1 Truncation fix** (`_clean`; `num_predict: 200`). Scale `num_predict` with input (≈ `max(256, 2.5 × words)`) and set `num_ctx` explicitly. Read `done_reason`. If `"length"`, reject the rewrite and fall back to raw. *Accept:* a 400-word input is never returned truncated (test with a fake Ollama returning `done_reason: "length"`).
- **W1.2 Guard rewrite** in `editor.py`.
  - *False positives (bug):* `REFUSAL_MARKERS` are matched against the output only, so "I can't make it Tuesday" / "I'm sorry for the delay" get discarded and the raw text is typed. Count a marker only if it's in the output and **not** in the raw input.
  - *False negatives:* add `validate_rewrite(raw, out) -> (ok, reason)`. Reject empty output, chatbot preambles ("Here's the corrected text", "Sure"), and answered-instead-of-edited output. Reject invented content: the share of output words absent from the input must be ≤ ~0.25, checked only when the output has ≥ 6 words. Normalise numbers/ordinals/times both ways first so "three pm" ↔ "3 PM" passes. Cap length growth. Strip stray `<transcript>` tags and enclosing quotes. All thresholds are named constants.
  - *Smart mode:* `should_use_llm(raw, mode, min_words)` returns True if correction cues (`scratch that|strike that|delete that|never mind|no wait|actually|I mean|instead|cancel|remove|replace|change it to`), list cues (`first|second|third|next|list|item|bullet`), filler cues (`um|uh|er|you know|like`), stutter repeats, or length ≥ `smart_min_words` is present. When skipped, apply only a deterministic tidy (collapse whitespace, capitalise the first letter).
  - *Accept:* the parametrised tests in W6.2 all pass.
- **W1.3 Prompt fixes** (`config.SYSTEM_PROMPT`).
  - Remove "Please" from Example 4 (it violates rule 6 and teaches the model to add words).
  - Send the user turn as `<transcript>…</transcript>` and tell the model to output only the rewritten text.
  - Add three short examples, keeping the prompt compact:
    - a sentence containing "I can't" that must be preserved
    - a question that must be **cleaned, not answered** ("what time is the meeting tomorrow" → "What time is the meeting tomorrow?")
    - an already-clean sentence returned unchanged
  - If Ollama is reachable, re-run the bench (W6.1) on `llama3.2` and `qwen2.5:3b` and report before/after scores in the walkthrough. If not, say so.
- **W1.4 Silence & hallucination gate** in `transcriber.py` (`_process` only checks `if not raw_text`).
  - Compute peak/RMS before transcribing. Below `min_peak`, drop with the notice "No speech (too quiet)".
  - Use `result["segments"]`. Drop segments STT marks silent (`no_speech_prob > 0.6` and `avg_logprob < -1.0`) or looping (`compression_ratio > 2.4`).
  - Sound tags (`[BLANK_AUDIO]`, `(silence)`, `[Music]`): drop if they're the entire output, strip them if inline.
  - Known phantom phrases ("Thank you.", "Thanks for watching", "you", "bye") are dropped **only with corroborating evidence**: max `no_speech_prob > 0.3`, or audio < 2 s **and** low RMS. Never drop on the phrase alone. "Thank you." is a legitimate dictation.
  - Exclude the start-cue blip window from gating math only. Don't remove audio samples.
  - *Accept:* silence, room noise and a quiet tap produce no typed text and a visible notice. A real "Thank you." at normal volume is typed.
- **W1.5 Long dictations.** If the transcript exceeds ~200 words, split at sentence boundaries into chunks of ≤ ~150 words. Run each through the same guards and join. A chunk that fails the guard uses its raw text only. Document that corrections spanning chunk boundaries may be missed. Also enforce `max_record_seconds` (see W2.1).
- **W1.6 Never lose a dictation.** Every job ends with a recorded status in history: `typed | copied | failed | dropped(reason)`. Injection failure → clipboard + `notice`.

### Phase 2 — Capture & hotkey

- **W2.1 `recorder.py`.**
  - Start the stream **first** on key-down. Do logging, blip and state emit afterwards or off-thread. Today the order is log → blip → `_emit_state` (tray icon redraw + registry write) → start recorder thread, so the first word gets clipped.
  - Allocate a fresh buffer object per recording in the hotkey thread and hand it to the recorder. This removes the race where `_audio_data = []` is reset on the recorder thread but read on the poller thread.
  - After key-up, keep capturing for `release_tail_ms` before closing (today the callback stops instantly, clipping trailing audio).
  - Play the start blip from the first audio callback, so it truthfully means "recording".
  - `_trim_silence`: replace the fixed 0.008 threshold + zero padding with an adaptive, noise-floor-aware trim (or VAD) and keep ~200 ms padding each side.
  - Enforce `max_record_seconds` with an auto-stop, a double-blip and a notice.
  - Honour `input_device` with fallback to the default; handle device errors with a visible notice.
  - Optional `keep_mic_open` ring buffer (off by default; document the mic-indicator trade-off).
- **W2.2 `hotkey.py`.**
  - Primary backend: `WH_KEYBOARD_LL` hook on a dedicated thread with a message loop, swallowing the configured key (only while listening and `swallow_hotkey`).
  - Callback rules: **do almost nothing**. Set flags or post to a queue, with no I/O or logging (Windows removes slow hooks). Ignore injected events (`LLKHF_INJECTED`) so our own `SendInput` never loops. Suppress key auto-repeat without retriggering. Keep the callback object referenced. Set `argtypes`/`restype` correctly for 64-bit. `UnhookWindowsHookEx` on shutdown.
  - Fallback backend: the current `GetAsyncKeyState` polling, used if the hook fails to install (log it, notice once).
  - Known limit: keys aren't seen or swallowed while an **elevated** window has focus. Document it.
  - Restrict `VK_MAP` to the allowed keys; an invalid saved hotkey falls back to `f9` with a notice.
  - *Accept:* with the hook active, F9 does nothing in the foreground app (manual check in Excel / VS Code / Outlook).
- **W2.3 Emergency exit & shutdown.**
  - Replace bare Scroll Lock `os._exit(0)` with **Ctrl+Shift+Scroll Lock** (or a 1 s hold).
  - Make an unknown hotkey no longer kill the emergency exit.
  - Tray "Exit" and the emergency path do a graceful shutdown (engine → hook → `icon.stop()` → `root.destroy()`), with a 2 s watchdog that then calls `os._exit`. This avoids ghost tray icons.
- **W2.4 Busy handling.** Replace the silent drop of key presses during processing with a single worker thread and a bounded FIFO queue (max 2). If full, play a distinct cue and show "Busy". Set `processing` before spawning work. Expose `queue_depth`.
- **W2.5 STT options.**
  - Remove `beam_size=1, best_of=1`. With `temperature=0`, `beam_size=1` selects `BeamSearchDecoder`, not greedy; omitting both gives `GreedyDecoder`.
  - Pass `initial_prompt` built from `custom_vocabulary` (Phase 8).
  - Record per-stage timings (`audio_ms`, `transcribe_ms`, `clean_ms`, `inject_ms`) via logging (numbers only).

### Phase 3 — Text injection

- **W3.1 `injector.py`** replaces pyautogui + pyperclip.
  - `type` path: `SendInput` with `KEYEVENTF_UNICODE`, batched. Send UTF-16 surrogate pairs as two units. This fixes silently dropped non-ASCII (é, ’, —, emoji; pyautogui's `_keyDown` returns silently for unmapped keys), the `FailSafeException` when the mouse is in a screen corner, and pyautogui's 0.1 s `PAUSE`.
  - `paste` path (multi-line/long text or `inject_mode=paste`):
    - retry `OpenClipboard` (≈10 × 20–30 ms)
    - snapshot existing clipboard contents (all copyable standard formats; if a format can't be copied, skip the restore for it and log a debug line)
    - set `CF_UNICODETEXT` together with the clipboard formats `CanIncludeInClipboardHistory=0`, `CanUploadToCloudClipboard=0` and `ExcludeClipboardContentFromMonitorProcessing`, so Win+V history / cloud sync don't record dictations (_verify_)
    - send Ctrl+V, wait ~150–300 ms, then restore **only if** `GetClipboardSequenceNumber()` still equals ours (never clobber something the user copied in the meantime)
  - Multi-line text uses the paste path. Enter keystrokes send messages in Slack/Teams/WhatsApp Web.
  - `auto` mode falls back to paste if `type` fails, and to clipboard-only + notice if both fail.
  - Remove the pyautogui/pyperclip imports and requirements only after the tests pass. (`resonant_ui._copy` may keep a simple clipboard helper from `platform_win`.)
- **W3.2 Foreground guard.** Snapshot `GetForegroundWindow()` (and process image name) at key-down. At injection time, if it differs (and isn't our own window), don't inject: put the text on the clipboard, mark history `copied`, and show a notice. Modifiers physically held at paste time can turn Ctrl+V into other shortcuts, so wait briefly for Shift/Alt/Win to be released, with a timeout. Detect UIPI-blocked (elevated) targets where feasible (token query fails) and notify (_verify_).
- **W3.3 Formatting policy.** `append_trailing_space` setting (a trailing space breaks URL bars, code and search fields). Deliberate newline handling per W3.1.

### Phase 4 — Startup, lifecycle, robustness

- **W4.1 Crash visibility.**
  - `resonant.py` imports `resonant_engine` at module level, **outside** the try/except that writes `app_error.log`. Under `pythonw`, a missing dependency means the app silently doesn't start. Move imports inside a guarded `main()`.
  - On fatal startup errors: write `app_error.log` (timestamped) **and** show a MessageBox. This includes `SystemExit("Could not take the instance lock.")`.
  - Install `sys.excepthook`, `threading.excepthook` and `root.report_callback_exception` → `logging`. Uncaught worker/Tk exceptions currently vanish under `pythonw`.
  - Stop swallowing exceptions silently in `_emit`, `load_settings` and `save_settings`; log them.
- **W4.2 `startup.py`.**
  - Currently `_ensure_startup` is subscribed to `"state"`, so it rewrites the Run key on every state emit (≥ 4× per dictation, on the hotkey thread). Do the sync **once** after `engine.start()` and on user toggle. Never re-add an entry the user removed.
  - `startup_command()` must append `--hidden`. The flag is documented but passed nowhere, so the dashboard pops up at every login.
  - Centralise the Run-key path, the `VoiceType` migration (run once, guarded by `legacy_startup_migrated`) and enable/disable/is_enabled here.
  - First-run consent: a non-blocking dashboard banner ("Start Resonant when you sign in? Yes / No"). If a Run entry already exists, keep it and mark the prompt done. Honour `--no-startup`.
- **W4.3 Single instance.**
  - Second launch signals the first via a named event (`Local\Resonant.Show`) so the running dashboard comes to front, then exits.
  - Only if the first doesn't release the lock within ~3 s, fall back to `taskkill`, and only after verifying identity: image name is `python(w).exe`, **and** the process creation time is not later than the PID file's mtime (otherwise the PID was recycled). Never kill unverified PIDs.
  - Respect `RESONANT_DEV=1`.
- **W4.4 Settings** (`settings.py`).
  - `save_settings` currently writes **all** defaults, so later edits to `config.py` defaults (which the README tells users to do) are silently ignored. Persist only overrides.
  - Atomic writes: temp file in the same directory + `os.replace`, with retry on `PermissionError`.
  - Validate types and ranges.
  - **Migration:** on first load of a legacy full-dump file, keep only values that differ from the *old* defaults, back it up as `settings.json.bak`, then apply the new schema and legacy key mappings.
  - Move magic numbers (0.008, 0.3 s, 200, 60 s, 30m, 25 ms poll) into named constants or settings.
- **W4.5 Model switching** (`_change_model` + `apply_settings` + `load_models`).
  - Persisting the new `stt_model` before the load succeeds can brick the next launch when it fails (e.g. offline `small.en`).
  - Load into a local variable under a lock. Commit `self.stt_model` and persist the setting **only on success**. On failure, revert the setting and dropdown and raise a notice.
  - Serialise overlapping loads with a generation counter (last *requested* wins, not last *finished*).
  - Show "Loading <model>…" in the UI while dictation continues on the old model.
  - A model-load failure must not leave the app permanently dead: offer retry and fall back to the last-good model.
- **W4.6 Logging & privacy** (`logging_setup.py`).
  - **Rotation bug:** the rotated file (200,000 chars + marker) is still over the 200,000-byte trigger, so once the log crosses it, *every* `log()` call re-reads and rewrites ~200 KB, on the hotkey thread. Use `RotatingFileHandler` (≈512 KB × 2 backups) behind a `QueueHandler`/`QueueListener`, so no hot thread does file I/O.
  - Don't write `Heard:` / `Final:` transcript text unless `log_transcripts` is on. Log lengths and timings instead.
  - Add "Clear history & logs" in the dashboard/tray.
  - Keep a `log(msg)` shim so existing imports work.
- **W4.7 Ollama lifecycle** (in `editor.py`).
  - Use a `requests.Session`. Lower the default timeout to `ollama_timeout_s` (10 s). On timeout, type the raw text, show a notice and start a background warm-up.
  - Health check (`GET /api/tags`) with backoff; expose `ollama_ok`. Retry warm-up when Ollama comes up later (at login it often isn't up yet).
  - If the model has been idle longer than ~80% of `keep_alive`, fire a background warm-up on hotkey **press**, so cold-load time overlaps with the user's speech.
  - Warm up after an `ollama_model` change.
  - Optional experiment: Ollama structured output (`format` JSON schema) to remove the quote-stripping hack. Keep plain output as default unless the bench shows a clear win.

### Phase 5 — UI, UX, accessibility

- **W5.1 History rows (click-to-copy is broken).** Bindings are on the row `Frame`, but two `Label`s cover ~97% of it and Tk delivers clicks to the Label, so the copy handler almost never fires. Hover recolours only the hidden Frame. Bind to the row and all children, recolour all, and make rows keyboard-operable (`takefocus`, Enter/Space). Show a status glyph per row (typed / copied / failed / dropped) and a way to copy the *raw* transcript as well.
- **W5.2 `live_lbl`** is created but never updated (the README promises "the text it heard"). Show the latest raw → final (or the latest notice), honouring `log_transcripts`-independent in-memory display, since history stays in memory only.
- **W5.3 Pill = the main feedback surface.** Currently the pill closes at key-up, so the slowest phase (2–6 s) has no visible feedback when the dashboard is hidden, and every failure goes only to the log. Make the pill persistent and reused (not created/destroyed each time) and show stages: *Listening → Transcribing… → Polishing… → Typed ✓ / Copied to clipboard / error text* with auto-dismiss. Bind the "release F9" text to the configured hotkey. Give the pill `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` so it can never steal focus or appear in Alt+Tab (_verify_ in Notepad). Clamp it to the monitor work area. Send errors also as tray toasts (`icon.notify`).
- **W5.4 Tray.**
  - Cache the PIL icons (paused / ready / recording / error) and assign only when the state changes. `_tray_state` currently redraws on every state event.
  - "Edit settings file" raises on a fresh install (file doesn't exist yet) and `.json` may open in a browser. Create the file if missing and open with Notepad.
  - Add "Copy last dictation" and "Clear history & logs".
- **W5.5 DPI & layout.**
  - `_dpi(root)` runs **after** `tk.Tk()`, but per Microsoft's guidance awareness must be set before the first window is created, so it is likely a no-op (symptom: blurry text ≥ 125%). Call it once, before `tk.Tk()`. Delete the duplicate.
  - Replace the fixed `430×660` + `wraplength=370` layout with sizes derived from `winfo_reqwidth/height`, and set `minsize`. Pack the footer **first** (`side="bottom"`) so it's never the element that gets clipped.
  - *Accept:* no clipped controls at 100/125/150/200% (manual checklist).
- **W5.6 UI performance & robustness.**
  - `_poll` re-arms `after` only if `_apply_state` doesn't raise; one exception freezes the dashboard forever. Re-arm in `try/finally`.
  - Skip dashboard widget redraws while the window is withdrawn (keep pill + tray logic), or drop to a slower poll (~500 ms) when hidden.
- **W5.7 Accessibility.**
  - Toggle state is effectively colour-only (~1.1:1 background, ~1.5:1 text between ON and OFF). Show it as text ("AI cleanup: Smart", "Sounds: On"), and add a visible focus ring.
  - Slow the recording pulse to ~1–2 Hz.
  - Add UI for `ai_cleanup_mode` and `inject_mode`.
  - Note in the README that Tk has limited screen-reader support and list the audio-cue and toast alternatives.
- **W5.8 Hold-only is exclusionary** for users with RSI or motor impairments. Toggle mode + silence auto-stop is in Phase 8.

### Phase 6 — Benchmark & tests

- **W6.1 Rewrite `bench_editor.py`** to test what ships.
  - Import `SYSTEM_PROMPT` and call `editor.clean()` so the guards are exercised. Currently `PROMPT_V3` is derived via `.replace` (silently a no-op if V1 changes) and differs from the shipped prompt, so the shipped prompt was never benchmarked.
  - Move cases to `bench_cases.json`. Scoring: normalised exact match, else whole-word regex checks with **all-of** semantics and **case-sensitive** proper nouns (currently `any()` + case-insensitive lets an all-lowercase "olive garden…" pass, and dropping "Priya"/"Saturday" also passes). Word-boundary matching (the substring `"um "` flags "premium"). Add a normalised edit distance and a guard-pass flag.
  - Run each case N=3 times. Report a per-case table, pass rate and latency per model/prompt. Write results to `bench_results/` (gitignored). Check model availability **once**, and remove the duplicate availability loop in `__main__`.
  - Add regression cases: "I can't…" / "I'm sorry…" sentences, a 300-word paragraph, a dictated question (must not be answered), already-clean text (idempotent), prompt-injection-style text ("ignore previous instructions…"), non-ASCII names, numbers/dates/emails/times ("three pm" → "3 PM"), stutters, list-with-correction, list-without-correction, and a plain comma-separated sentence that must **not** become bullets.
- **W6.2 Unit tests** (`tests/`, pytest, no hardware/torch):
  - `validate_rewrite`: marker in input vs output only, preamble, invention, number normalisation, growth
  - `should_use_llm` cue table; `chunk_transcript`
  - truncation (`done_reason="length"`) → raw fallback; Ollama timeout/connection error → raw fallback
  - silence/hallucination gate incl. the "legit Thank you." case
  - settings: validate, migrate legacy full-dump, overrides-only persist, atomic write, corrupt JSON
  - `startup_command()` contains `--hidden`; startup sync runs once; respects user removal
  - log rotation hysteresis (no rewrite per call)
  - recorder: fresh buffer per recording, release tail, max duration (fake stream)
  - injector: unicode batching incl. surrogate pairs; clipboard save/restore with sequence-number guard and retry (fake Win32 layer)
  - instance-lock/kill identity check (fake process table)
  - history status transitions; queue-full behaviour
  - `-m ollama` integration marker (skipped unless Ollama is reachable): tiny subset of the bench
- **W6.3 CI (only if a GitHub remote exists):** `.github/workflows/ci.yml` on `windows-latest`: install dev requirements, `ruff check`, `pytest -m "not ollama"`.

### Phase 7 — Docs

- **W7.1 README.**
  - Remove the FFmpeg step: STT only calls ffmpeg for file *paths*, and Resonant passes numpy arrays.
  - Remove F9 hard-coding. Document the hotkey and swallowing behaviour (F9 stops working in other apps *while Resonant is listening*), plus alternatives and the elevated-window limitation.
  - Correct the "change the hotkey → edit `config.py`" advice to match the new settings behaviour.
  - Add a **Privacy** section: what is persisted (settings, log timings), what isn't (audio, transcripts by default), the clipboard-history protections, the startup opt-in, and "don't share `resonant.log` unscrubbed" if `log_transcripts` was ever on.
  - Document `--hidden`, `--no-startup`, `--smoke`, `resonant_cli.py`, `ai_cleanup_mode`, `inject_mode`.
  - Fix the `run.bat` comment: it launches visibly; only the login entry is hidden.
  - Note the AV-heuristic caveat for anyone packaging an `.exe`.
- **W7.2** `CHANGELOG.md`; fix stale docstrings (e.g. the `apply_settings` docstring about needing a restart for `stt_model`); `docs/MANUAL_VERIFICATION.md`; `docs/ROADMAP.md` (§Backlog).

### Phase 8 — Optional features (do after Phases 0–7 are green; smallest effort first)

1. **Custom vocabulary & replacements.** `custom_vocabulary` → STT `initial_prompt` (truncate to ~200 tokens) and "preferred spellings" in the LLM prompt. `replacements` dictionary applied whole-word after cleanup. Tray: "Edit vocabulary".
2. **Toggle mode & silence auto-stop.** `hotkey_mode="toggle"`, `silence_autostop_ms` (energy-based end-of-speech). Also the biggest accessibility gain.
3. **Undo last dictation.** Track `{method, length, text, ts, hwnd}` for the last injection. Tray item (and optional chord): Ctrl+Z for paste, Backspace × length for typed text, only if the foreground window is unchanged and within ~30 s; otherwise say why not (_verify_ per app).
4. **Spoken punctuation & formatting** (deterministic pre-pass, before the LLM, off by default): "new line", "new paragraph", "comma", "period", "question mark", "open/close quote". Ambiguous words ("period") are why it's opt-in.
5. **Mic selector** + level test in the dashboard, with refresh and graceful fallback.
6. **History export** (opt-in, warns it writes plaintext) and clear.
7. **`faster-stt` backend** behind `Transcriber`, selectable via `asr_backend`, plus `bench_asr.py` for user-supplied WAV files. Do not change the default without measurements.

### Backlog — write to `docs/ROADMAP.md`, do NOT implement

Selection-rewrite "command mode" (select text, hold a second hotkey, say "make this formal / fix grammar / translate"); per-app profiles (build on the W3.2 foreground snapshot: no LLM in terminals/IDEs, per-app trailing-space and paste method); live partial-text preview; multilingual/auto-detect (`LANGUAGE=None`, multilingual model, per-language prompt); installer/first-run wizard/code signing; speaker diarization (not recommended for single-speaker dictation).

---

## 6. Verification

**Automated (you run these):** `ruff check`, `pytest -m "not ollama"`, `python resonant.py --smoke` with `RESONANT_DEV=1`, and `python resonant_cli.py clean "..."` / `doctor`. If Ollama is running locally, also run the bench and include the results table in the walkthrough.

**Manual (owner runs on Windows; generate this as `docs/MANUAL_VERIFICATION.md`, one line per item, each tagged with its W-ID):**

- Hold the hotkey in Excel, VS Code, Outlook and Word: no side effects, no key repeat (W2.2).
- Say a sentence starting with a hard consonant and ending with a plosive: neither the first nor the last word is clipped (W2.1).
- Dictate 200+ words: nothing is missing at the end (W1.1, W1.5).
- "I can't make it Tuesday, scratch that, Wednesday": the correction is applied (W1.2).
- Tap the hotkey silently, or with only background noise: nothing typed, and a notice appears (W1.4).
- Copy an image and some text, dictate, then paste: the clipboard is restored (W3.1).
- Win+V history and cloud clipboard don't contain the dictation (W3.1, _verify_).
- Put the mouse in a screen corner: text still types (W3.1).
- Type é, ’, — and an emoji: they arrive intact (W3.1).
- Dictate a bullet list into Slack/Teams: it doesn't send per line (W3.1).
- Alt-Tab during processing: the text is **not** pasted into the wrong window; it's on the clipboard with a notice (W3.2).
- Dictate into an elevated window: a clear notice, no silent loss (W3.2, _verify_).
- Pill: appears without stealing focus, stages visible, clamped at screen edges, correct hotkey text (W5.3).
- Sign out/in: starts hidden. Removing the entry (Task Manager/Autoruns) sticks. First-run prompt appears once (W4.2).
- Launch a second copy: the first comes to front, and nothing is killed (W4.3).
- Switch model while offline: a graceful failure, the old model keeps working, and a restart works (W4.5).
- Stop Ollama mid-session: raw text is typed with a notice. Restart it: recovery without an app restart (W4.7).
- Scroll Lock alone does nothing. Ctrl+Shift+Scroll Lock exits cleanly with no ghost tray icon (W2.3).
- 100/125/150/200% display scaling: no clipped controls, sharp text (W5.5).
- Keyboard-only pass: Tab reaches every control, history rows copy with Enter, focus ring visible (W5.1, W5.7).
- Quick Narrator/NVDA pass: note limitations in the README (W5.7).

---

## 7. Definition of done

- Phases 0–7 complete, or each deferred item is listed in the final walkthrough with a reason.
- `ruff check` clean, `pytest -m "not ollama"` green, `--smoke` exits 0, and no new unused imports.
- Public engine API and events unchanged (only additive changes).
- Every Phase-1 item has a failing-then-passing test.
- `README.md`, `CHANGELOG.md`, `docs/MANUAL_VERIFICATION.md` and `docs/ROADMAP.md` are updated.
- The final Walkthrough artifact includes: summary of changes per phase, bench before/after (or why unavailable), the list of *verify-on-Windows* items, and any decisions from §3 that you had to deviate from and why.

---

## Appendix A — Reference sketches (adapt, don't paste blindly)

```python
# editor.py — pure, unit-testable
def validate_rewrite(raw: str, out: str) -> tuple[bool, str]:
    if not out.strip():
        return False, "empty"
    low_in, low_out = raw.lower(), out.lower()
    if any(m in low_out and m not in low_in for m in REFUSAL_MARKERS):
        return False, "refusal"                    # only if the SPEAKER didn't say it
    if PREAMBLE_RE.match(low_out) and not PREAMBLE_RE.match(low_in):
        return False, "preamble"
    r, o = norm_tokens(raw), norm_tokens(out)      # numbers/ordinals/times normalised
    if len(o) >= 6:
        rset = set(r)
        if sum(w not in rset for w in o) / len(o) > INVENTED_MAX:
            return False, "invented"
        if len(o) > GROWTH_FACTOR * len(r) + GROWTH_SLACK:
            return False, "grew"
    return True, "ok"
```

```python
# transcriber.py — never drop on the phrase alone
def is_phantom(text, stats, segments) -> bool:
    if only_sound_tags(text):
        return True
    corroborated = (
        stats.peak < cfg.min_peak
        or max((s["no_speech_prob"] for s in segments), default=0) > 0.3
        or (stats.duration < 2.0 and stats.rms < cfg.min_rms)
    )
    return corroborated and normalise(text) in PHANTOM_PHRASES
```

```python
# hotkey.py — LL hook callback must be tiny
def _ll_proc(nCode, wParam, lParam):
    if nCode == HC_ACTION:
        kb = KBDLLHOOKSTRUCT.from_address(lParam)
        if not (kb.flags & LLKHF_INJECTED) and kb.vkCode == self._vk and self._swallow_active:
            self._q.put_nowait((wParam in (WM_KEYDOWN, WM_SYSKEYDOWN), kb.time))
            return 1                                # swallow; NO logging / I/O here
    return user32.CallNextHookEx(None, nCode, wParam, lParam)
```

## Appendix B — Traceability (review finding → work item)

| Review finding | Work items |
|---|---|
| A1 truncation | W1.1, W1.5 |
| A2 guard wrong both ways + prompt | W1.2, W1.3 |
| A3 hallucinations typed | W1.4 |
| A4 clipboard / non-ASCII / failsafe | W3.1 |
| A5 hotkey not swallowed | W2.2 |
| A6 first/last-word clipping, key-down ordering, buffer race | W2.1 |
| A7 silent no-start / invisible errors | W4.1 |
| A8 startup registration (per-event writes, `--hidden`, duplication) | W4.2 |
| A9 model switch can brick launch | W4.5 |
| A10 log rotation + plaintext transcripts | W4.6 |
| A11 history click + `live_lbl` | W5.1, W5.2 |
| B: busy drop, max length, wrong-window, silence trim, trailing space, Scroll Lock, taskkill, `_poll`, settings, settings-file open, DPI | W2.4, W2.1, W3.2, W2.1, W3.3, W2.3, W4.3, W5.6, W4.4, W5.4, W5.5 |
| B perf: skip LLM, greedy decode, Ollama timeout/warm-up, timings, UI/tray redraw, pyautogui overhead, faster-stt | W1.2, W2.5, W4.7, W2.5/W4.6, W5.4/W5.6, W3.1, Phase 8 |
| B quality: split engine, bench, dead code, swallowed exceptions, logging, requirements/tests, stale docs | §3, W6.1, W0.5, W4.1, W4.6, W0.1/W6.2, W7 |
| B security/privacy: startup consent, privacy docs, AV note, Ollama localhost-only | W4.2, W7.1, W7.1, §1 rule 6 |
| B UX/a11y: feedback, tray states, hold-only, contrast/focus, pill, portability | W5.3, W5.4, W5.8/Phase 8, W5.7, W5.3, W0.3 |
| C features | Phase 8 (small) and Backlog (large) |
| D quick wins | folded into the phases above (FAILSAFE/PAUSE → W3.1; `--hidden` → W4.2; DPI → W5.5; history bind → W5.1; `_poll` → W5.6; log hysteresis → W4.6; guard → W1.2; imports → W4.1; settings-file open → W5.4; FFmpeg/README → W7.1; greedy decode → W2.5; Scroll Lock → W2.3; bench → W6.1; `.gitignore`/requirements → W0.1) |
