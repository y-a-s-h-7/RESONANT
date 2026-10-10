"""Resonant — interactive Windows verification (`python resonant_cli.py verify`).

Bridges the two halves of the verification story:

  * docs/MANUAL_VERIFICATION.md is the human checklist. `parse_items` reads it
    into one item per checkbox, with its section and exact steps.
  * verification_results.json (gitignored) records pass / fail / skip per item
    together with the date and Windows version of the run. It never contains
    transcript text or audio — only statuses and short check details.

`run()` walks the automated checks first (safe, self-contained), then asks the
human about the remaining manual items one at a time. The automatable checks
are:

    * WH_KEYBOARD_LL installs and uninstalls cleanly (via hotkey.Hotkey)
    * clipboard set / read / restore round trip using the same
      sequence-number guard as injector.paste_text (never clobbers a copy made
      while the check runs)
    * Unicode SendInput of é, ’, — and an emoji into a temporary Tk entry
      window that verify creates and focuses itself — nothing is typed into
      the user's apps
    * the default microphone opens and delivers frames

Everything is injectable (`platform=`, `tk_factory=`, `type_fn=`) so tests use
fakes and never touch real hardware.
"""
import json
import os
import platform
import re
import sys
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_PATH = os.path.join(BASE_DIR, "docs", "MANUAL_VERIFICATION.md")
RESULTS_PATH = os.path.join(BASE_DIR, "verification_results.json")

PASS, FAIL, SKIP = "pass", "fail", "skip"
VALID_STATUSES = (PASS, FAIL, SKIP)

SECTION_RE = re.compile(r"^#{2,3}\s+(.*\S)\s*$")
CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.*)$")

UNICODE_SAMPLE = "é ’ — 🙂"


# --------------------------------------------------------------- result file
def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def windows_label():
    """'Windows 11 10.0.22631' (falls back to platform.platform())."""
    try:
        release, version, _csd, _ptype = platform.win32_ver()
        if release or version:
            return f"Windows {release} {version}".strip()
    except Exception:
        pass
    return platform.platform()


def _empty_results():
    return {"generated": _now(), "os": windows_label(),
            "python": platform.python_version(), "results": {}}


def load_results(path=RESULTS_PATH):
    """Read the result file; never raises. Missing/corrupt -> empty results."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return _empty_results()
        data.setdefault("results", {})
        return data
    except FileNotFoundError:
        return _empty_results()
    except Exception:
        return _empty_results()


def save_results(data, path=RESULTS_PATH):
    """Atomic write (temp + os.replace, retried for Windows AV locks)."""
    data["generated"] = _now()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        for _ in range(3):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.05)
        return False
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def set_result(data, item_id, status, detail=""):
    """Record one outcome. `detail` is a short check message, never a transcript."""
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    data.setdefault("results", {})[item_id] = {
        "status": status,
        "ts": _now(),
        "detail": str(detail)[:500],
    }
    return data


def summarize(data):
    """(passed, failed, skipped) over everything recorded so far."""
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for entry in (data.get("results") or {}).values():
        counts[entry.get("status")] = counts.get(entry.get("status"), 0) + 1
    return counts[PASS], counts[FAIL], counts[SKIP]


# ------------------------------------------------------------- item parser
def _slug(text, max_words=6, max_len=48):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    slug = "-".join(words[:max_words])[:max_len].strip("-")
    return slug or "item"


def parse_items(markdown_text):
    """One dict per `- [ ]` / `- [x]` line: section, text (continuations joined),
    checked, and a stable id derived from the item number and the first words."""
    items = []
    section = ""
    current = None
    for raw_line in (markdown_text or "").splitlines():
        heading = SECTION_RE.match(raw_line)
        if heading:
            section = heading.group(1).strip()
            current = None
            continue
        box = CHECKBOX_RE.match(raw_line)
        if box:
            current = {"section": section,
                       "text": box.group(2).strip(),
                       "checked": box.group(1).lower() == "x"}
            items.append(current)
            continue
        stripped = raw_line.strip()
        if current is not None and stripped and not stripped.startswith(("#", "|", "```")):
            current["text"] += " " + stripped          # wrapped checklist line
        elif not stripped:
            current = None
    for index, item in enumerate(items, 1):
        item["id"] = f"{index:02d}-{_slug(item['text'])}"
        item["kind"] = "manual"
    return items


def read_items(path=MANUAL_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return parse_items(fh.read())


# --------------------------------------------------------- automated checks
def check_hook_install(platform=None):
    """Install the low-level hook through the app's own code, then stop it."""
    import hotkey
    from platform_win import real_platform

    plat = platform or real_platform()
    hook = hotkey.Hotkey(key="f9", platform=plat)
    backend = hook.start()
    time.sleep(0.2)
    installed = backend == "hook"
    hook.stop()
    time.sleep(0.3)
    released = getattr(hook, "_hook", None) is None
    if not installed:
        return False, f"hook backend is '{backend}' (expected 'hook')"
    if not released:
        return False, "hook handle still registered after stop()"
    return True, "WH_KEYBOARD_LL installed and uninstalled cleanly"


