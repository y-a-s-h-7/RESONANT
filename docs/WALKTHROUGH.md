# Resonant — walkthrough (Phases 0–7)

Artifact required by the brief (§1 rule 2). One section per phase: what changed,
files touched, tests added, results, and anything that could only be checked on
Windows (collected in `docs/MANUAL_VERIFICATION.md`).

Gate status at the end of this pass:

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `pytest -m "not ollama"` | **139 passed** |
| `python resonant.py --smoke` (`RESONANT_DEV=1`) | exit 0, `Resonant --smoke OK` |
| `python resonant_cli.py doctor` | full report, no notices |
| Full app launch | **not performed automatically** (brief §1 rule 4) |

---

## Phase 0 — Baseline & scaffolding (W0.1–W0.5)

* `.venv` (Python 3.14.7), `requirements.txt` (runtime) and `requirements-dev.txt`
  (pytest/ruff + the *light* runtime imports; never heavy ML dependencies), `pyproject.toml`
  (ruff + pytest config, `requires-python = ">=3.11,<3.15"`), `.gitignore`
  covering `.vt.*`, `*.log`, `settings.json*`, `.venv`, `*.wav`, `bench_results/`.
* `platform_win.py` — injectable Win32 layer (keyboard hook, `SendInput`,
  clipboard read/write, foreground window + process identity, registry, named
  events, MessageBox). Everything else imports Win32 through it.
* `resonant_cli.py` — `clean`, `transcribe`, `doctor`; the app also gained
  `--smoke`, `--no-startup` and `RESONANT_DEV=1` so no automated step can touch
  the owner's running instance, registry or hardware.
* Dead code removed (`threading`/`sys`/`time` imports, `_dpi`, unused startup
  constants, old bench prompt copy).

Tests added: `tests/conftest.py` (stubs `sounddevice`/`stt` so tests run
without heavy ML or audio hardware).

## Phase 1 — Silent data loss & wrong output (W1.1–W1.6)

* `editor.py` (new): `num_predict` scales with input (`max(256, 2.5×words+64)`),
  explicit `num_ctx=4096`, `done_reason == "length"` → rewrite rejected and the
  raw text stands (W1.1).
* `validate_rewrite` rejects empty output, chatbot preambles and answered
  questions; refusal markers now count **only when the speaker did not say
  them** (the "I can't make it Tuesday" false positive); invented-content and
  growth limits with numbers/ordinals/times normalised both ways; stray
  `<transcript>` tags and one enclosing quote layer stripped (W1.2).
* `should_use_llm` (smart mode) skips the model unless there are correction,
  list or filler cues, a stutter, or `smart_min_words` words.
* Prompt fixes (W1.3): "Please" removed from example 4, the user turn is wrapped
  in `<transcript>` tags, plus a "keep I can't" example, a
  "clean-don't-answer-the-question" example, an idempotence example, later a
  comma-list example and a "you know" filler example, and rules 8–10.
* `transcriber.py` (new): peak/RMS gate before transcribing, segment filter
  (`no_speech_prob` + `avg_logprob`, `compression_ratio`), sound-tag handling,
  and phantom phrases ("Thank you.") dropped **only** with corroborating
  evidence (W1.4).
* `chunk_transcript` splits >200-word transcripts at sentence boundaries into
  ≤150-word chunks; each chunk keeps its own raw text on guard rejection (W1.5).
* Every job now ends with a history status: `typed | copied | failed |
  dropped(reason)` (W1.6).

Tests added: `tests/test_editor.py`, `tests/test_transcriber.py`.

## Phase 2 — Capture & hotkey (W2.1–W2.5)

* `recorder.py` (new): the hotkey thread allocates a **fresh**
  `RecordingBuffer` per recording and `Recorder.start()` opens the mic stream
  **first** — logging, blip and state emit happen only after the stream exists,
  so the first word is not clipped; a `release_tail_ms` tail keeps capturing
  after key-up; `max_record_seconds` auto-stops with a double blip; adaptive
  noise-floor trim (10th percentile, not median) with ~200 ms padding.
