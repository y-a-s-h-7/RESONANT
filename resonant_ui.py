"""Resonant — UI layer: dashboard, floating pill, tray icon.

All tkinter work happens on the main thread. Engine events arrive on background
threads, so every callback is marshalled back with `root.after(0, …)` and the
dashboard also polls state (100 ms visible / 500 ms hidden).

Fixes carried by this rewrite (Phase 5):
  W5.1 history rows are clickable and keyboard-operable, with a status glyph
  W5.2 the "live" line actually shows what was heard / the last notice
  W5.3 the pill is persistent, shows every stage, never steals focus
  W5.4 tray icons are cached; settings file opens in Notepad; copy/clear entries
  W5.5 layout follows requested sizes; footer packed first; DPI set in resonant.py
  W5.6 `_poll` re-arms in `finally`; hidden windows skip dashboard redraws
  W5.7 toggle state is text, not colour-only; focus ring; ~1 Hz pulse
"""
import os
import subprocess
import threading
import time
import tkinter as tk

import pystray
from PIL import Image, ImageDraw, ImageTk

import hotkey as hotkey_mod
import startup
from config import APP_NAME, LOG_PATH, SETTINGS_PATH
from logging_setup import clear_log_files, log

# --- Colors (single dark palette; contrast raised for accessibility) ---
BG = "#0f1115"
CARD = "#171a21"
CARD2 = "#1f242e"
LINE = "#2b3240"
TXT = "#eef0f3"
MUT = "#a2a9b6"
GREEN = "#35d07f"
GREEN_DK = "#1d8a4c"
RED = "#ff5f52"
AMBER = "#f5a524"
FONT = "Segoe UI"

POLL_MS = 100
POLL_HIDDEN_MS = 500
PULSE_MS = 1100
NOTICE_MS = 2400
DROP_GAP = 0.6

STATUS_GLYPH = {
    "typed": "✓",
    "copied": "⧉",
    "failed": "⚠",
    "dropped": "–",
}


def status_key(status):
    status = status or ""
    for key in ("typed", "copied", "failed", "dropped"):
        if status.startswith(key):
            return key
    return "failed" if status else "dropped"