def check_clipboard_roundtrip(platform=None):
    """Set a marker, read it back, restore the previous text — but only while
    the clipboard sequence number is unchanged (injector.paste_text's guard)."""
    from platform_win import real_platform

    plat = platform or real_platform()
    try:
        previous = plat.clipboard_get_text()
        before = plat.clipboard_sequence_number()
    except Exception as e:
        return False, f"clipboard unavailable ({e})"
    marker = f"Resonant verify {uuid.uuid4().hex[:8]}"
    if not plat.clipboard_set_text(marker, exclude_history=True):
        return False, "clipboard_set_text failed (another app may hold it)"
    ours = plat.clipboard_sequence_number()
    if plat.clipboard_get_text() != marker:
        return False, "read-back mismatch after set"
    if ours == before:
        return False, "clipboard sequence number did not advance after set"
    # Same guard as injector.paste_text: never clobber a newer copy.
    if plat.clipboard_sequence_number() != ours:
        return True, "clipboard changed during the check; guarded restore skipped"
    expected = previous if previous is not None else ""
    plat.clipboard_set_text(expected, exclude_history=True)
    if plat.clipboard_get_text() != expected:
        return False, "previous clipboard text was not restored"
    return True, "set / read / restore round trip ok (sequence-number guarded)"


def _focus_tk_window(root, entry):
    """Bring the temporary Tk window to the foreground and verify it.

    Windows only lets the foreground process take focus, so a plain
    focus_force() from a console-launched process is ignored and SendInput
    would type into whatever window the user has focused. AttachThreadInput is
    the standard workaround; the caller refuses to send keys unless this
    returns True.
    """
    import ctypes
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()  # GA_ROOT
        user32.ShowWindow(hwnd, 5)               # SW_SHOW
        user32.BringWindowToTop(hwnd)
        root.update()
        foreground = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(foreground, None)
        our_thread = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(our_thread, fg_thread, True)
        try:
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            entry.focus_force()
            root.update()
        finally:
            user32.AttachThreadInput(our_thread, fg_thread, False)
        time.sleep(0.15)
        root.update()
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def check_unicode_sendinput(platform=None, tk_factory=None, type_fn=None):
    """Type é, ’, — and an emoji with Unicode SendInput into a Tk window that
    this check creates itself, so nothing lands in the user's apps."""
    import tkinter as tk

    import injector
    from platform_win import real_platform

    root = (tk_factory or tk.Tk)()
    try:
        root.title("Resonant verify — Unicode typing check")
        root.geometry("440x110")
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        entry = tk.Entry(root, width=44)
        entry.pack(padx=12, pady=24)
        root.update()
        if not _focus_tk_window(root, entry):
            return False, ("could not bring the temporary window to the foreground "
                           "— no keys were sent; run verify on an interactive "
                           "desktop")
        plat = platform or real_platform()
        send = type_fn or (lambda text: injector.type_text(
            plat, text, append_trailing_space=False))
        try:
            send(UNICODE_SAMPLE)
        except Exception as e:
            return False, f"SendInput failed ({e})"
        time.sleep(0.35)
        root.update()
        got = entry.get()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    if got == UNICODE_SAMPLE:
        return True, f"typed {UNICODE_SAMPLE!r} intact into a temporary Tk entry"
    return False, f"expected {UNICODE_SAMPLE!r}, got {got!r}"


def check_microphone(sample_rate=16000):
    """Open the default input device and pull a short block of frames."""
    try:
        import sounddevice as sd
    except ImportError as e:
        return False, f"sounddevice unavailable ({e})"
    try:
        with sd.InputStream(samplerate=sample_rate, channels=1,
                            dtype="float32") as stream:
            frames, _overflowed = stream.read(int(sample_rate * 0.15))
            name = sd.query_devices(kind="input").get("name", "default input")
        if frames is None or len(frames) == 0:
            return False, "default input opened but delivered no frames"
        return True, f"default input opened and delivered frames ({name!r})"
    except Exception as e:
        return False, f"could not open the default input device ({e})"