* `hotkey.py` (new): `WH_KEYBOARD_LL` hook installed **and pumped** on its own
  thread, auto-repeat suppressed, injected events ignored, release swallowed
  only if the press was, callback kept referenced, `UnhookWindowsHookEx` on
  shutdown; `GetAsyncKeyState` polling fallback with a one-time notice; the key
  map is restricted to `f1`–`f24`, `pause`, `capslock`; `Ctrl+Shift+Scroll Lock`
  is the emergency chord (Scroll Lock alone does nothing) (W2.2, W2.3).
* Busy handling: one worker thread with a bounded FIFO (max 2). Full queue →
  distinct cue + notice + a `dropped(queue-full)` history row; `processing` is
  set before work starts; `queue_depth` is exposed (W2.4).
* STT options: `beam_size`/`best_of` removed so `temperature=0` uses the
  greedy decoder, `initial_prompt` from `custom_vocabulary`, and per-stage
  timings (`audio_ms`, `transcribe_ms`, `clean_ms`, `inject_ms`) logged as
  numbers only (W2.5).

Tests added: `tests/test_recorder.py`, `tests/test_hotkey.py`, plus queue,
status and model-switch cases in `tests/test_engine.py`.

## Phase 3 — Text injection (W3.1–W3.3)

* `injector.py` (new) replaces pyautogui + pyperclip:
  * `type`: batched `SendInput` with `KEYEVENTF_UNICODE`, UTF-16 surrogate pairs
    handled — non-ASCII survives, no screen-corner `FailSafeException`, no
    0.1 s per-character pause.
  * `paste`: retry `OpenClipboard`, snapshot the previous clipboard text, mark
    our content with `CanIncludeInClipboardHistory=0`,
    `CanUploadToCloudClipboard=0` and
    `ExcludeClipboardContentFromMonitorProcessing`, send Ctrl+V, then restore
    **only if** `GetClipboardSequenceNumber()` is still ours.
  * `auto`: typing for short single-line text, paste for multi-line/long; paste
    failure falls back to typing and then to clipboard-only + notice.
* Foreground guard: the window (and process image name) is snapshotted at
  key-down; if focus moved, the text goes to the clipboard, the history status
  is `copied`, and the notice says why. Modifiers are awaited before Ctrl+V
  (W3.2).
* `append_trailing_space` setting (default on) and deliberate newline handling —
  multi-line text always uses the paste path so Enter keystrokes never send a
  message in Slack/Teams/WhatsApp Web (W3.3).
* `pyautogui` and `pyperclip` are gone from the code and from
  `requirements.txt`.

Tests added: `tests/test_injector.py` (fake Win32 layer: unicode batching,
surrogate pairs, restore guard, clipboard busy, mode fallbacks, foreground
guard, modifier wait).

## Phase 4 — Startup, lifecycle, robustness (W4.1–W4.7)

* `resonant.py`: heavy imports moved inside `main()`, `app_error.log` **and** a
  MessageBox on fatal start-up errors (including a failed lock),
  `sys.excepthook` / `threading.excepthook` installed, listener exceptions
  logged instead of swallowed (W4.1).
* `startup.py` (new): Run-key logic in one place, `startup_command()` appends
  `--hidden`, sync happens once after `engine.start()` and on user toggle,
  a removed entry is never re-added, existing entries are adopted as the user's
  choice, and the legacy `VoiceType` entry is migrated once (W4.2).
* Single instance: a second launch signals `Local\Resonant.Show` so the running
  dashboard comes to front and exits; only if the lock is still held after ~3 s
  does it `taskkill`, and only after verifying the PID is a `python(w).exe`
  whose creation time is not newer than the PID file (recycled-PID guard).
  `RESONANT_DEV=1` / `--no-startup` disable both paths (W4.3).
* `settings.py` (new): defaults stay in `config.py`; the file stores **only
  overrides** (atomic temp-file + `os.replace`, retry on `PermissionError`);
  types/ranges/enums validated with a notice per bad value; the legacy
  full-dump file is backed up to `settings.json.bak` and migrated
  (`ai_cleanup`→`ai_cleanup_mode`, `paste_mode`→`inject_mode`, old-default
  values dropped) (W4.4).
* Model switching: load into a local variable under the lock, commit the setting
  **only on success**, revert the dropdown on failure, discard superseded loads
  via a generation counter, show "Loading <model>…" while the old model keeps
  dictating, and never brick the next launch (W4.5).