# ------------------------------------------------------------ brand icon
def draw_icon(active=True, recording=False, error=False):
    """Sound-wave glyph in a rounded-square badge. Cached by the tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if error:
        outline, color = (255, 95, 82, 255), (255, 95, 82, 255)
    elif recording:
        outline, color = (255, 95, 82, 255), (255, 95, 82, 255)
    elif active:
        outline, color = (53, 208, 127, 255), (53, 208, 127, 255)
    else:
        outline, color = (138, 145, 158, 200), (150, 156, 168, 230)
    d.rounded_rectangle([2, 2, 62, 62], radius=16,
                        fill=(23, 26, 33, 255), outline=outline, width=3)
    for x, h in ((20, 16), (28, 30), (36, 22), (44, 12)):
        d.rounded_rectangle([x, 32 - h // 2, x + 4, 32 + h // 2], radius=2,
                            fill=color)
    return img


# ------------------------------------------------------------------- pill
class Pill:
    """Persistent, reusable status bubble near the cursor (W5.3).

    WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW so it can never steal focus or show up
    in Alt+Tab (_verify_ manually).
    """

    EX_NOACTIVATE = 0x08000000
    EX_TOOLWINDOW = 0x00000080

    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=CARD, highlightbackground=GREEN, highlightthickness=1)
        self.win.withdraw()
        self.inner = tk.Frame(self.win, bg=CARD)
        self.inner.pack(padx=12, pady=7)
        self.dot = tk.Label(self.inner, text="●", fg=RED, bg=CARD, font=(FONT, 10))
        self.dot.pack(side="left")
        self.label = tk.Label(self.inner, text="", fg=TXT, bg=CARD, font=(FONT, 10))
        self.label.pack(side="left", padx=(6, 0))
        self._hide_job = None
        self._text = None
        self.win.update_idletasks()
        self._make_noactivate()

    def _make_noactivate(self):
        try:
            import ctypes
            hwnd = self.win.winfo_id()
            user32 = ctypes.windll.user32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            ex_style = get_long(hwnd, -20)
            set_long(hwnd, -20, ex_style | self.EX_NOACTIVATE | self.EX_TOOLWINDOW)
        except Exception:
            pass

    def _place(self):
        try:
            import ctypes
            from ctypes import wintypes
            pt = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            # Clamp to the monitor work area so the pill is never off-screen.
            r = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
            self.win.update_idletasks()
            w = self.win.winfo_reqwidth()
            h = self.win.winfo_reqheight()
            x = min(max(pt.x + 18, r.left + 8), max(r.left + 8, r.right - w - 8))
            y = min(max(pt.y + 26, r.top + 8), max(r.top + 8, r.bottom - h - 8))
            self.win.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def show(self, text, color=GREEN, auto_hide_ms=None):
        if text != self._text:
            self._text = text
            self.label.configure(text=text)
        self.win.configure(highlightbackground=color)
        self.dot.configure(fg=color)
        self._place()
        if not self.win.winfo_viewable():
            self.win.deiconify()
        if self._hide_job is not None:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None
        if auto_hide_ms:
            self._hide_job = self.root.after(auto_hide_ms, self.hide)

    def hide(self):
        self._hide_job = None
        try:
            self.win.withdraw()
        except Exception:
            pass

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- dashboard
class ResonantUI:
    def __init__(self, root, engine):
        self.root = root
        self.engine = engine
        self.on_quit = None
        self.pill = Pill(root)
        self._pulse_dir = True
        self._last_pulse = 0.0
        self._history_key = None
        self._notice_until = 0.0
        self._notice_seq = None
        self._mic_map = {}
        self._startup_banner = None
        self._copied_job = None
        self._focus_ring = {}

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.minsize(430, 520)
        root.resizable(True, True)
        root.iconphoto(True, ImageTk.PhotoImage(draw_icon(True)))
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._build()
        root.update_idletasks()
        w = max(440, root.winfo_reqwidth())
        h = max(560, root.winfo_reqheight())
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(20, (sh - h) // 3)}")
        self._poll()

    # ------------------------------------------------------------- layout
    def _card(self, parent, pady=(0, 10)):
        c = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        c.pack(fill="x", pady=pady, padx=16)
        return c

    def _build(self):
        # Footer is packed FIRST so it can never be the clipped element (W5.5).
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", side="bottom", pady=8)
        self._button(foot, "Open log", self._open_log).pack(side="left", padx=8)
        self._button(foot, "Clear history & logs", self._clear_all).pack(side="left")
        self._button(foot, "Hide to tray", self.hide).pack(side="right", padx=8)
        self.copied_lbl = tk.Label(foot, text="", fg=GREEN, bg=BG, font=(FONT, 9))
        self.copied_lbl.pack(side="right", padx=6)

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", pady=(14, 6), padx=20)
        tk.Label(head, text=APP_NAME, fg=TXT, bg=BG,
                 font=(FONT, 19, "bold")).pack(side="left")
        self.version_lbl = tk.Label(head, text="  voice → clean text, fully local",
                                    fg=MUT, bg=BG, font=(FONT, 9))
        self.version_lbl.pack(side="left", pady=(8, 0))

        # First-run startup consent (W4.2) — non-blocking banner, hidden by default
        self.banner = tk.Frame(self.root, bg=CARD2)
        tk.Label(self.banner, text="Start Resonant automatically when you sign in?",
                 fg=TXT, bg=CARD2, font=(FONT, 9)).pack(side="left", padx=12, pady=8)
        self._button(self.banner, "Yes", lambda: self._startup_choice(True)).pack(
            side="right", padx=(4, 10), pady=6)
        self._button(self.banner, "No", lambda: self._startup_choice(False)).pack(
            side="right", pady=6)

        # Status card
        sc = self._card(self.root)
        row = tk.Frame(sc, bg=CARD)
        row.pack(fill="x", padx=14, pady=12)
        self.dot = tk.Canvas(row, width=16, height=16, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left")
        self.status_lbl = tk.Label(row, text="Starting…", fg=TXT, bg=CARD,
                                   font=(FONT, 11, "bold"))
        self.status_lbl.pack(side="left", padx=10)
        self.hint_lbl = tk.Label(row, text="", fg=MUT, bg=CARD, font=(FONT, 9),
                                 wraplength=200, justify="right")
        self.hint_lbl.pack(side="right")

        self.meter = tk.Canvas(sc, width=370, height=6, bg=CARD, highlightthickness=0)
        self.meter.pack(fill="x", padx=14, pady=(0, 10))

        # W5.2: this line is actually updated now
        self.live_lbl = tk.Label(sc, text="", fg=MUT, bg=CARD, font=(FONT, 9),
                                 wraplength=370, justify="left", anchor="w")
        self.live_lbl.pack(fill="x", padx=14, pady=(0, 10))

        self.toggle_btn = tk.Button(self.root, font=(FONT, 11, "bold"),
                                    relief="flat", cursor="hand2", bd=0,
                                    activeforeground="#06130a",
                                    command=self._toggle_listening)
        self.toggle_btn.pack(fill="x", padx=16, pady=(2, 10), ipady=8)
        self._add_focus_ring(self.toggle_btn)

        # Settings card
        st = self._card(self.root)
        tk.Label(st, text="SETTINGS", fg=MUT, bg=CARD,
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=14, pady=(10, 4))
        srow = tk.Frame(st, bg=CARD)
        srow.pack(fill="x", padx=14, pady=(0, 4))
        self.cleanup_btn = self._toggle_btn(srow, "AI cleanup", self._cycle_cleanup)
        self.sound_btn = self._toggle_btn(srow, "Sounds", self._toggle_sound)
        prow = tk.Frame(st, bg=CARD)
        prow.pack(fill="x", padx=14, pady=(0, 6))
        self.paste_btn = self._toggle_btn(prow, "Typing", self._cycle_inject, col=False)
        self.space_btn = self._toggle_btn(prow, "Trailing space",
                                          self._toggle_trailing_space)
        mrow = tk.Frame(st, bg=CARD)
        mrow.pack(fill="x", padx=14, pady=(2, 8))
        tk.Label(mrow, text="Accuracy:", fg=MUT, bg=CARD,
                 font=(FONT, 9)).pack(side="left")
        models = ["tiny.en", "base.en", "small.en"]
        current_model = self.engine.settings.get("stt_model")
        if current_model and current_model not in models:
            models.insert(0, current_model)
        self.model_var = tk.StringVar()
        self.model_menu = self._menu(mrow, self.model_var, models,
                                     self._change_model)
        self.model_menu.pack(side="left", padx=8)
        self.model_hint = tk.Label(mrow, text="applies immediately", fg=MUT,
                                   bg=CARD, font=(FONT, 8))
        self.model_hint.pack(side="left")

        # Hotkey picker + hold/toggle mode (W2.2b): neither was reachable from
        # the app before — changing the key meant hand-editing settings.json.
        hrow = tk.Frame(st, bg=CARD)
        hrow.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(hrow, text="Hotkey:", fg=MUT, bg=CARD,
                 font=(FONT, 9)).pack(side="left")
        self.hotkey_var = tk.StringVar()
        self.hotkey_menu = self._menu(hrow, self.hotkey_var, self._hotkey_options(),
                                      self._change_hotkey)
        self.hotkey_menu.pack(side="left", padx=8)
        self.mode_btn = self._toggle_btn(hrow, "Mode: Hold",
                                         self._toggle_hotkey_mode)
        self.hotkey_hint = tk.Label(hrow, text="", fg=MUT, bg=CARD,
                                    font=(FONT, 8))
        self.hotkey_hint.pack(side="left")

        microw = tk.Frame(st, bg=CARD)
        microw.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(microw, text="Microphone:", fg=MUT, bg=CARD,
                 font=(FONT, 9)).pack(side="left")
        self.mic_var = tk.StringVar()
        self.mic_menu = self._menu(microw, self.mic_var, self._mic_options(),
                                   self._change_mic)
        self.mic_menu.pack(side="left", padx=8)
        self.mic_hint = tk.Label(microw, text="applies to the next recording",
                                 fg=MUT, bg=CARD, font=(FONT, 8))
        self.mic_hint.pack(side="left")

        # History card
        hc = self._card(self.root, pady=(0, 8))
        tk.Label(hc, text="HISTORY   click = copy · right-click = copy raw",
                 fg=MUT, bg=CARD, font=(FONT, 8, "bold")).pack(
            anchor="w", padx=14, pady=(10, 2))
        self.hist_frame = tk.Frame(hc, bg=CARD)
        self.hist_frame.pack(fill="x", padx=10, pady=(0, 10))

    def _menu(self, parent, var, options, command):
        """One styled OptionMenu for every dropdown (keeps the palette in one place)."""
        menu = tk.OptionMenu(parent, var, *options, command=command)
        menu.configure(bg=CARD2, fg=TXT, relief="flat", activebackground=CARD2,
                       activeforeground=TXT, font=(FONT, 9), highlightthickness=0)
        return menu

    @staticmethod
    def _input_devices():
        """[(index, name)] for every real input device; [] when unavailable."""
        try:
            import sounddevice as sd
            found = []
            for index, dev in enumerate(sd.query_devices()):
                if int(dev.get("max_input_channels") or 0) > 0:
                    name = str(dev.get("name") or f"device {index}")
                    found.append((index, name))
            return found
        except Exception as e:
            log(f"Microphone list unavailable: {e}")
            return []

    def _hotkey_options(self):
        """F1–F12 in NUMERIC order, then the rarely-wanted keys.

        `hotkey.ALLOWED_KEYS` is alphabetically sorted (F1, F10, F11, F2 …),
        which makes for an unusable dropdown; the current key is always kept in
        the list so a hand-edited settings.json still displays.
        """
        wanted = [f"f{i}" for i in range(1, 13)] + ["pause", "capslock"]
        options = [key.upper() for key in wanted
                   if key in hotkey_mod.ALLOWED_KEYS]
        current = (self.engine.settings.get("hotkey") or "f9").upper()
        if current not in options:
            options.append(current)
        return options

    def _mic_options(self):
        """Dropdown labels -> device index (None = system default)."""
        self._mic_map = {"System default": None}
        options = ["System default"]
        for index, name in self._input_devices():
            label = name if len(name) <= 30 else name[:29] + "…"
            label = f"{label}  (#{index})"
            options.append(label)
            self._mic_map[label] = index
        current = self.engine.settings.get("input_device")
        if current is not None and current not in self._mic_map.values():
            label = f"device #{current} (not connected)"
            options.append(label)
            self._mic_map[label] = current
        return options

    def _mic_label_for(self, index):
        for label, mapped in self._mic_map.items():
            if mapped == index:
                return label
        return "System default" if index is None else f"device #{index} (not connected)"

    def _button(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, relief="flat",
                      bg=CARD, fg=MUT, activebackground=CARD2, activeforeground=TXT,
                      font=(FONT, 9), cursor="hand2", bd=0, padx=12, pady=4)
        self._add_focus_ring(b)
        return b

    def _toggle_btn(self, parent, label, cmd, col=True):
        b = tk.Button(parent, text=label, relief="flat", bd=0, cursor="hand2",
                      font=(FONT, 9), padx=10, pady=5, bg=CARD2, fg=TXT,
                      activebackground=CARD2, command=cmd)
        b.pack(side="left", padx=(0, 8))
        self._add_focus_ring(b)
        return b

    def _add_focus_ring(self, widget):
        """Visible focus ring (W5.7)."""
        def on_in(_e):
            try:
                widget.configure(highlightthickness=2, highlightbackground=GREEN,
                                 highlightcolor=GREEN)
            except Exception:
                pass

        def on_out(_e):
            try:
                widget.configure(highlightthickness=0)
            except Exception:
                pass
        widget.bind("<FocusIn>", on_in)
        widget.bind("<FocusOut>", on_out)
        widget.configure(takefocus=1)

    # -------------------------------------------------------------- poll
    def _note_notice(self, s):
        """Timestamp a NEW notice exactly once (W5.2).

        Driven by `notice_seq`, not by comparing text: a notice that arrived
        while the dashboard was hidden is shown once when the window returns,
        instead of replaying a minutes-old message whenever it is reopened.
        """
        seq = s.get("notice_seq")
        if seq is not None and seq == self._notice_seq:
            return
        self._notice_seq = seq
        if s.get("notice"):
            self._notice_until = time.monotonic() + NOTICE_MS / 1000

    def _poll(self):
        try:
            s = self.engine.get_state()
            if not self.root.winfo_viewable():
                self._apply_light(s)
            else:
                self._apply_state(s)
        except Exception as e:                 # W5.6: one error must not freeze the UI
            log(f"UI poll error: {e}")
        finally:
            delay = POLL_MS if self.root.winfo_viewable() else POLL_HIDDEN_MS
            self.root.after(delay, self._poll)

    def _apply_light(self, s):
        """While hidden: pill + tray only (W5.6)."""
        self._update_pill(s)

    def _apply_state(self, s):
        st = s["settings"]
        rec, proc, ready, listening = (s["recording"], s["processing"],
                                       s["models_ready"], s["listening"])
        hotkey = (st.get("hotkey") or "f9").upper()
        hk_mode = st.get("hotkey_mode", "hold")

        if s.get("model_error"):
            color, text, hint = RED, "Model failed to load", "see the log"
        elif s.get("loading_model"):
            color, text = AMBER, f"Loading {s['loading_model']}…"
            hint = "dictation continues on the current model"
        elif not ready:
            color, text, hint = MUT, "Loading AI models…", "one moment"
        elif rec:
            now = time.monotonic()
            if now - self._last_pulse > PULSE_MS / 1000:
                self._pulse_dir = not self._pulse_dir
                self._last_pulse = now
            color = RED if self._pulse_dir else "#7a241c"
            text = "Recording — speak now"
            # Never tell a toggle-mode user to release a key (W2.2b).
            hint = (f"tap {hotkey} to stop" if hk_mode == "toggle"
                    else "release to finish")
        elif proc:
            color, text, hint = AMBER, "Processing…", s.get("stage") or "working"
        elif s.get("queue_depth"):
            color, text = AMBER, "Busy — queued"
            hint = f"{s['queue_depth']} waiting"
        elif listening:
            color, text = GREEN, "Listening"
            hint = (f"tap {hotkey} to start / stop" if hk_mode == "toggle"
                    else f"hold {hotkey} anywhere to dictate")
        else:
            color, text, hint = MUT, "Paused", "press Start to begin"

        self.dot.delete("all")
        self.dot.create_oval(2, 2, 14, 14, fill=color, outline="")
        self.status_lbl.configure(text=text)
        self.hint_lbl.configure(text=hint)

        self.meter.delete("all")
        if rec:
            w = int(min(1.0, s["level"]) * 366)
            self.meter.create_rectangle(0, 0, max(w, 2), 6, fill=GREEN, width=0)

        if listening:
            self.toggle_btn.configure(text="Pause listening", bg=CARD2, fg=TXT,
                                      activebackground=LINE, activeforeground=TXT)
        else:
            self.toggle_btn.configure(text="Start listening", bg=GREEN, fg="#06130a",
                                      activebackground=GREEN_DK)

        # Text states, never colour-only (W5.7)
        mode = st.get("ai_cleanup_mode", "smart")
        self.cleanup_btn.configure(
            text=f"AI cleanup: {mode.capitalize()}",
            bg=CARD2 if mode != "off" else CARD,
            fg=GREEN if mode != "off" else MUT)
        self.sound_btn.configure(
            text=f"Sounds: {'On' if st.get('sound_feedback') else 'Off'}",
            bg=CARD2 if st.get("sound_feedback") else CARD,
            fg=GREEN if st.get("sound_feedback") else MUT)
        inject = st.get("inject_mode", "auto")
        self.paste_btn.configure(text=f"Typing: {inject.capitalize()}",
                                 bg=CARD2, fg=TXT)
        self.space_btn.configure(
            text=f"Trailing space: {'On' if st.get('append_trailing_space') else 'Off'}",
            bg=CARD2 if st.get("append_trailing_space") else CARD,
            fg=GREEN if st.get("append_trailing_space") else MUT)

        if self.model_var.get() != st.get("stt_model"):
            self.model_var.set(st.get("stt_model", ""))
        self.model_hint.configure(
            text="loading…" if s.get("loading_model") else "applies immediately")

        if self.hotkey_var.get().lower() != (st.get("hotkey") or "f9").lower():
            self.hotkey_var.set(hotkey)
        self.mode_btn.configure(
            text=f"Mode: {'Toggle' if hk_mode == 'toggle' else 'Hold'}")
        self.hotkey_hint.configure(
            text="tap to start / stop" if hk_mode == "toggle" else "hold to talk")
        mic_label = self._mic_label_for(st.get("input_device"))
        if self.mic_var.get() != mic_label:
            self.mic_var.set(mic_label)

        self._update_live(s)
        self._render_history(s["history"])
        self._update_pill(s)

    def _update_live(self, s):
        notice = s.get("notice") or ""
        now = time.monotonic()
        self._note_notice(s)
        if notice and now < self._notice_until:
            self.live_lbl.configure(text=notice, fg=AMBER)
        elif s.get("last_final"):
            raw = (s.get("last_raw") or "")[:70]
            final = (s.get("last_final") or "")[:90]
            text = f"“{final}”"
            if raw and raw != final:
                text = f"heard: {raw}\n→ {text}"
            fg = GREEN if status_key(s.get("last_status")) in ("typed", "copied") else MUT
            self.live_lbl.configure(text=text, fg=fg)
        else:
            self.live_lbl.configure(
                text="Hold the hotkey anywhere, speak, release — the text is typed "
                     "wherever the cursor is.", fg=MUT)

    def _update_pill(self, s):
        hotkey = (s["settings"].get("hotkey") or "f9").upper()
        toggle = s["settings"].get("hotkey_mode") == "toggle"
        # Also runs while the dashboard is hidden, so a notice still reaches the
        # pill and its 2.4 s window is measured from arrival, not from the next
        # time the window happens to be visible (W5.3).
        self._note_notice(s)
        if s["recording"]:
            tail = "tap again to stop" if toggle else f"release {hotkey} to type"
            self.pill.show(f"Listening… {tail}", RED)
            return
        if s["processing"]:
            self.pill.show(s.get("stage") or "Processing…", AMBER)
            return
        notice = s.get("notice") or ""
        if notice and time.monotonic() < self._notice_until:
            self.pill.show(notice, AMBER,
                           auto_hide_ms=max(300, int((self._notice_until
                                                      - time.monotonic()) * 1000)))
            return
        if not s["recording"] and not s["processing"]:
            self.pill.hide()

    # --------------------------------------------------------- history (W5.1)
    def _render_history(self, history):
        rows = history[-8:]
        key = tuple((h.get("ts"), h.get("status"), h.get("final")) for h in rows)
        if key == self._history_key:
            return
        self._history_key = key
        for w in self.hist_frame.winfo_children():
            w.destroy()
        if not rows:
            tk.Label(self.hist_frame, text="Your dictations will appear here.",
                     fg=MUT, bg=CARD, font=(FONT, 9)).pack(anchor="w", padx=6, pady=6)
            return
        for item in reversed(rows):
            self._history_row(item)

    def _history_row(self, item):
        final = item.get("final") or ""
        raw = item.get("raw") or ""
        skey = status_key(item.get("status"))
        row = tk.Frame(self.hist_frame, bg=CARD2, cursor="hand2", takefocus=1)
        row.pack(fill="x", pady=2)
        glyph_color = {"typed": GREEN, "copied": AMBER, "failed": RED,
                       "dropped": MUT}[skey]
        glyph = tk.Label(row, text=STATUS_GLYPH[skey], fg=glyph_color, bg=CARD2,
                         font=(FONT, 9, "bold"), width=2)
        glyph.pack(side="left", padx=(6, 0))
        time_lbl = tk.Label(row, text=item.get("ts", ""), fg=MUT, bg=CARD2,
                            font=(FONT, 8))
        time_lbl.pack(side="left", padx=(2, 6))
        short = final if len(final) <= 58 else final[:57] + "…"
        text_lbl = tk.Label(row, text=short or "(nothing typed)", fg=TXT, bg=CARD2,
                            font=(FONT, 9), anchor="w", justify="left")
        text_lbl.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=3)

        widgets = [row, glyph, time_lbl, text_lbl]

        def copy_final(_event=None):
            self._copy(final or raw)

        def copy_raw(_event=None):
            self._copy(raw or final)

        def on_enter(_event=None):
            for w in widgets:
                w.configure(bg=LINE)

        def on_leave(_event=None):
            for w in widgets:
                w.configure(bg=CARD2)

        for w in widgets:
            w.bind("<Button-1>", copy_final)
            w.bind("<Button-3>", copy_raw)
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
        row.bind("<Return>", copy_final)
        row.bind("<space>", copy_final)

    def _copy(self, text):
        if not text:
            return
        try:
            from platform_win import real_platform
            if not real_platform().clipboard_set_text(text, exclude_history=False):
                return
        except Exception:
            return
        self._flash_copied()

    def _flash_copied(self):
        self.copied_lbl.configure(text="copied ✓")
        if self._copied_job:
            try:
                self.root.after_cancel(self._copied_job)
            except Exception:
                pass
        self._copied_job = self.root.after(1500, lambda: self.copied_lbl.configure(text=""))

    # ---------------------------------------------------------- actions
    def _toggle_listening(self):
        self.engine.set_listening(not self.engine.listening)

    def _cycle_cleanup(self):
        order = ["smart", "always", "off"]
        current = self.engine.settings.get("ai_cleanup_mode", "smart")
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else "smart"
        self.engine.apply_settings({"ai_cleanup_mode": nxt})

    def _toggle_sound(self):
        self.engine.apply_settings(
            {"sound_feedback": not self.engine.settings.get("sound_feedback", True)})

    def _toggle_trailing_space(self):
        self.engine.apply_settings(
            {"append_trailing_space":
             not self.engine.settings.get("append_trailing_space", True)})

    def _cycle_inject(self):
        order = ["auto", "type", "paste"]
        current = self.engine.settings.get("inject_mode", "auto")
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else "auto"
        self.engine.apply_settings({"inject_mode": nxt})

    def _change_model(self, value):
        if self.engine.is_recording or self.engine.processing:
            self.engine.set_notice("Finish the current dictation first.")
            return
        if value == self.engine.settings.get("stt_model"):
            return
        threading.Thread(target=self.engine.change_model, args=(value,),
                         daemon=True).start()

    def _toggle_hotkey_mode(self):
        current = self.engine.settings.get("hotkey_mode", "hold")
        new_mode = "hold" if current == "toggle" else "toggle"
        if not self.engine.apply_settings({"hotkey_mode": new_mode}):
            return
        key = (self.engine.settings.get("hotkey") or "f9").upper()
        self.engine.set_notice(
            f"{key}: " + ("tap to start, tap again to stop" if new_mode == "toggle"
                          else "hold while you speak"))

    def _change_hotkey(self, value):
        key = (value or "").lower()
        if not key or key == self.engine.settings.get("hotkey"):
            return
        if not self.engine.apply_settings({"hotkey": key}):
            self.hotkey_var.set((self.engine.settings.get("hotkey") or "f9").upper())
            return
        mode = self.engine.settings.get("hotkey_mode", "hold")
        self.engine.set_notice(
            f"Hotkey is now {key.upper()} — " + ("tap to start / stop"
                                                 if mode == "toggle"
                                                 else "hold to dictate"))

    def _change_mic(self, label):
        index = self._mic_map.get(label)
        if index == self.engine.settings.get("input_device"):
            return
        if self.engine.apply_settings({"input_device": index}):
            self.engine.set_notice(
                "Microphone: " + ("system default" if index is None else label))

    def _open_log(self):
        try:
            if not os.path.exists(LOG_PATH):
                log("(log opened before anything was written)")
            subprocess.Popen(["notepad.exe", LOG_PATH])
        except Exception:
            os.startfile(os.path.dirname(LOG_PATH))

    def _open_settings_file(self):
        try:
            if not os.path.exists(SETTINGS_PATH):
                from settings import save_settings
                save_settings(dict(self.engine.settings))
            subprocess.Popen(["notepad.exe", SETTINGS_PATH])
        except Exception as e:
            log(f"Could not open settings file: {e}")

    def _clear_all(self):
        clear_log_files()
        self.engine.clear_history()
        self._history_key = None
        self._flash_copied()
        self.copied_lbl.configure(text="history & logs cleared")

    def _startup_choice(self, enabled):
        try:
            startup.apply_user_choice(self.engine.settings, enabled)
            self.engine.settings["startup_prompted"] = True
        except Exception as e:
            log(f"Startup choice failed: {e}")
        self._hide_banner()

    def show_startup_prompt(self):
        if self._startup_banner is not None:
            return
        self._startup_banner = self.banner
        self.banner.pack(fill="x", pady=(0, 8), before=self.toggle_btn)
        try:
            self.root.deiconify()
        except Exception:
            pass

    def _hide_banner(self):
        if self._startup_banner is None:
            return
        try:
            self._startup_banner.pack_forget()
        except Exception:
            pass
        self._startup_banner = None

    def hide(self):
        self.pill.hide()
        self.root.withdraw()


# ------------------------------------------------------------------ tray
def create_tray(engine, ui):
    """System tray icon; menu actions marshal onto the tkinter thread."""
    icons = {}

    def icon_for(name):
        if name not in icons:
            icons[name] = draw_icon(active=name in ("ready", "recording"),
                                    recording=name == "recording",
                                    error=name == "error")
        return icons[name]

    def show():
        ui.root.after(0, lambda: (ui.root.deiconify(), ui.root.lift()))

    def toggle_listen(icon, item):
        ui.root.after(0, ui._toggle_listening)

    def listen_label(item):
        return "Pause listening" if engine.listening else "Start listening"

    def mode_label(item):
        return ("Mode: Toggle" if engine.settings.get("hotkey_mode") == "toggle"
                else "Mode: Hold")

    def toggle_mode(icon, item):
        ui.root.after(0, ui._toggle_hotkey_mode)

    def open_log(icon, item):
        ui.root.after(0, ui._open_log)

    def open_settings_file(icon, item):
        ui.root.after(0, ui._open_settings_file)

    def copy_last(icon, item):
        final = engine.get_state().get("last_final") or ""
        if final:
            ui.root.after(0, lambda: ui._copy(final))

    def clear_all(icon, item):
        ui.root.after(0, ui._clear_all)

    def toggle_startup(icon, item):
        """pystray passes the menu item; `item.checked` is the CURRENT state."""
        enabled = not bool(item.checked)
        startup.apply_user_choice(engine.settings, enabled)
        engine.settings["startup_prompted"] = True
        ui.root.after(0, lambda: ui._hide_banner())

    def startup_checked(item):
        return startup.is_enabled()

    def quit_app(icon, item):
        log("Exiting from tray menu.")
        icon.stop()
        if ui.on_quit:
            ui.root.after(0, ui.on_quit)
        else:
            ui.root.after(0, lambda: os._exit(0))

    def on_double(icon):
        show()

    menu = pystray.Menu(
        pystray.MenuItem("Show Resonant", lambda i: show(), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(listen_label, toggle_listen),
        pystray.MenuItem(mode_label, toggle_mode),
        pystray.MenuItem("Copy last dictation", copy_last),
        pystray.MenuItem("Clear history & logs", clear_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open log", open_log),
        pystray.MenuItem("Edit settings file", open_settings_file),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Run at Windows startup", toggle_startup,
                         checked=startup_checked, radio=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", quit_app),
    )
    icon = pystray.Icon(APP_NAME, icon=icon_for("paused"),
                        title=f"{APP_NAME} — starting…", menu=menu)

    tray_state = {"name": None}

    def _tray_state(s):
        """Mirror engine state onto the tray icon; assign only on change (W5.4)."""
        try:
            if s.get("model_error"):
                name = "error"
            elif s.get("recording"):
                name = "recording"
            elif s.get("processing"):
                name = "processing"
            elif not s["models_ready"]:
                name = "loading"
            elif s["listening"] and s["settings"].get("swallow_hotkey"):
                name = "ready"
            elif s["listening"]:
                name = "ready_poll"
            else:
                name = "paused"
            if name != tray_state["name"]:
                tray_state["name"] = name
                if name == "processing":
                    icon.icon = icons.setdefault("processing", draw_icon(active=True))
                elif name == "loading":
                    icon.icon = icons.setdefault("loading", draw_icon(active=False))
                elif name in ("ready", "ready_poll"):
                    icon.icon = icon_for("ready")
                else:
                    icon.icon = icon_for(name)
            hotkey = (s["settings"].get("hotkey") or "f9").upper()
            if s.get("recording"):
                tip = "Recording…"
            elif s.get("processing"):
                tip = s.get("stage") or "Processing…"
            elif not s["models_ready"]:
                tip = "Loading AI models…"
            elif s["listening"]:
                if s["settings"].get("hotkey_mode") == "toggle":
                    tip = f"Listening — tap {hotkey} to start / stop"
                else:
                    tip = f"Listening — hold {hotkey} to dictate"
            else:
                tip = "Paused"
            icon.title = f"{APP_NAME} — {tip}"
        except Exception as e:
            log(f"Tray update failed: {e}")

    def _tray_notice(message, toast=True):
        """Windows balloon. Startup chatter (`toast=False`) stays silent."""
        try:
            if message and toast:
                icon.notify(message, APP_NAME)
        except Exception:
            pass

    engine.on("state", _tray_state)
    engine.on("notice", _tray_notice)
    threading.Thread(target=icon.run, daemon=True, name="tray").start()
    return icon
