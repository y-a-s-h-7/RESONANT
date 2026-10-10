# Resonant — manual verification checklist

Everything here needs a human on Windows: real hardware input, real focus, real
scaling. Automated coverage is in `tests/` (`pytest -m "not ollama"`), plus
`python resonant.py --smoke` and `python resonant_cli.py doctor`.

Tick marks: `[ ]` = not yet verified, `[x]` = verified by the owner.

## Capture & hotkey

- [ ] Hold the hotkey in **Excel, VS Code, Outlook and Word**: no side effects,
      no key repeat while held, and the key does not reach the app while
      listening (W2.2).
- [ ] Say a sentence starting with a hard consonant and ending with a plosive
      ("**B**ring the **c**heque **b**ack"). Neither the first nor the last word
      is clipped (W2.1).
- [ ] Dictate 200+ words: nothing missing at the end (W1.1, W1.5).
- [ ] Tap the hotkey silently, or with only background noise: nothing is typed
      and a notice appears (pill + tray toast) (W1.4).
- [ ] Dictate a legitimate "Thank you." at normal volume: it **is** typed
      (W1.4).
- [ ] Reach `max_record_seconds` (temporarily set it to 10): auto-stop with a
      double blip and a notice (W2.1).
- [ ] Press Scroll Lock alone: nothing happens. Press Ctrl+Shift+Scroll Lock:
      clean exit, no ghost tray icon (W2.3).
- [ ] Dashboard → **Mode: Toggle**: one tap starts recording and it keeps going
      with the key released; a second tap stops and types. The pill says "tap
      again to stop", and no "release" hint appears anywhere (W2.2b).
- [ ] Dashboard → **Hotkey**: pick F10. F10 dictates, F9 goes back to its normal
      job, and the pill/tooltip use the new key name immediately (W2.2b).
- [ ] Dashboard → **Microphone**: pick a non-default device, dictate. Then
      unplug that device (or pick a device that no longer exists) and dictate
      again: it records from the system default and says so, instead of typing
      nothing (W2.1b).
- [ ] Hotkey from an **elevated** window: confirm the documented limitation
      (nothing recorded) (W2.2, _verify_).

## Correction quality

- [ ] "I can't make it Tuesday, scratch that, Wednesday" → the correction is
      applied and "I can't" survives (W1.2).
- [ ] "Tell him I'm sorry for the delay" → not replaced by a refusal (W1.2).
- [ ] A dictated question ("what time is the meeting tomorrow") is cleaned but
      **not answered** (W1.3).
