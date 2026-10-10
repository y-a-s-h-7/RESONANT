# Contributing to Resonant

Thanks for helping out! This project is a Windows-only, offline voice
dictation app, so a few conventions keep everything verifiable without
hardware, accounts or network access.

## Ground rules

- **No cloud, no telemetry.** Nothing may call out beyond `localhost` (Ollama)
  and Whisper's own model download.
- **Tests never touch real hardware.** Keyboard hook, clipboard, microphone and
  registry go through `platform_win.py` / injected fakes so the suite runs on
  any OS (`pytest -m "not ollama"`).
- **Don't commit the heavy stack.** `requirements-dev.txt` intentionally omits
  `torch` / `openai-whisper`; runtime dependencies belong only in
  `requirements.txt`.
- **No committed personal data.** `settings.json`, `resonant.log`, audio files
  and `verification_results.json` are gitignored — keep it that way.
- **Don't rewrite git history** (no force-push, no rebases of shared commits).

## Before you open a PR

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -m "not ollama"
.venv\Scripts\python.exe resonant.py --smoke
```

CI runs the first two on `windows-latest` for Python 3.11 and 3.13 — see
[.github/workflows/ci.yml](.github/workflows/ci.yml).

- Update `CHANGELOG.md` for user-visible changes.
- If you touch a claim the README makes (hotkey swallowing, Win+V exclusion,
  injection), run `python resonant_cli.py verify` and note the result.

## Repository setup checklist (do these in the GitHub UI)

- [ ] **Description**: *Zero-cloud, hold-to-talk voice dictation for Windows —
      local Whisper + Ollama, no accounts, no telemetry.*
- [ ] **Website**: leave blank (or point at a demo GIF/release page).
- [ ] **Topics**: `voice-dictation`, `speech-to-text`, `whisper`, `ollama`,
      `offline`, `windows`, `python`, `tkinter`, `win32`, `push-to-talk`
- [ ] **README social preview**: set to the demo GIF (or a screenshot).
- [ ] **Default branch**: `master` (CI badge above assumes it).
- [ ] **About → Releases**: tag a release once the manual verification
      checklist in [docs/MANUAL_VERIFICATION.md](docs/MANUAL_VERIFICATION.md)
      has a passing run recorded.

## Pull request checklist

- [ ] `ruff check .` passes.
- [ ] `pytest -m "not ollama"` passes.
- [ ] New behaviour has tests; existing tests were updated, not weakened.
- [ ] `CHANGELOG.md` updated where the change is user-visible.
- [ ] No secrets, personal settings, logs or audio in the diff.
