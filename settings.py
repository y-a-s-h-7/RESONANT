"""Resonant — settings schema, validation, migration, atomic persistence (W4.4).

Why this module exists: `save_settings` used to write *all* defaults, so editing
`config.py` defaults (which the README tells the owner to do) was silently
ignored on the next launch. Now only the values that actually differ from the
defaults are written, which makes `config.py` authoritative for everything the
user has not explicitly changed.

Public API:
    load_settings(path=None)   -> dict (defaults + valid overrides)
    save_settings(s, path=None) -> persists ONLY the overrides, atomically
    validate(key, value)       -> (ok, coerced_value, reason)
    default_for(key)           -> the default

Anything invalid falls back to the default and appends a human-readable notice
to `LAST_NOTICES` so the caller can surface it (brief §3: "bad values fall back
to default with a notice").
"""
import json
import os

from config import (
    DEFAULT_SETTINGS,
    SETTINGS_PATH,
    SILENCE_AUTOSTOP_MAX_MS,
    SILENCE_AUTOSTOP_MIN_MS,
    SILENCE_OFF,
)

LAST_NOTICES = []

# Legacy defaults used by the pre-refactor flat settings.json. Migration keeps
# only values that were an ACTUAL override of these (brief W4.4).
OLD_DEFAULTS = {
    "hotkey": "f9",
    "stt_model": "base.en",
    "ollama_model": "llama3.2",
    "ai_cleanup": True,
    "sound_feedback": True,
    "start_listening_on_launch": True,
    "run_at_startup": True,
    "paste_mode": True,
}

LEGACY_KEY_MAP = {
    # legacy key -> (new key, {legacy value: new value})
    "ai_cleanup": ("ai_cleanup_mode", {True: "always", False: "off"}),
    "paste_mode": ("inject_mode", {True: "paste", False: "type"}),
}

ENUMS = {
    "ai_cleanup_mode": ("smart", "always", "off"),
    "inject_mode": ("auto", "type", "paste"),
    "hotkey_mode": ("hold", "toggle"),
    "asr_backend": ("default", "faster"),
    # Ctrl+Shift+Z is redo almost everywhere; the offered chords are the
    # Scroll Lock/Pause combos that apps rarely bind, and "off" is default.
    "undo_chord": ("off", "ctrl+alt+scroll_lock", "ctrl+alt+pause"),
}


# ____________________________________________________________ ENUM branch guards

ENUM_KEYS = frozenset(ENUMS)

def default_for(key):
    return DEFAULT_SETTINGS.get(key)


