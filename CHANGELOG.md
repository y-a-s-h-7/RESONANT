# Changelog

## [1.3.0] — reachability & honest failures

### Added

* **Toggle mode** (`hotkey_mode: "toggle"`): tap the hotkey to start and tap
  again to stop, instead of holding it — switchable from the dashboard button or
  the tray menu. In toggle mode a key *release* is no longer a stop signal, and
  every hint, pill and tooltip says "tap" rather than "release".
* **Hotkey picker in the dashboard** (F1–F12, Pause, Caps Lock, in numeric
  order). Changing the key used to require hand-editing `settings.json`.
* **Microphone picker in the dashboard** — every input device is listed,
  including a selected device that is no longer connected, so a silent mic is
  visible instead of mysterious.

### Fixed

* **A vanished microphone no longer ends dictation.** If the configured device
  fails to open (unplugged headset, shifted USB index) the recorder retries once
  on the system default, announces the fallback, and reports
  `Microphone unavailable (…)` rather than the misleading "released too fast?".
* **Stale notices replayed on reopening the dashboard.** Notice freshness is now
  driven by a monotonic `notice_seq` instead of comparing text, so a message
  that arrived while the window was hidden is shown once — not again minutes
  later. Notices also reach the pill while the dashboard is hidden, which they
  previously never did (the timing clock only advanced on the visible path).
* **A Windows balloon for startup chatter.** "AI model ready" still logs but no
  longer raises a notification on every launch; real failures still do.
* **Recording hints in toggle mode** said "release to finish" — impossible
  advice when no key is held.
* **Settings problems at startup were invisible in the log** (they were emitted
  as "quiet" notices) — they are now logged as well as shown.
* **`replacements` only matched exact case** and read `\1`/`C:\path` targets as
  regex backreferences. Matching is now whole-word and case-insensitive, and the
  target is inserted literally.

### Changed

* Tests log to a per-test temp file — the suite no longer appends noise to the
  `resonant.log` that the app is diagnosed from.
* 155 tests (was 139), including a real Tk dashboard suite that renders every
  engine state and exercises the new pickers without showing a window.
* `pyproject.toml` version aligned with this changelog (was stuck at 1.1.0).

## [1.2.0] — hardening & reliability pass

Executed from `RESONANT_TASK_BRIEF.md` (Phases 0–7). See `docs/WALKTHROUGH.md`
for the per-phase detail and `docs/MANUAL_VERIFICATION.md` for the checklist.

### Fixed — data loss and wrong output

* **Truncation:** the cleanup request no longer uses a fixed `num_predict=200`;
  it scales with the input, sets `num_ctx` explicitly and rejects a rewrite that
  hit the token limit, keeping the raw transcript instead.
* **False refusals:** refusal markers are matched only when the *speaker* did not
  say them, so "I can't make it Tuesday" survives.
* **Missed bad output:** new guards reject empty output, chatbot preambles,
  answered questions, invented content and runaway growth.
* **Hallucinations:** peak/RMS gate before transcription, STT segment
  filtering, sound-tag handling, and phantom phrases dropped only with
  corroborating evidence — a real "Thank you." is still typed.
* **First/last word clipping:** the mic stream now opens before any blip,
  logging or UI update, and recording continues for `release_tail_ms` after
  key-up.
* **Buffer race:** each recording gets a private buffer handed to the recorder,
  instead of shared mutable state read across threads.
* **Double typing:** the single-instance lock handle is module-global again, so
  it cannot be garbage-collected and release the lock (which let two pollers
  run).
* **Wrong-window pastes:** the foreground window is snapshotted at key-down; if
  focus moved, the text goes to the clipboard with a notice instead.
* **Non-ASCII silently dropped:** typing now uses Win32 `SendInput` with
  `KEYEVENTF_UNICODE` (surrogate pairs included) rather than pyautogui.
* **Clipboard clobbering:** paste retries `OpenClipboard`, snapshots the
  previous text and restores it only if the clipboard is still ours.
* **Model switches that bricked the next launch:** the setting is committed only
  after a successful load, superseded loads are discarded, and the current model
  keeps working on failure.
* **UI freeze:** the dashboard poll re-arms in `finally`; a hidden window uses a
  slow poll and skips redraws.
* **Unusable history rows:** click/hover bindings now cover the row and its
  labels; rows are keyboard-operable and show a status glyph.
* **Silent non-start under `pythonw`:** imports moved inside `main()`, plus
  `app_error.log`, a MessageBox, and `sys`/`threading` excepthooks.
* **Log rotation storm:** replaced with a queued `RotatingFileHandler`
  (512 KB × 2); transcripts are no longer written unless opted in.
* **Registry writes on every state emit:** startup sync happens once, appends
  `--hidden`, and never re-adds a removed entry.
* **DPI awareness set too late:** now applied before the first Tk window.

### Added

* `hotkey.py`, `recorder.py`, `transcriber.py`, `editor.py`, `injector.py`,
  `settings.py`, `startup.py`, `logging_setup.py`, `platform_win.py`,
  `resonant_cli.py` — the engine is split into cohesive, testable modules
  (public `ResonantEngine` API and events unchanged; `notice`, `stage`,
  `queue_depth`, `ollama_ok` and `notice` are additive).
* `--smoke`, `--no-startup` and `RESONANT_DEV=1` safety switches; `--hidden` is
  now actually passed by the login entry.
* Emergency exit is `Ctrl+Shift+Scroll Lock` (Scroll Lock alone did nothing
  before, and an unknown hotkey used to disable it).
* Bounded dictation queue: a press during processing is queued (max 2) or
  refused with a distinct cue and a notice — never silently dropped.
* `resonant_cli.py clean / transcribe / doctor`.
* `bench_cases.json` + a rewritten `bench_editor.py` that benchmarks the shipped
  prompt and guards.
* `docs/IMPLEMENTATION_PLAN.md`, `docs/WALKTHROUGH.md`,
  `docs/MANUAL_VERIFICATION.md`, `docs/ROADMAP.md`, `CHANGELOG.md`.
* 139 unit tests, all runnable without a microphone, torch, STT, Ollama or
  Windows.

### Changed

* `settings.json` now stores **only overrides**; defaults live in `config.py`
  and legacy full-dump files are migrated (with a `.bak`).
* `ai_cleanup_mode` (`smart`/`always`/`off`) replaces the `ai_cleanup` boolean;
  `inject_mode` (`auto`/`type`/`paste`) replaces `paste_mode` — the old keys are
  mapped on first load.
* Hotkey choices are restricted to `f1`–`f24`, `pause`, `capslock`.
* The pill is persistent, shows every stage, cannot steal focus and is clamped
  to the work area.
* STT engine runs with the greedy decoder (removed `beam_size=1`/`best_of=1`, which
  actually selected beam search), and per-stage timings are logged.

### Removed

* `pyautogui` and `pyperclip` (typing/clipboard now use Win32 directly).
* Dead code: the duplicate DPI helper, unused UI constants, the old bench
  prompt copy, and unused imports.

### Known limitations

* Keys are neither seen nor swallowed while an **elevated** window has focus.
* Corrections spanning a chunk boundary may be missed in >200-word dictations.
* `qwen2.5:3b` exceeded the 10 s timeout on the development machine, so
  `llama3.2` remains the default `ollama_model`.
* Clipboard restore covers text only, not other formats.
