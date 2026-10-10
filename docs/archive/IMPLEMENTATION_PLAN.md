# Resonant — Implementation Plan (from `docs/archive/RESONANT_TASK_BRIEF.md`)

Planning-mode artifact. Work items keep their brief IDs (W0.1 … W7.2).
Ground rules, invariants and STOP-AND-ASK triggers are taken verbatim from the brief.

## 0. Environment findings (plan inputs)

| Fact | Consequence |
|---|---|
| Python 3.14.7 (system) | Document supported range **3.11–3.14**; do not upgrade (W0.1). |
| No git repository / no remote | W6.3 (CI) is **not applicable** — recorded in the walkthrough as deferred-by-environment. |
| Owner's Resonant instance is RUNNING (PID 16516, per .vt.pid) | Never taskkill or launch the full app. All automated verification via `--smoke` (RESONANT_DEV=1) + pytest. Brief §1 rule 4 and §4 agree. |
| Ollama reachable (llama3.2 + qwen2.5:3b present) | W1.3/W6.1 bench re-run is possible later; *not* required to proceed. |
| Shell here is Git Bash, not PowerShell | §1 rule 3 still honoured by using `.venv\Scripts\python.exe`; I will invoke it as `./.venv/Scripts/python.exe` from bash — same interpreter, no global installs. |
| Runtime deps already installed system-wide (STT, torch, sounddevice, pystray…) | Tests must not import torch/STT anyway (§1 rule 5); dev venv gets only light deps. Rationale for venv: repeatable test env + pinned dev tooling, not runtime isolation. |

## 1. Decisions taken from §3 (no deviations)

All rows of the decisions table are adopted as written: f9 + swallow (LL hook),
`ai_cleanup_mode=smart`, `inject_mode=auto`, startup opt-in default off,
`log_transcripts=false`, keep default ASR backend, mic opens first on key-down.

Settings schema: adopt the full §3 table exactly (keys, defaults, legacy mappings
`ai_cleanup`→mode, `paste_mode`→inject_mode, `startup_prompted`,
`legacy_startup_migrated`).

## 2. Module plan (§3 split) — target file map

| New module | Draws from | Notes |
|---|---|---|
| `platform_win.py` | scattered ctypes in engine/UI/entry | user32/kernel32/winreg/msvcrt wrappers; ALL injectable (module-level `_impl` dict or constructor params); fakes live in tests/fakes. |
| `hotkey.py` | `hotkey_poller` (engine) | LL hook primary + polling fallback; Scroll Lock emergency chord moves here (W2.3). |
| `recorder.py` | `_record_loop`, `_trim_silence` | fresh buffer per recording, release tail, max duration, level. |
| `transcriber.py` | `_transcribe` + `_process` checks | silence/hallucination gate (W1.4), STT options (W2.5). |
| `editor.py` | `_clean` + guard logic | pure: `should_use_llm`, `validate_rewrite`, `chunk_transcript`, `clean`; Ollama client with Session (W4.7). |
| `injector.py` | `_type_out` | SendInput Unicode + guarded clipboard paste + foreground guard (W3.1–W3.3). |
| `startup.py` | `_ensure_startup` (resonant.py), tray toggle + `startup_checked` (resonant_ui), `migrate_old_startup_entry` | sync-once semantics (W4.2). |
| `settings.py` | `load_settings/save_settings` (config.py) | schema validation, overrides-only persistence, atomic writes, legacy migration (W4.4). `config.py` keeps re-exports. |
| `logging_setup.py` | `log()` (engine) | RotatingFileHandler + QueueHandler/Listener; `log()` shim stays (W4.6). |
| `resonant_cli.py` | new | `clean` / `transcribe` / `doctor` (W0.4). |

Invariants honoured: engine facade (`ResonantEngine`) keeps exact public API; new
events `notice` and state keys `stage/queue_depth/ollama_ok/notice` are additive.

## 3. Phase execution order & dependencies

```
P0 (venv, tests scaffold, platform_win, cli, lint cleanup)
  → P1 (correctness guards; depends on platform_win only lightly, editor is pure)
  → P2 (recorder + hotkey; depends on platform_win, settings constants)
  → P3 (injector; depends on platform_win)
  → P4 (startup/lifecycle/settings/logging/ollama)
  → P5 (UI/UX against new state keys)
  → P6 (bench rewrite + unit tests completed; CI n/a)
  → P7 (docs)
  → Phase 8 optional features (smallest first) — only if P0–P7 green
```
Each phase: ruff + pytest + `--smoke`, then a Walkthrough artifact appended to
`docs/archive/WALKTHROUGH.md` (one section per phase). Items I cannot verify without the
owner go to `docs/MANUAL_VERIFICATION.md` tagged with W-IDs (brief §6 list).

## 4. Task List (tagged; every brief item covered)

Phase 0
- [ ] W0.1 venv, requirements*.txt, pyproject (ruff+pytest cfg), .gitignore, Python-range doc
- [ ] W0.2 characterisation tests: `_trim_silence`, quote-strip, load_settings merge (tests/test_characterisation.py)
- [ ] W0.3 `platform_win.py` injectable Win32 layer
- [ ] W0.4 `resonant_cli.py` clean/transcribe/doctor
- [ ] W0.5 remove dead code: unused imports (threading in resonant.py, sys/time in engine/UI, CRASH_PATH in UI), `_dpi_aware`, `OLD_STARTUP_NAME`, `PROMPT_V2`