def validate(key, value):
    """Return (ok, coerced_value, reason). Unknown keys are ignored upstream."""
    if key not in DEFAULT_SETTINGS:
        return False, None, f"unknown setting '{key}'"
    default = DEFAULT_SETTINGS[key]

    if key in ENUMS:
        if isinstance(value, str) and value in ENUMS[key]:
            return True, value, None
        return False, default, f"'{key}' must be one of {', '.join(ENUMS[key])}"

    if key == "hotkey":
        from hotkey import validate_key
        canonical = validate_key(value) if isinstance(value, str) else None
        if canonical:
            return True, canonical, None
        return False, default, f"unsupported hotkey '{value}' — using {default}"

    if key == "stt_model":
        if isinstance(value, str) and value.strip():
            return True, value.strip(), None
        return False, default, "stt_model must be a non-empty string"

    if key == "stt_fp16":
        # "auto" = fp16 only on a CUDA GPU; True/False force the choice.
        if value is True or value is False or value == "auto":
            return True, value, None
        return False, default, "stt_fp16 must be 'auto', true or false"

    if key == "silence_autostop_ms":
        # 0 (off) or a window long enough to be deliberate: a value below
        # 500 ms would end a dictation during an ordinary breath.
        reason = "silence_autostop_ms must be 0 or 500-5000 ms"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, default, reason
        ms = int(value)
        if ms == SILENCE_OFF or SILENCE_AUTOSTOP_MIN_MS <= ms <= SILENCE_AUTOSTOP_MAX_MS:
            return True, ms, None
        return False, default, reason

    if key == "ollama_model":
        if isinstance(value, str) and value.strip():
            return True, value.strip(), None
        return False, default, "ollama_model must be a non-empty string"

    if key == "input_device":
        if value is None or (isinstance(value, int) and value >= 0):
            return True, value, None
        return False, default, "input_device must be a device index or null"

    if key == "custom_vocabulary":
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return True, value, None
        return False, default, "custom_vocabulary must be a list of strings"

    if key == "replacements":
        if isinstance(value, dict) and all(isinstance(v, str) for v in value.values()):
            return True, value, None
        return False, default, "replacements must be a string->string mapping"

    if isinstance(default, bool):
        if isinstance(value, bool):
            return True, value, None
        return False, default, f"'{key}' must be true or false"

    if key == "faster_vad":
        # merged from ENUMS above when it was a plain tuple; kept explicit for
        # clarity since it is a new setting.
        if isinstance(value, bool):
            return True, value, None
        return False, DEFAULT_SETTINGS[key], "'faster_vad' must be true or false"

    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, default, f"'{key}' must be a number"
        num = int(value)
        if num < 0 or num > 100_000:
            return False, default, f"'{key}' is out of range"
        return True, num, None

    if isinstance(default, str):
        if isinstance(value, str):
            return True, value, None
        return False, default, f"'{key}' must be text"

    if default is None:
        return True, value, None

    return False, default, f"'{key}' has an unsupported type"


def _apply(key, value, out, notices):
    ok, coerced, reason = validate(key, value)
    if ok:
        out[key] = coerced
    elif reason:
        notices.append(reason)


def _migrate_legacy(saved, notices):
    """Turn a pre-refactor full-dump settings.json into overrides only."""
    out = {}
    for key, value in saved.items():
        if key in LEGACY_KEY_MAP:
            new_key, mapping = LEGACY_KEY_MAP[key]
            if value != OLD_DEFAULTS.get(key):        # a real override
                if value in mapping:
                    _apply(new_key, mapping[value], out, notices)
            continue
        if key in OLD_DEFAULTS:
            if value != OLD_DEFAULTS[key]:            # override of an old default
                _apply(key, value, out, notices)
            continue
        _apply(key, value, out, notices)
    return out


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        LAST_NOTICES.append(f"settings.json could not be read ({e}) — using defaults")
        return {}


def _backup_once(path):
    """Keep the pre-migration file as settings.json.bak (idempotent)."""
    bak = path + ".bak"
    if os.path.exists(bak) or not os.path.exists(path):
        return
    try:
        with open(path, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
    except Exception:
        pass


def load_settings(path=None):
    """Defaults merged with the user's valid overrides. Never raises."""
    del LAST_NOTICES[:]
    path = path or SETTINGS_PATH
    saved = _read(path)
    is_legacy = any(k in saved for k in LEGACY_KEY_MAP) or (
        any(k in OLD_DEFAULTS for k in saved)
        and not any(k in DEFAULT_SETTINGS for k in saved if k not in OLD_DEFAULTS)
    )
    if is_legacy:
        _backup_once(path)
        overrides = _migrate_legacy(saved, LAST_NOTICES)
    else:
        overrides = {}
        for key, value in saved.items():
            if key in DEFAULT_SETTINGS:
                _apply(key, value, overrides, LAST_NOTICES)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(overrides)
    return merged


def save_settings(s, path=None):
    """Persist ONLY the overrides, atomically (temp file + os.replace)."""
    path = path or SETTINGS_PATH
    overrides = {}
    for key, default in DEFAULT_SETTINGS.items():
        if key in s and s[key] != default:
            overrides[key] = s[key]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2, sort_keys=True)
        for _ in range(3):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                import time
                time.sleep(0.05)
        return False
    except Exception as e:
        LAST_NOTICES.append(f"Settings could not be saved ({e})")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