* `logging_setup.py` (new): `QueueHandler` + `QueueListener` over a
  `RotatingFileHandler` (512 KB × 2) — the old hand-rolled rotation rewrote
  ~200 KB on every `log()` call once it had rotated, on the hotkey thread.
  Transcripts are logged only with `log_transcripts=true`; otherwise lengths.
  `log(msg)` shim kept; "Clear history & logs" wired in the dashboard and tray
  (W4.6).
* Ollama lifecycle in `editor.py`/engine: `requests.Session`, 10 s default
  timeout with raw-text fallback + notice, `GET /api/tags` health check with
  backoff exposing `ollama_ok`, warm-up retry when Ollama appears later, and a
  background warm-up on hotkey press once 80 % of `keep_alive` has elapsed
  (W4.7).

Tests added: `tests/test_startup_and_logging.py`, model-switch and settings
cases in `tests/test_engine.py`.

## Phase 5 — UI, UX, accessibility (W5.1–W5.7)

* History rows: the click/hover bindings are now on the row **and every child
  label** (previously the labels covered ~97 % of the row and swallowed the
  click), rows are keyboard-operable (`takefocus`, Enter/Space), and each row
  shows a status glyph (`✓ typed`, `⧉ copied`, `⚠ failed`, `– dropped`);
  right-click copies the raw transcript (W5.1).
* The live line is actually updated: last notice, or `heard → final` (W5.2).
* The pill is created once and reused, shows *Listening… → Transcribing… →
  Polishing… → result/notice* (auto-dismiss), names the configured hotkey,
  clamps to the monitor work area, and gets
  `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` so it cannot take focus or join
  Alt+Tab; notices also become tray toasts (W5.3).
* Tray: cached PIL icons assigned only on change, `Edit settings file` creates
  the file and opens Notepad (it used to raise when the file was missing and
  could hand `.json` to a browser), plus `Copy last dictation` and
  `Clear history & logs` (W5.4).
* DPI awareness is set in `resonant.py` **before** `tk.Tk()` (the old call ran
  after the first window and was a likely no-op); the window sizes itself from
  `winfo_reqwidth/height`, has a `minsize`, and the footer is packed first so it
  can never be the clipped element (W5.5).
* `_poll` re-arms in `finally` (one exception used to freeze the dashboard
  forever) and drops to a 500 ms cadence, skipping dashboard redraws, while the
  window is hidden (W5.6).
* Accessibility: toggle state is text ("AI cleanup: Smart", "Sounds: On",
  "Typing: Auto", "Trailing space: On"), a visible focus ring was added, the
  recording pulse slowed to ~1 Hz, the dashboard exposes `ai_cleanup_mode` and
  `inject_mode`, and the README documents the Tk screen-reader limitation with
  the audio/toast alternatives (W5.7).

Tests added: none (Tk in CI-less environment) — all Phase 5 items are on
`docs/MANUAL_VERIFICATION.md`.

## Phase 6 — Benchmark & tests (W6.1–W6.3)

* `bench_editor.py` rewritten: it imports the shipped `SYSTEM_PROMPT` and calls
  `editor.clean()`, so the guards are exercised; cases moved to
  `bench_cases.json` (correction, list-with/without correction, comma sentence,
  "I can't", "I'm sorry", dictated question, idempotence, fillers, stutter,
  numbers, non-ASCII, prompt injection, pronoun preservation, cancel item, plus
  a synthetic 300-word paragraph); scoring uses whole-word matching, all-of
  semantics, case-sensitive proper nouns, normalised comparison and an edit
  distance, with a guard-pass flag; N runs per case; results in
  `bench_results/`; model availability checked once.
* Test suite: 139 tests, all hardware-free (`tests/`), covering guards,
  chunking, truncation and Ollama failure fallbacks, the silence/hallucination
  gate including the legitimate "Thank you.", settings validate/migrate/atomic
  behaviour, startup command + sync semantics, log rotation gating, recorder
  buffers/tail/max duration, injection and clipboard guards, the instance-lock
  identity check, history statuses and queue-full behaviour.
* **W6.3 (CI) is not applicable:** this folder is not a git repository and has no
  remote, so no `.github/workflows/ci.yml` was added.

### Benchmark results (W1.3, W6.1)

Measured with the new harness while Ollama was up (`--runs 2`, 16 cases):

