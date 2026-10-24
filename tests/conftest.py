"""Test bootstrap (brief §1 rule 5): importing resonant_engine must never
require audio hardware or heavy ML frameworks. Stub anything heavy or absent so the
pure logic is testable on any machine and OS.

Also redirects logging: the log is the app's main diagnostic tool, so test
noise must never land in the owner's resonant.log."""
import sys
import types

import pytest


def _ensure_module(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]
    try:
        __import__(name)
        return sys.modules[name]
    except ImportError:
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod


def _stub_module(name, **attrs):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    else:
        mod = sys.modules[name]
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


_ensure_module("sounddevice", play=lambda *a, **k: None, InputStream=object)
# The ASR backend is OpenAI Whisper: PyPI distribution `openai-whisper`, which
# imports as `whisper` and pulls in torch. Stub it under its real import name so
# tests run without the multi-GB dependency.
_stub_module("whisper", load_model=lambda name: None)

# Faster-whisper backend (Phase 8): stubbed so tests that touch the faster
# path never import the real CTranslate2 package in the light dev venv. The fake
# model's .transcribe() returns a (segments, info) tuple whose segments are
# objects with the attributes faster_whisper.Segment exposes — that is exactly
# what faster_stt.FasterModel transcribe() converts into the dict shape.
class _FakeSegment:
    def __init__(self, text="", start=0.0, end=0.0, no_speech_prob=0.0,
                 avg_logprob=0.0, compression_ratio=1.0):
        self.text = text
        self.start = start
        self.end = end
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio


class _FakeFasterModel:
    def __init__(self, target="base.en"):
        self.target = target

    def transcribe(self, audio, **kwargs):
        vad = kwargs.get("vad_filter")
        initial_prompt = kwargs.get("initial_prompt")
        return [_FakeSegment(text="hello world")], {
            "language": kwargs.get("language", "en"),
            "vad_filter": vad,
            "initial_prompt": initial_prompt,
        }




@pytest.fixture(autouse=True)
def _isolated_log(tmp_path):
    """Every test logs to its own temp file, never to the real log."""
    import logging_setup
    logging_setup.shutdown()
    logging_setup.setup(str(tmp_path / "resonant.log"))
    yield
    logging_setup.shutdown()
