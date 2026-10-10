"""Dashboard smoke tests.

The pipeline tests never touch resonant_ui.py, yet that is where a typo becomes
a silently dead app: `_poll` catches every exception and only logs it, so the
window would just sit there frozen. These tests build the real dashboard against
a real engine and drive it through every render path.

Two guards keep the tests offline and non-intrusive:
  * the window is withdrawn immediately (nothing appears on screen);
  * `ui.pill` is replaced with a recorder, so the floating pill is never shown.
Skipped automatically when Tk cannot initialise.
"""
import time

import pytest

pytest.importorskip("tkinter")

import config  # noqa: E402
import resonant_engine  # noqa: E402
import settings as settings_mod  # noqa: E402


class FakePill:
    """Records pill commands instead of showing a window near the cursor."""

    def __init__(self):
        self.shown = []
        self.hidden = 0

    def show(self, text, color=None, auto_hide_ms=None):
        self.shown.append(text)

    def hide(self):
        self.hidden += 1

    def close(self):
        pass


def build_ui(monkeypatch, tmp_path):
    import tkinter as tk

    import resonant_ui
    from resonant_ui import ResonantUI

    # Never write the owner's real settings.json, install a real low-level
    # keyboard hook, or delete the owner's real log from a test.
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH",
                        str(tmp_path / "settings.json"), raising=False)
    monkeypatch.setattr(resonant_ui, "clear_log_files", lambda *a, **k: None)
    monkeypatch.setattr(resonant_engine.ResonantEngine, "_start_hotkey",
                        lambda self: None)
    settings = dict(config.DEFAULT_SETTINGS)
    settings["ai_cleanup_mode"] = "off"
    settings["sound_feedback"] = False
    engine = resonant_engine.ResonantEngine(settings=settings)
    engine.models_ready = True
    try:
        root = tk.Tk()
    except Exception as e:                      # pragma: no cover - headless
        pytest.skip(f"Tk unavailable: {e}")
    root.withdraw()
    ui = ResonantUI(root, engine)
    ui.pill = FakePill()
    return root, ui, engine


def teardown(root):
    try:
        root.destroy()
    except Exception:
        pass