| Model | Cases fully passed | Median latency | Notes |
|---|---|---|---|
| `llama3.2` | **13/16** | ≈2.4 s | all guard behaviour correct: the prompt-injection case was rejected by the invention guard and fell back to raw |
| `qwen2.5:3b` | 9/16 | ≈12.1 s | **unusable on this machine**: every LLM call exceeded the 10 s timeout, so every case fell back to the raw transcript. 3 of the "passes" are therefore cases that expect raw output. |

Honest caveats:

* "Before" is undefined: the old bench built its own prompt copy with
  `.replace` and scored with `any()` + case-insensitive checks, so the shipped
  prompt had **never** actually been benchmarked. The numbers above are the
  first real measurement.
* After that measurement the prompt gained rule 10 and examples 8–9 (comma list
  preserved, trailing "you like/you know" filler removed). Ollama then went
  down mid-session (`Connection refused`), and the re-run produced only
  `raw:llm-unavailable` for every case — the re-measure is on
  `docs/MANUAL_VERIFICATION.md`. The plumbing is proven: the timeout path
  degraded to the raw transcript with a notice exactly as designed.
* Recommendation unchanged: keep `llama3.2` as the default `ollama_model`.

## Phase 7 — Docs (W7.1, W7.2)

* `README.md`: FFmpeg step removed (arrays, not file paths), hotkey +
  swallowing behaviour and the elevated-window limitation documented, settings
  section rewritten around "defaults in `config.py`, overrides only in
  `settings.json`", new **Privacy** section (what is stored, what is not, the
  clipboard-history flags, the startup opt-in, unscrubbed logs), the
  `--hidden` / `--no-startup` / `--smoke` / `resonant_cli.py` /
  `ai_cleanup_mode` / `inject_mode` documented, the `run.bat` comment corrected
  (it launches visibly; only the login entry hides), plus an antivirus-heuristic
  note for future packaging.
* `CHANGELOG.md`, `docs/MANUAL_VERIFICATION.md`, `docs/ROADMAP.md` (backlog),
  stale docstrings fixed (`apply_settings` no longer claims `stt_model`
  needs a restart), and the engine/UI docstrings now describe the real
  behaviour.

---

## Deviations from §3, and why

| Decision | What was done | Why |
|---|---|---|
| Clipboard snapshot: "all copyable standard formats" | Only `CF_UNICODETEXT` is snapshotted/restored; a non-text clipboard is left alone rather than partially restored | Restoring every format needs a full `IStream`/`OleGetClipboard` round-trip; the risk of half-restoring an image was judged worse than leaving it. Flagged on the manual checklist. |
| `keep_mic_open` ring buffer | Not implemented (setting remains, documented as off/unused) | An always-open stream keeps Windows' microphone-in-use indicator lit, which reads as a privacy problem. |
| Startup default `false` vs the existing entry | Registry is treated as the source of truth: an existing entry is adopted (`run_at_startup=true`, `startup_prompted=true`) rather than removed | The owner already had the app starting at login; silently deleting it would be worse than keeping it. |
| `settings.py` vs `config.py` | Split as specified, with `config.load_settings`/`save_settings` kept as lazy re-exports | Keeps the flat file layout and backwards compatibility (brief §3, §1 rule 8). |
| Phase 8 items | `custom_vocabulary` (STT `initial_prompt`) and whole-word `replacements` were implemented; the rest stay in `docs/ROADMAP.md` | They are ~15 lines total and were already in the settings schema; the brief says to do Phase 8 only after 0–7 are green, which they now are. |
| `pyperclip` | Removed entirely (UI copies through `platform_win`) | One less dependency; the brief allowed the UI to use the platform layer. |

## What still needs a human (Windows)

See `docs/MANUAL_VERIFICATION.md` — 34 items, each tagged with its work-item ID.
The five that matter most:

1. Real hotkey behaviour in Excel/VS Code/Outlook/Word (hook swallowing, no
   auto-repeat) (W2.2).
2. First/last word not clipped, 200+ words intact (W2.1, W1.1, W1.5).
3. Clipboard restore, Win+V/cloud clipboard _verify_, non-ASCII characters,
   corner-mouse typing (W3.1).
4. Pill focus/Alt-Tab behaviour and clamping; DPI at 125/150/200 % (W5.3, W5.5).
5. Sign-out/sign-in hidden start, and that a removed startup entry stays
   removed (W4.2).
