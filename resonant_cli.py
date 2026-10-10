"""Resonant CLI — headless harness (W0.4).

    python resonant_cli.py clean "text to clean up"   # editor pipeline, no mic
    python resonant_cli.py transcribe file.wav        # ASR + editor, no typing
    python resonant_cli.py doctor                     # environment health report
    python resonant_cli.py verify                     # Windows verification walkthrough

Safe to run while the app is running: `clean`, `transcribe` and `doctor` never
touch the instance lock, hotkey, registry, clipboard or microphone — they only
talk to localhost Ollama and run local STT. `verify` is the deliberate
exception: it exercises the real keyboard hook, clipboard, SendInput (into a
window it creates itself) and microphone, and is meant to be run on a Windows
desktop.
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


def _load_stt_model(name, backend=None):
    """Load the configured ASR backend model, or exit cleanly if absent.

    The default backend is openai-whisper (PyPI `openai-whisper`, imports as
    `whisper`, pulls in torch). The optional faster backend (`faster_stt`,
    CTranslate2-based) is loaded when `backend="faster"`. The light dev venv
    omits both (brief §1 rule 5), so an unguarded import here surfaces as a
    raw ModuleNotFoundError traceback.
    """
    if backend is None:
        import settings as settings_mod
        backend = settings_mod.load_settings().get("asr_backend", "default")
    if backend == "faster":
        try:
            import faster_stt
        except ImportError as e:
            print("Faster-whisper backend not installed — run: pip install -r "
                  "requirements-extras.txt (installs faster-whisper).",
                  file=sys.stderr)
            print(f"  detail: {e}", file=sys.stderr)
            raise SystemExit(1) from None
        return faster_stt.load_model(name)
    try:
        import whisper
    except ImportError as e:
        print("ASR package not installed — run: pip install -r requirements.txt "
              "(installs openai-whisper and torch).", file=sys.stderr)
        print(f"  detail: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    return whisper.load_model(name)


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
    from config import LANGUAGE, SAMPLE_RATE

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

    s = _settings()
    model = _load_stt_model(s["stt_model"], backend=s.get("asr_backend", "default"))
    audio = recorder.trim_silence(audio, SAMPLE_RATE)
    stats = transcriber.compute_stats(audio, SAMPLE_RATE)
    result = transcriber.transcribe(model, audio,
                                    language=LANGUAGE,
                                    initial_prompt=None,
                                    fp16=s.get("stt_fp16", "auto"))
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


def cmd_verify(args):
    import verification
    return verification.run(args)


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

    # ASR backend guard: openai-whisper imports as `whisper` and pulls in torch.
    # Report a clear message here instead of letting a later load traceback.
    try:
        import whisper
        report["asr_package"] = (
            f"installed ({getattr(whisper, '__version__', 'version unknown')})")
    except ImportError:
        report["asr_package"] = ("ASR package not installed — run: "
                                 "pip install -r requirements.txt")
    except Exception as e:
        report["asr_package"] = f"installed but failed to import ({e})"

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

    cache = os.path.join(os.path.expanduser("~"), ".cache", "whisper")
    report["stt_cache"] = os.listdir(cache) if os.path.isdir(cache) else "empty"
    report["stt_model"] = s.get("stt_model", "")
    report["stt_fp16"] = s.get("stt_fp16", "auto")
    try:
        import transcriber
        report["stt_fp16_active"] = transcriber._use_fp16(
            s.get("stt_fp16", "auto"))
    except Exception as e:
        report["stt_fp16_active"] = f"unknown ({e})"
    report["stt_model_cached"] = any(
        s.get("stt_model", "").split(".")[0] in name
        for name in (report["stt_cache"] if isinstance(report["stt_cache"], list) else []))

    report["hotkey"] = (s["hotkey"] if hotkey.validate_key(s["hotkey"])
                        else f"INVALID ('{s['hotkey']}' falls back to f9)")
    report["hotkey_mode"] = s["hotkey_mode"]
    report["swallow_hotkey"] = s["swallow_hotkey"]
    report["ai_cleanup_mode"] = s["ai_cleanup_mode"]
    try:
        import editor
        # In-memory and per-process: a fresh doctor run starts at zero.
        report["smart_decisions"] = editor.decision_counts()
    except Exception as e:
        report["smart_decisions"] = f"unknown ({e})"
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
    p = sub.add_parser("verify", help="interactive Windows verification walkthrough")
    p.add_argument("--list", action="store_true",
                   help="list the checklist items and exit")
    p.add_argument("--auto", action="store_true",
                   help="run only the automated checks (no manual prompts)")
    p.add_argument("--checks-only", action="store_true",
                   help="alias for --auto")
    p.add_argument("--all", action="store_true",
                   help="re-run items/checks already marked pass")
    p.add_argument("--results", default=None,
                   help="result file (default: verification_results.json)")
    args = ap.parse_args()
    return {"clean": cmd_clean, "transcribe": cmd_transcribe,
            "doctor": cmd_doctor, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
