"""Resonant CLI — headless harness (W0.4).

    python resonant_cli.py clean "text to clean up"   # editor pipeline, no mic
    python resonant_cli.py transcribe file.wav        # ASR + editor, no typing
    python resonant_cli.py doctor                     # environment health report

Safe to run while the app is running: these commands never touch the instance
lock, hotkey, registry, clipboard or microphone. `clean`/`transcribe` only talk
to localhost Ollama and run local STT.
"""
import argparse
import json
import os
import sys

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)


def _settings():
    import settings as settings_mod
    return settings_mod.load_settings()


def cmd_clean(args):
    import editor
    s = _settings()
    final, status = editor.clean(
        args.text,
        model=s["ollama_model"],
        mode=s["ai_cleanup_mode"],
        min_words=s["smart_min_words"],
        timeout_s=s["ollama_timeout_s"],
        keep_alive=s["ollama_keep_alive"],
    )
    print(f"status: {status}")
    print(final)
    return 0


def cmd_transcribe(args):
    import wave

    import numpy as np

    import editor
    import recorder
    import transcriber
    from config import SAMPLE_RATE

    with wave.open(args.wav, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    if rate != SAMPLE_RATE:
        print(f"note: file is {rate} Hz; STT wants {SAMPLE_RATE} — "
              f"resampling is not applied by this command")

    import stt
    s = _settings()
    model = stt.load_model(s["stt_model"])
    audio = recorder.trim_silence(audio, SAMPLE_RATE)
    stats = transcriber.compute_stats(audio, SAMPLE_RATE)
    result = transcriber.transcribe(model, audio)
    raw = (result.get("text") or "").strip()
    gated, reason = transcriber.gate(raw, stats, result.get("segments") or [],
                                    min_peak=s["min_peak"])
    print(f"heard:  {raw}")
    if reason:
        print(f"gate:   {reason} (nothing would be typed)")
        return 0
    final, status = editor.clean(
        gated, model=s["ollama_model"], mode=s["ai_cleanup_mode"],
        min_words=s["smart_min_words"], timeout_s=s["ollama_timeout_s"],
        keep_alive=s["ollama_keep_alive"])
    print(f"clean:  {final}   [{status}]")
    return 0


def cmd_doctor(args):
    import hotkey
    import settings as settings_mod
    import startup

    report = {"python": sys.version.split()[0], "platform": sys.platform}

    missing = []
    for mod in ("numpy", "sounddevice", "requests", "pystray", "PIL"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    report["python_deps_missing"] = missing or "none"

    try:
        import sounddevice as sd
        devices = sd.query_devices()
        inputs = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
        report["mic_inputs"] = inputs or "NONE FOUND"
    except Exception as e:
        report["mic_inputs"] = f"unavailable ({e})"

    s = _settings()
    try:
        import requests
        resp = requests.get("http://localhost:11434/api/tags", timeout=3)
        models = [m["name"] for m in resp.json().get("models", [])]
        report["ollama"] = "reachable"
        report["ollama_models"] = models
        report["ollama_model_present"] = any(
            m.startswith(s["ollama_model"]) for m in models)
    except Exception as e:
        report["ollama"] = f"NOT reachable ({e})"

    cache = os.path.join(os.path.expanduser("~"), ".cache", "stt")
    report["stt_cache"] = os.listdir(cache) if os.path.isdir(cache) else "empty"
    report["stt_model"] = s.get("stt_model", "")
    report["stt_model_cached"] = any(
        s.get("stt_model", "").split(".")[0] in name
        for name in (report["stt_cache"] if isinstance(report["stt_cache"], list) else []))

    report["hotkey"] = (s["hotkey"] if hotkey.validate_key(s["hotkey"])
                        else f"INVALID ('{s['hotkey']}' falls back to f9)")
    report["hotkey_mode"] = s["hotkey_mode"]
    report["swallow_hotkey"] = s["swallow_hotkey"]
    report["ai_cleanup_mode"] = s["ai_cleanup_mode"]
    report["inject_mode"] = s["inject_mode"]
    report["log_transcripts"] = s["log_transcripts"]

    try:
        report["startup_entry"] = ("registered"
                                   if startup.is_enabled() else "not registered")
    except Exception as e:
        report["startup_entry"] = f"unknown ({e})"

    try:
        defaults = _settings()
        report["settings"] = f"valid ({len(defaults)} keys)"
        report["settings_notices"] = settings_mod.LAST_NOTICES or "none"
    except Exception as e:
        report["settings"] = f"ERROR: {e}"

    print(json.dumps(report, indent=2, default=str))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="resonant_cli", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("clean", help="run the editor pipeline on text")
    p.add_argument("text")
    p = sub.add_parser("transcribe", help="ASR + editor on a wav file")
    p.add_argument("wav")
    sub.add_parser("doctor", help="environment health report")
    args = ap.parse_args()
    return {"clean": cmd_clean, "transcribe": cmd_transcribe,
            "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
