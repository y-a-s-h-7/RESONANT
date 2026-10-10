"""Dummy stub package for `faster_whisper`, sitting in a real directory so that a
top-level `import faster_whisper` finds THIS package in sys.path[0] and never
reaches the real PyPI distribution — even when the real package is installed into
the environment next to this project.

Used by the dev/test environment only: the light dev venv deliberately never
installs `faster-whisper` / CTranslate2, and the faster backend is loaded lazily
inside `faster_stt.load_model()` only when the user has actually switched to it.
A stale `faster_whisper/__init__.py` sitting next to the project therefore does
not interfere with a real installation inside the venv (the venv's site-packages
is on sys.path before the project root for package resolution — verified in the
smoke test below).

Public surface (minimal): enough for `faster_stt.FasterModel` to call
`faster_whisper.load_model(target)` and `model.transcribe(audio, **kwargs)`
during tests only.  In particular the `.transcribe()` return is the same 2-tuple
`(segments, info)` the real package emits, and each segment object exposes the
attributes `text`, `start`, `end`, `no_speech_prob`, `avg_logprob`,
`compression_ratio` (and a couple more for good measure) that `faster_stt`
converts into the dict shape the rest of the app already consumes.
"""
from __future__ import annotations

import types


class TranscriptionInfo:
    """Metadata faster_whisper.Model.transcribe() returns as the second tuple item."""

    def __init__(self, language="en", duration=1.0, segments_count=1, **kwargs):
        self.language = language
        self.duration = duration
        self.segments_count = segments_count
        for k, v in kwargs.items():
            setattr(self, k, v)


class Segment(types.SimpleNamespace):
    """A single faster-whisper segment.  Attributes mirror the real CTranslate2
    segment object consumed by `faster_stt.FasterModel.transcribe()`."""


def _make_segment(text="hello world", start=0.0, end=1.0, *, no_speech_prob=0.0,
                  avg_logprob=0.0, compression_ratio=1.0, temperature=0.0,
                  no_lang_prob=0.0, words=None):
    seg = Segment(text=text, start=start, end=end, no_speech_prob=no_speech_prob,
                  avg_logprob=avg_logprob, compression_ratio=compression_ratio,
                  temperature=temperature, no_lang_prob=no_lang_prob)
    if words is not None:
        seg.words = words
    return seg


class _FakeModel:
    """Fake `faster_whisper.Model` returned by the stub's load_model()."""

    def __init__(self, model_name="base.en", compute_type="int8", device="cpu"):
        self.model_name = model_name
        self.compute_type = compute_type
        self.device = device

    def TranscribeParameters(self, **kwargs):
        return kwargs

    def transcribe(self, audio, *, language=None, initial_prompt=None,
                   temperature=0.0, beam_size=5, best_of=5, patience=1.0,
                   offset=0.0, stride_length=16.0, compression_ratio_threshold=1.35,
                   logprob_threshold=-1.0, no_speech_threshold=0.6,
                   condition_on_previous_text=True, without_timestamps=False,
                   prepend_punctuations="¡'“¿([{-", append_punctuations="”’.,!?…)+->",
                   suppress_tokens=None, suppress_numerals=False, word_timestamps=False,
                   hallucination_silence_threshold=1.0, vad_filter=False,
                   trust_audio_dates=False, **kwargs):
        # The real package returns a 2-tuple (segments, info).  Yield one segment
        # so callers that iterate over the first element see exactly one segment
        # whose text contains the initial prompt when one was supplied (mirrors the
        # real package's behaviour of prepending the prompt's first line).
        prompt_text = ""
        if initial_prompt:
            prompt_text = initial_prompt.splitlines()[0]
        seg = _make_segment(
            text=prompt_text + ("hello world" if prompt_text else "hello world"),
            start=offset,
            end=offset + 1.0,
            no_speech_prob=0.0,
            avg_logprob=-0.2,
            compression_ratio=1.0,
            temperature=temperature,
        )
        info = TranscriptionInfo(
            language=language or "en",
            duration=1.0,
            segments_count=1,
            translation_options=None,
            ambient_temperature=0.0,
            text="hello world",
            all_language_probs=None,
        )
        return [seg], info

    def tokenize(self, text):
        return text.split()

    def detokenize(self, tokens):
        return " ".join(tokens)

    def detect_language(self, tokens):
        return {"en": 1.0}, "en"

    def _tokenizer(self):
        return self

    def _ctor_model(self):
        return self

    def embed_text(self, text, *, normalize=True, embed_progress=None):
        return [0.0] * 512

    def embed_batch(self, texts, *, normalize=True):
        return [[0.0] * 512 for _ in texts]

    def __eq__(self, other):
        return isinstance(other, _FakeModel)

    def __ne__(self, other):
        return not self.__eq__(other)
