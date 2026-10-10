"""Resonant — transcription + the never-type-hallucinations gate (W1.4, W2.5).

Pure, testable gate functions; the STT model object is injected, so tests
never import heavy ML frameworks.

Gate rules (brief W1.4):
  * peak/RMS computed BEFORE transcribing; below `min_peak` -> drop early.
  * STT segments marked silent (no_speech_prob > 0.6 and avg_logprob < -1.0)
    or looping (compression_ratio > 2.4) are dropped.
  * Sound tags as the whole output -> drop; inline -> stripped.
  * Known phantom phrases ("Thank you.") are dropped ONLY with corroborating
    evidence (high no_speech_prob, or very short + quiet audio). "Thank you."
    is a legitimate dictation at normal volume.
  * The start-blip window is excluded from gating math only (never from audio).
"""
import re

# ------------------------------------------------------------ constants
MIN_PEAK_DEFAULT = 0.01
MIN_RMS_DEFAULT = 0.004
NO_SPEECH_DROP = 0.6
AVG_LOGPROB_DROP = -1.0
COMPRESSION_LOOP = 2.4
PHANTOM_CORROBORATION_NO_SPEECH = 0.3
PHANTOM_SHORT_S = 2.0

SOUND_TAG_RE = re.compile(
    r"(\[(?:BLANK_AUDIO|Music|Applause|Laughter|Silence|noise|inaudible)\]|"
    r"\((?:silence|music|noise)\))", re.IGNORECASE)

PHANTOM_PHRASES = {"thank you", "thanks", "thank you.", "thanks for watching",
                   "you", "bye", "bye."}


# ------------------------------------------------------------ audio stats
class AudioStats:
    def __init__(self, peak, rms, duration_s):
        self.peak = peak
        self.rms = rms
        self.duration_s = duration_s


def compute_stats(audio, sample_rate=16000):
    """peak / rms / duration over the audio EXCLUDING the blip window head."""
    import numpy as np
    duration = len(audio) / sample_rate
    window = audio
    if duration > 0.2:                     # exclude start-cue blip window head
        blip = int(0.09 * sample_rate)     # blip is ~90 ms, played on output —
        window = audio[blip:]              # mic may bleed it in; gate math only
    if len(window) == 0:
        window = audio
    peak = float(np.abs(window).max()) if len(window) else 0.0
    rms = float(np.sqrt(np.mean(window ** 2))) if len(window) else 0.0
    return AudioStats(peak, rms, duration)


# ------------------------------------------------------------ segment filter
def filter_segments(segments):
    """Drop segments marked silent or looping. Returns kept list."""
    kept = []
    for seg in segments or []:
        nsp = seg.get("no_speech_prob", 0.0)
        alp = seg.get("avg_logprob", 0.0)
        cr = seg.get("compression_ratio", 1.0)
        if nsp > NO_SPEECH_DROP and alp < AVG_LOGPROB_DROP:
            continue
        if cr > COMPRESSION_LOOP:
            continue
        kept.append(seg)
    return kept


def text_from_segments(segments, fallback_text):
    segs = filter_segments(segments)
    if not segs:
        return ""
    return " ".join(s.get("text", "").strip() for s in segs).strip() or fallback_text


# ------------------------------------------------------------ tags & phantoms
def strip_sound_tags(text):
    """Returns (text, only_tags). Inline tags stripped; whole-output tags signal drop."""
    only = bool(text) and SOUND_TAG_RE.sub("", text).strip() == ""
    return SOUND_TAG_RE.sub("", text).strip(), only


def normalise(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def is_phantom(text, stats, segments, min_peak=MIN_PEAK_DEFAULT,
               min_rms=MIN_RMS_DEFAULT):
    """True only when a known phantom phrase has corroborating evidence."""
    stripped, only_tags = strip_sound_tags(text)
    if only_tags:
        return True
    norm = normalise(stripped)
    if norm not in PHANTOM_PHRASES:
        return False
    max_nsp = max((s.get("no_speech_prob", 0.0) for s in segments or []), default=0.0)
    corroborated = (
        max_nsp > PHANTOM_CORROBORATION_NO_SPEECH
        or (stats.duration_s < PHANTOM_SHORT_S and stats.rms < min_rms)
        or stats.peak < min_peak
    )
    return corroborated


# ------------------------------------------------------------ gate pipeline
def gate(raw_text, stats, segments, min_peak=MIN_PEAK_DEFAULT,
         min_rms=MIN_RMS_DEFAULT):
    """Full gate. Returns (text_or_empty, reason_or_None). Never raises."""
    if stats.peak < min_peak:
        return "", "No speech (too quiet)."
    text, only_tags = strip_sound_tags(raw_text or "")
    if only_tags or not text:
        return "", "No speech detected (silence or sound tags)."
    if is_phantom(text, stats, segments, min_peak, min_rms):
        return "", "No speech detected (phantom transcript)."
    return text, None


# ------------------------------------------------------------ precision mode
def _use_fp16(mode="auto"):
    """Resolve the `stt_fp16` setting to a concrete half-precision flag.

    Half-precision only helps on a CUDA GPU — there it roughly halves
    transcription time, while on CPU torch refuses fp16 outright. `mode` is the
    setting: "auto" and True enable fp16 only when torch reports an available
    CUDA device; False pins the fp32 CPU path even on a flaky GPU where
    auto-detection would enable fp16. `torch` is imported lazily: importing
    transcriber — and therefore the engine/CLI — must never pull in the
    multi-GB stack, which the light dev venv does not install. Any failure
    (torch absent, broken CUDA, no device) means "no fp16".
    """
    if mode is False:
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


# ------------------------------------------------------------ STT transcription call
def transcribe(model, audio, language="en", initial_prompt=None, fp16="auto",
                  **kwargs):
    """Greedy decode: OMITTING beam_size/best_of gives GreedyDecoder with
    temperature=0 (beam_size=1 would select BeamSearchDecoder instead).

    `fp16` is the `stt_fp16` setting ("auto" | True | False); `_use_fp16`
    resolves it, so GPU users get half-precision while CPU users stay on fp32.

    Extra kwargs (e.g. `vad_filter` for the faster-whisper backend) are
    forwarded to the model unchanged — the caller is responsible for passing
    only kwargs the model accepts (brief Phase 8: pass vad_filter and
    initial_prompt through to the faster-whisper model)."""
    resolved = {"fp16": _use_fp16(fp16), "temperature": 0,
                "condition_on_previous_text": False}
    if language:
        resolved["language"] = language
    if initial_prompt:
        resolved["initial_prompt"] = initial_prompt
    for k, v in kwargs.items():
        if k not in resolved:
            resolved[k] = v
    return model.transcribe(audio, **resolved)