def test_dashboard_builds_and_renders_every_state(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        states = [
            {"listening": False},
            {"listening": True},
            {"listening": True, "recording": True, "stage": "Listening…"},
            {"listening": True, "processing": True, "stage": "Polishing…"},
            {"listening": True, "queue_depth": 1},
            {"models_ready": False, "loading_model": "small.en"},
            {"model_error": "offline"},
            {"last_raw": "hello there", "last_final": "Hello there.",
             "last_status": "typed"},
            {"last_raw": "hello there", "last_final": "Hello there.",
             "last_status": "copied"},
            {"last_raw": "hello there", "last_final": "Hello there.",
             "last_status": "failed(no window)"},
        ]
        for overrides in states:
            ui._apply_state(engine.get_state(overrides))
        # The hidden path (pill + notices only) must not raise either.
        for overrides in states:
            ui._apply_light(engine.get_state(overrides))
    finally:
        teardown(root)


def test_history_rows_render_and_statuses_map_to_glyphs(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        for status in ("typed", "copied", "failed(error)", "dropped(quiet)",
                       "dropped(no-mic)"):
            engine._record_history("raw words", "Final words.", status)
        ui._apply_state(engine.get_state())
        assert len(ui.hist_frame.winfo_children()) == 5
        ui._clear_all()
        assert ui.hist_frame.winfo_children()      # the empty-state label
    finally:
        teardown(root)


def test_hotkey_picker_switches_key_and_mode(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        ui._change_hotkey("f10")
        assert engine.settings["hotkey"] == "f10"
        assert "f10" in engine.notice.lower()

        ui._toggle_hotkey_mode()
        assert engine.settings["hotkey_mode"] == "toggle"
        assert "tap" in engine.notice.lower()
        ui._toggle_hotkey_mode()
        assert engine.settings["hotkey_mode"] == "hold"

        # Invalid keys must not change anything (validation, W2.2).
        ui._change_hotkey("banana")
        assert engine.settings["hotkey"] == "f10"
    finally:
        teardown(root)


def test_menus_follow_settings(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        ui._apply_state(engine.get_state())
        assert ui.hotkey_var.get() == "F9"
        assert ui.mic_var.get() == "System default"

        engine.settings["hotkey"] = "f12"
        engine.settings["input_device"] = None
        ui._apply_state(engine.get_state())
        assert ui.hotkey_var.get() == "F12"

        # A device that is no longer connected still shows, so the user can see
        # why dictation is silent instead of facing an empty dropdown.
        engine.settings["input_device"] = 99
        assert ui._mic_label_for(99) == "device #99 (not connected)"
    finally:
        teardown(root)


def test_toggle_mode_hints_and_pill(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        engine.settings["hotkey_mode"] = "toggle"
        ui._apply_state(engine.get_state({"listening": True}))
        assert "tap F9 to start / stop" in ui.hint_lbl.cget("text")
        ui._apply_state(engine.get_state({"listening": True, "recording": True}))
        assert "tap F9 to stop" in ui.hint_lbl.cget("text")
        assert "tap again to stop" in ui.pill.shown[-1]

        engine.settings["hotkey_mode"] = "hold"
        ui._apply_state(engine.get_state({"listening": True, "recording": True}))
        assert "release to finish" in ui.hint_lbl.cget("text")
        assert "release F9 to type" in ui.pill.shown[-1]
    finally:
        teardown(root)


def test_notice_is_shown_once_per_sequence(monkeypatch, tmp_path):
    """A stale notice must not replay when the window is re-shown (W5.2)."""
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        engine._notice("No speech (too quiet).")
        ui._apply_state(engine.get_state())
        first = ui._notice_until
        assert time.monotonic() < first

        # Same notice, many polls later: the window must not be extended.
        ui._apply_state(engine.get_state())
        assert ui._notice_until == first

        engine._notice("Microphone unavailable (busy).")
        ui._apply_state(engine.get_state())
        assert ui._notice_until > first
    finally:
        teardown(root)


def test_notices_reach_the_pill_while_the_dashboard_is_hidden(monkeypatch,
                                                              tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        ui._apply_state(engine.get_state())
        ui.hide()                                    # dashboard closed to tray
        engine._notice("Dictation failed — see the log.")
        ui._apply_light(engine.get_state())          # the hidden poll path
        assert ui.pill.shown[-1] == "Dictation failed — see the log."
    finally:
        teardown(root)


def test_silence_stop_control_cycles_valid_windows(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        ui._apply_state(engine.get_state())
        assert engine.settings["silence_autostop_ms"] == 0
        assert ui.silence_btn.cget("text") == "Silence stop: Off"

        ui._cycle_silence_stop()
        assert engine.settings["silence_autostop_ms"] == 1000
        ui._apply_state(engine.get_state())
        assert ui.silence_btn.cget("text") == "Silence stop: 1 s"

        # Walk the whole ring: 1500, 2000, 3000, 5000, then back to off.
        for expected in (1500, 2000, 3000, 5000, 0):
            ui._cycle_silence_stop()
            assert engine.settings["silence_autostop_ms"] == expected
        ui._apply_state(engine.get_state())
        assert ui.silence_btn.cget("text") == "Silence stop: Off"

        # A value outside the ring must not break the cycle (defensive index).
        engine.settings["silence_autostop_ms"] = 700
        ui._cycle_silence_stop()
        assert engine.settings["silence_autostop_ms"] == 1000
    finally:
        teardown(root)


def test_undo_chord_control_cycles_valid_chords(monkeypatch, tmp_path):
    root, ui, engine = build_ui(monkeypatch, tmp_path)
    try:
        ui._apply_state(engine.get_state())
        assert engine.settings["undo_chord"] == "off"
        assert ui.undo_btn.cget("text") == "Undo: Off"

        ui._cycle_undo_chord()
        assert engine.settings["undo_chord"] == "ctrl+alt+scroll_lock"
        ui._apply_state(engine.get_state())
        assert ui.undo_btn.cget("text") == "Undo: Ctrl+Alt+Scroll Lock"

        ui._cycle_undo_chord()
        assert engine.settings["undo_chord"] == "ctrl+alt+pause"
        ui._apply_state(engine.get_state())
        assert ui.undo_btn.cget("text") == "Undo: Ctrl+Alt+Pause"

        ui._cycle_undo_chord()
        assert engine.settings["undo_chord"] == "off"
        ui._apply_state(engine.get_state())
        assert ui.undo_btn.cget("text") == "Undo: Off"
    finally:
        teardown(root)


if __name__ == "__main__":
    pytest.main([__file__])
