"""Resonant — Windows "Run at startup" registration (W4.2).

Centralises logic that used to be duplicated in three places (resonant.py,
resonant_ui.py's tray toggle and startup_command).

Key behaviours:
  * `startup_command()` appends `--hidden`, so the dashboard does NOT pop up at
    login (the flag was documented but never passed before).
  * `sync(settings)` is called ONCE after `engine.start()` (and on a user
    toggle), never on every state emit — the old code rewrote the registry
    ≥4× per dictation from the hotkey thread.
  * Never re-adds an entry the user removed: `apply_user_choice()` is the only
    path that turns it back on.
  * One-time migration of the previous "VoiceType" entry.
"""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "Resonant"
LEGACY_NAME = "VoiceType"


def _platform(platform):
    if platform is not None:
        return platform
    from platform_win import real_platform
    return real_platform()


def startup_command(hidden=True):
    """Quoted pythonw + script path (+ --hidden)."""
    exe = sys.executable
    flag = " --hidden" if hidden else ""
    if getattr(sys, "frozen", False):
        return f'"{exe}"{flag}'
    if os.path.basename(exe).lower().startswith("python"):
        candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(candidate):
            exe = candidate
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resonant.py")
    return f'"{exe}" "{script}"{flag}'


def is_enabled(platform=None):
    try:
        return _platform(platform).registry_get(RUN_KEY, STARTUP_NAME) is not None
    except Exception:
        return False


def enable(platform=None):
    try:
        _platform(platform).registry_set(RUN_KEY, STARTUP_NAME, startup_command())
        return True
    except Exception:
        return False


def disable(platform=None):
    try:
        _platform(platform).registry_delete(RUN_KEY, STARTUP_NAME)
        return True
    except Exception:
        return False


def migrate_legacy(platform=None):
    """Remove the old VoiceType entry so the app never runs twice at login."""
    try:
        return bool(_platform(platform).registry_delete(RUN_KEY, LEGACY_NAME))
    except Exception:
        return False


def sync(settings, platform=None):
    """Make the registry match settings['run_at_startup'] once. Returns bool."""
    want = bool(settings.get("run_at_startup"))
    if want:
        return enable(platform)
    return disable(platform)


def apply_user_choice(settings, enabled, platform=None):
    """User explicitly toggled the opt-in: persist the choice and the registry."""
    settings["run_at_startup"] = bool(enabled)
    settings["startup_prompted"] = True
    return sync(settings, platform)
