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


_ensure_module("sounddevice", play=lambda *a, **k: None, InputStream=object)
_ensure_module("stt", load_model=lambda name: None)


@pytest.fixture(autouse=True)
def _isolated_log(tmp_path):
    """Every test logs to its own temp file, never to the real log."""
    import logging_setup
    logging_setup.shutdown()
    logging_setup.setup(str(tmp_path / "resonant.log"))
    yield
    logging_setup.shutdown()