Phase 1
- [ ] W1.1 num_predict scaling + num_ctx + done_reason=="length" rejection (editor.clean)
- [ ] W1.2 editor.py guards: output-only refusal match, validate_rewrite (preamble/invention/growth/tags/quotes, named constants), should_use_llm cue tables, smart-mode deterministic tidy
- [ ] W1.3 SYSTEM_PROMPT fixes (Example 4 "Please", <transcript> wrapper, 3 new examples) + bench re-run report
- [ ] W1.4 transcriber.py silence/hallucination gate (peak/RMS, segment stats, sound tags, corroborated phantom phrases, blip-window exclusion)
- [ ] W1.5 chunk_transcript + max_record_seconds enforcement
- [ ] W1.6 history status ∈ {typed, copied, failed, dropped(reason)}; injection failure → clipboard + notice

Phase 2
- [ ] W2.1 recorder.py: stream-first ordering, fresh buffer, release_tail_ms, blip-from-first-callback, adaptive trim, max_record_seconds, input_device, optional keep_mic_open
- [ ] W2.2 hotkey.py: LL hook primary (tiny callback, LLKHF_INJECTED filter, auto-repeat, 64-bit argtypes, unhook), polling fallback, VK restriction f1–f24/pause/capslock
- [ ] W2.3 Ctrl+Shift+Scroll Lock chord (or 1s hold), graceful shutdown with 2s watchdog
- [ ] W2.4 worker queue (max 2), Busy cue, queue_depth state
- [ ] W2.5 drop beam_size/best_of, initial_prompt (vocab, P8), stage timings

Phase 3
- [ ] W3.1 injector.py SendInput UNICODE (surrogates, batched), guarded paste (retry, snapshot, exclude-from-history formats _verify_, seq-number restore), auto fallbacks; drop pyautogui/pyperclip
- [ ] W3.2 foreground guard (snapshot at key-down, mismatch → clipboard+copied+notice; modifier wait; UIPI detect _verify_)
- [ ] W3.3 append_trailing_space setting, newline policy

Phase 4
- [ ] W4.1 guarded imports in main(), crash MessageBox + app_error.log incl. lock SystemExit, sys/threading/tk excepthooks, no silent _emit/settings swallows
- [ ] W4.2 startup.py: sync once + on toggle, --hidden in command, VoiceType migration guarded by legacy_startup_migrated, consent banner, respect --no-startup
- [ ] W4.3 named-event focus signal, identity-verified taskkill (image name + creation-time vs pid-file mtime), RESONANT_DEV=1
- [ ] W4.4 settings.py: overrides-only persistence, atomic write + PermissionError retry, validation, legacy full-dump migration + .bak, magic numbers → constants
- [ ] W4.5 model switch: load-local-then-commit, generation counter, revert on failure, "Loading…" UI, retry/last-good
- [ ] W4.6 logging_setup.py: RotatingFileHandler (512KB×2) + QueueListener, log_transcripts gating, Clear history & logs, log() shim
- [ ] W4.7 editor Ollama: Session, ollama_timeout_s, /api/tags health + backoff + ollama_ok, warm-up at 80% keep_alive on press, warm-up on model change

Phase 5
- [ ] W5.1 history rows: bind row+children, keyboard operable, status glyph, copy raw
- [ ] W5.2 live_lbl updates (raw → final / notice), in-memory independent of log_transcripts
- [ ] W5.3 persistent pill with stages + auto-dismiss, hotkey-bound label, WS_EX_NOACTIVATE|TOOLWINDOW (_verify_), work-area clamp, tray toasts on errors
- [ ] W5.4 cached tray icons per state; settings-file open creates + Notepad; Copy last dictation; Clear history & logs
- [ ] W5.5 DPI before Tk, derived sizes + minsize, footer packed first
- [ ] W5.6 _poll try/finally re-arm; hidden-window slow poll
- [ ] W5.7 text-state toggles + focus ring, 1–2Hz pulse, ai_cleanup_mode/inject_mode UI, README a11y note
- [ ] W5.8 (defer note) toggle mode arrives with Phase 8 item 2

Phase 6
- [ ] W6.1 bench_editor rewrite: imports SYSTEM_PROMPT, calls editor.clean, bench_cases.json, all-of regex scoring + case-sensitive proper nouns + word boundaries, N=3, bench_results/ gitignored, single availability check, new regression cases
- [ ] W6.2 full pytest suite per brief list (editor, transcriber, settings, startup, logging, recorder, injector, lock identity, history/queue; -m ollama optional integration)
- [ ] W6.3 CI — N/A (no git remote); recorded in walkthrough

Phase 7
- [ ] W7.1 README: no FFmpeg, hotkey/swallow docs + elevated-window limit, settings-corrected advice, Privacy section, flags + cli docs, run.bat comment fix, AV note
- [ ] W7.2 CHANGELOG.md, docstring fixes (apply_settings), docs/MANUAL_VERIFICATION.md, docs/ROADMAP.md (backlog verbatim)

Phase 8 (post-green, smallest first): vocabulary/replacements → toggle+autostop → undo → spoken punctuation → mic selector → export → faster ASR behind Transcriber (no default change).

## 5. Verification strategy per phase

Automated: `ruff check`, `pytest -m "not ollama"` (venv), `RESONANT_DEV=1 python resonant.py --smoke`, `resonant_cli.py clean/doctor`. Bench only if Ollama reachable (it is; optional, not blocking).
Manual: generate `docs/MANUAL_VERIFICATION.md` from §6 verbatim, one line per item, W-ID tagged, with a Status column for the owner.

## 6. STOP-AND-ASK pre-check (§4)

None triggered at plan time: no heavy ML installs needed, no elevation, no
full-app launches, no storage changes beyond documented settings schema, hotkey
stays f9, Ollama stays localhost. If the engine split ever requires touching what
is stored about speech → STOP.
