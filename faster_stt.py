"""Faster-whisper backend (Phase 8) behind the Transcriber interface.

Lives in its own module so the dev/test venv (which never installs
`faster-whisper` or torch) can import this file without pulling in the heavy
stack — the real package is imported lazily inside `load_model` only when the
user has actually switched to the faster backend (config.py / settings.py
default `asr_backend` is "default" = openai-whisper, so a dev launch never
touches faster-whisper).

Public contract (matches the whisper backend so the engine / transcriber need
no per-backend branching):
    load_model(target) -> object with .transcribe(audio, **kwargs)
    the returned adapter's .transcribe() returns {"text": str, "segments": [dicts]}
    where each dict has text / start / end / no_speech_prob / avg_logprob /
    compression_ratio — exactly what transcriber.filter_segments / gate expect.

The adapter forwards kwargs (language, initial_prompt, fp16, temperature,
condition_on_previous_text, vad_filter, ...) straight through to the underlying
faster_whisper.Model.transcribe(), and converts the (segments, info) tuple it
returns into the dict shape the rest of the app already consumes.
"""

from __future__ import annotations

# NOTE: nothing heavy imported here — faster_whisper is imported lazily inside
# load_model so a dev launch that stays on the default whisper backend never
# touches it.


class FasterModel:
    """Thin adapter over a faster_whisper.Model that speaks the whisper shape."""

    def __init__(self, model):
        self._model = model

    def transcribe(self, audio, **kwargs):
        result = self._model.transcribe(audio, **kwargs)
        # faster_whisper.Model.transcribe() returns a 2-tuple
        #   (generator-over-Segments, TranscriptionInfo)
        if isinstance(result, tuple) and len(result) == 2 and not hasattr(result[0], "text"):
            segments = result[0]
        else:
            segments = result
        kept = []
        parts = []
        for seg in segments:
            d = {
                "text": seg.text or "",
                "start": float(getattr(seg, "start", 0.0)),
                "end": float(getattr(seg, "end", 0.0)),
                "no_speech_prob": float(getattr(seg, "no_speech_prob", 0.0)),
                "avg_logprob": float(getattr(seg, "avg_logprob", 0.0)),
                "compression_ratio": float(getattr(seg, "compression_ratio", 1.0)),
            }
            kept.append(d)
            if d["text"].strip():
                parts.append(d["text"].strip())
        return {"text": " ".join(parts).strip(), "segments": kept}


def load_model(target):
    """Load a faster-whisper model and wrap it so transcribe() returns dict shape.

    `target` is the model identifier forwarded to faster_whisper.load_model
    (e.g. "base.en", "small.en", or a local CTranslate2 model path).  The
    whisper-side conventions (`.en` suffixed distilled models, etc.) are the
    same ones the user already knows from the default backend.
    """
    import faster_whisper  # lazy — only touched when the user switches backend

    model = faster_whisper.load_model(target)
    return FasterModel(model)