AUTOMATED_CHECKS = [
    {"id": "auto-hook-install", "name": "Keyboard hook installs and uninstalls",
     "fn": check_hook_install},
    {"id": "auto-clipboard-roundtrip",
     "name": "Clipboard set/read/restore (sequence-number guarded)",
     "fn": check_clipboard_roundtrip},
    {"id": "auto-unicode-sendinput",
     "name": "SendInput types é ’ — and an emoji into a temporary window",
     "fn": check_unicode_sendinput},
    {"id": "auto-microphone", "name": "Default microphone opens",
     "fn": check_microphone},
]


def run_automated(data, path=RESULTS_PATH, only_missing=True, checks=None,
                  out=print):
    """Run the automated checks, record them, save. Returns [(check, ok, detail)]."""
    results = data.setdefault("results", {})
    ran = []
    for check in (checks if checks is not None else AUTOMATED_CHECKS):
        if only_missing and results.get(check["id"], {}).get("status") == PASS:
            out(f"  [skip] {check['name']} — already passed")
            continue
        out(f"  [run ] {check['name']} …")
        try:
            ok, detail = check["fn"]()
        except Exception as e:
            ok, detail = False, f"check crashed ({e})"
        set_result(data, check["id"], PASS if ok else FAIL, detail)
        ran.append((check, bool(ok), detail))
        out(f"  [{'pass' if ok else 'fail'}] {check['name']} — {detail}")
    save_results(data, path)
    return ran


# ------------------------------------------------------------- manual walk
def _ask_choice(ask, out):
    prompt = "    pass / fail / skip / quit [p/f/s/q]: "
    while True:
        try:
            answer = ask(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"
        if answer in ("p", "pass"):
            return PASS
        if answer in ("f", "fail"):
            return FAIL
        if answer in ("s", "skip", ""):
            return SKIP
        if answer in ("q", "quit"):
            return "q"
        out("    please answer p, f, s or q")


def run_manual(data, items, path=RESULTS_PATH, ask=input, out=print,
               only_missing=True):
    """One item at a time. Returns True when the whole list was walked."""
    results = data.setdefault("results", {})
    total = len(items)
    for index, item in enumerate(items, 1):
        if only_missing and results.get(item["id"], {}).get("status") == PASS:
            out(f"[{index}/{total}] already passed — {item['text'][:60]}…")
            continue
        out("")
        out(f"[{index}/{total}] {item['section']}")
        out(f"    {item['text']}")
        choice = _ask_choice(ask, out)
        if choice == "q":
            out("  stopped — progress saved")
            save_results(data, path)
            return False
        set_result(data, item["id"], choice)
        save_results(data, path)
    return True


# -------------------------------------------------------------------- entry
def run(args, out=print, ask=input):
    # The checklist contains typographic characters (→, —, é). A cp1252
    # console would raise UnicodeEncodeError mid-walkthrough; degrade the
    # glyphs instead of the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if sys.platform != "win32":
        out("verify is a Windows-only walkthrough: it exercises the real "
            "keyboard hook, clipboard and SendInput.")
        return 1
    path = getattr(args, "results", None) or RESULTS_PATH
    try:
        items = read_items()
    except Exception as e:
        out(f"could not read {MANUAL_PATH}: {e}")
        return 1

    if getattr(args, "list", False):
        for item in items:
            out(f"{item['id']:<52} [{item['section']}] {item['text']}")
        return 0

    data = load_results(path)
    data["os"] = windows_label()
    data["python"] = platform.python_version()
    only_missing = not getattr(args, "all", False)

    automated_only = (getattr(args, "auto", False)
                      or getattr(args, "checks_only", False))
    out("== Automated checks (safe; no typing into your apps) ==")
    run_automated(data, path, only_missing=only_missing, out=out)
    if not automated_only:
        out("\n== Manual walkthrough ==")
        run_manual(data, items, path, ask=ask, out=out,
                   only_missing=only_missing)

    save_results(data, path)
    passed, failed, skipped = summarize(data)
    out(f"\nSaved: {path}")
    out(f"Recorded: {passed} passed, {failed} failed, {skipped} skipped.")
    return 0