- [ ] A dictated list with a correction ("…second item is Banana… cancel the
      bananas, replace them with oranges") loses the cancelled item (W1.2).
- [ ] A comma-separated sentence stays a sentence (no bullets) (W1.3).
- [ ] Switch the dashboard to `AI cleanup: Off`: text is typed immediately with
      no LLM step (W1.2).

## Injection & clipboard

- [ ] Copy an image and some text, dictate, then paste: the previous clipboard
      comes back (W3.1).
- [ ] Win+V history and cloud clipboard do **not** contain the dictation
      (W3.1, _verify_).
- [ ] Put the mouse in a screen corner: text still types (W3.1).
- [ ] Type `é`, `’`, `—` and an emoji: they arrive intact (W3.1).
- [ ] Dictate a bullet list into Slack/Teams/WhatsApp Web: it does not send a
      message per line (W3.1).
- [ ] Alt-Tab during processing: the text is **not** pasted into the wrong
      window — it lands on the clipboard with a notice (W3.2).
- [ ] Dictate into an elevated window: a clear notice, no silent loss
      (W3.2, _verify_).
- [ ] Turn `append_trailing_space` off and dictate into a URL bar / code editor:
      no stray space (W3.3).

## Lifecycle

- [ ] Sign out and back in: Resonant starts **hidden** (W4.2).
- [ ] Remove the entry (Task Manager → Startup, or Autoruns): it stays removed
      after a restart of the app (W4.2).
- [ ] First run on a clean profile: the "start at sign-in?" banner appears once
      (W4.2).
- [ ] Launch a second copy: the first window comes to front; nothing is killed
      (W4.3).
- [ ] Switch the accuracy model while offline: graceful failure, the old model
      keeps working, and a restart still works (W4.5).
- [ ] Stop Ollama mid-session: raw text is typed with a notice. Start it again:
      recovery without restarting Resonant (W4.7).
- [ ] Delete `settings.json`, restart: defaults from `config.py` come back with
      no errors (W4.4).
- [ ] Rename the app folder with `pythonw.exe` (no console): confirm failures
      show a MessageBox and `app_error.log` (W4.1).

## UI / UX

- [ ] Pill: appears without stealing focus, shows each stage, is clamped at the
      screen edges, and names the configured hotkey (W5.3).
- [ ] Pill does not appear in Alt+Tab and does not take typing focus (W5.3,
      _verify_).
- [ ] Click a history row: it copies. Right-click: copies the raw transcript.
      Hover highlights the whole row (W5.1).
- [ ] Keyboard-only pass: Tab reaches every control, Enter copies a focused
      history row, the focus ring is visible (W5.1, W5.7).
- [ ] 100 / 125 / 150 / 200 % display scaling: no clipped controls, sharp text
      (W5.5).
- [ ] While recording, the status dot pulses slowly (~1 Hz), not a strobe
      (W5.7).
- [ ] Tray tooltip and icon change correctly for listening / recording /
      processing / paused / error, and do not flicker (W5.4).
- [ ] Tray → *Edit settings file*: opens in Notepad and creates the file when
      missing (W5.4).
- [ ] Tray → *Clear history & logs*: history empties and `resonant.log` shrinks
      to nothing (W4.6).
- [ ] Quick Narrator/NVDA pass: note the Tk limitations; the pill, tray tooltip,
      blips and toasts are the alternatives (W5.7).

## Injection & clipboard

- [ ] Copy an image and some text, dictate, then paste: the previous clipboard
      comes back (W3.1).
- [ ] Win+V history and cloud clipboard do **not** contain the dictation
      (W3.1, _verify_).
- [ ] Put the mouse in a screen corner: text still types (W3.1).
- [ ] Type `é`, `’`, `—` and an emoji: they arrive intact (W3.1).
- [ ] Dictate a bullet list into Slack/Teams/WhatsApp Web: it does not send a
      message per line (W3.1).
- [ ] Alt-Tab during processing: the text is **not** pasted into the wrong
      window — it lands on the clipboard with a notice (W3.2).
- [ ] Dictate into an elevated window: a clear notice, no silent loss
      (W3.2, _verify_).
- [ ] Turn `append_trailing_space` off and dictate into a URL bar / code editor:
      no stray space (W3.3).

## Undo last dictation (Phase 8)

- [ ] Toggle the **Undo** switch on and set it to **Ctrl+Alt+Scroll Lock** / **Ctrl+Alt+Pause**; the tray item label updates to show the chosen chord; the tray item + chord + tray-icon title should all agree.
- [ ] With the chord enabled, dictate into Notepad: the text appears, then press the chord. The last dictation disappears and the pill/toast says "Last dictation undone".
- [ ] With the chord enabled, dictate, then Alt-Tab away (so the foreground window changed): the chord should do nothing (or the tray item should show a clear "window changed" notice).
- [ ] With the chord enabled, dictate, wait 31 s, then press the chord: the notice should say it is too old.
- [ ] With the chord enabled, dictate, then *without* the chord, type something yourself, then press the chord: the undo must never reach further back than the last dictation — it removes the prompted text only if the window and 30 s still match.
- [ ] Undo backed by **Ctrl+Z**: confirm the host app's own Ctrl+Z undo of remembered text works as expected after Resonant undoes (i.e. the two undo stacks do not fight). _verify_ per app.
- [ ] Undo of **surrogate-pair text** (emoji, CJK outside BMP): undo removes exactly the characters that were sent.
- [ ] Undo while a modifier (Shift/Ctrl/Alt/Win) is still physically down: Backspace path must refuse (would delete whole words) and explain why; Ctrl+Z path must not paste a redo into the wrong app.
- [ ] Undo with no prior dictation: notice says "Nothing to undo".
- [ ] Undo into an **elevated** window: a clear notice, no silent loss, no double-Ctrl+Z (W3.2, _verify_).
- [ ] Undo while Resonant is **hidden to the tray**: tray balloon should still say what happened.

## Lifecycle

- [ ] Sign out and back in: Resonant starts **hidden** (W4.2).
- [ ] Remove the entry (Task Manager → Startup, or Autoruns): it stays removed
      after a restart of the app (W4.2).
- [ ] First run on a clean profile: the "start at sign-in?" banner appears once
      (W4.2).
- [ ] Launch a second copy: the first window comes to front; nothing is killed
      (W4.3).
- [ ] Switch the accuracy model while offline: graceful failure, the old model
      keeps working, and a restart still works (W4.5).
- [ ] Stop Ollama mid-session: raw text is typed with a notice. Start it again:
      recovery without restarting Resonant (W4.7).
- [ ] Delete `settings.json`, restart: defaults from `config.py` come back with
      no errors (W4.4).
- [ ] Rename the app folder with `pythonw.exe` (no console): confirm failures
      show a MessageBox and `app_error.log` (W4.1).

## Benchmarks (_verify_ after Ollama is started)

- [ ] `python bench_editor.py --runs 3` with Ollama running: record the pass
      rate for `llama3.2` and `qwen2.5:3b` after the prompt tuning (W6.1).
