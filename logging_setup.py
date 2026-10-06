"""Resonant — logging configuration (W4.6).

Replaces the old hand-rolled rotating log, which had a hysteresis bug: the
rotated file was still larger than the trigger, so *every* `log()` call after a
rotation re-read and rewrote ~200 KB — on the hotkey thread.

Now: a `QueueHandler` on the calling thread (never touches a file) feeding a
`QueueListener` that owns a `RotatingFileHandler` (512 KB × 2 backups).

Privacy (§1 rule 6): transcript text is only written when `log_transcripts` is
on; otherwise just lengths and stage timings.
"""
import logging
import logging.handlers
import os
import queue

from config import LOG_PATH

MAX_BYTES = 512 * 1024
BACKUPS = 2
LOGGER_NAME = "resonant"
FORMAT = "[%(asctime)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"

_queue = None
_listener = None
_logger = logging.getLogger(LOGGER_NAME)


def setup(log_path=LOG_PATH):
    """Idempotent: attach a QueueHandler + rotating file listener once."""
    global _queue, _listener
    if _listener is not None:
        return _logger
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    # Never attach a second handler stack: after a shutdown, remove the stale
    # QueueHandler (whose queue no longer has a listener) and rebuild.
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
    _queue = queue.SimpleQueue()
    _logger.addHandler(logging.handlers.QueueHandler(_queue))
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
            encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    except Exception:
        handler = None
    if handler is not None:
        _listener = logging.handlers.QueueListener(_queue, handler)
        _listener.start()
    return _logger


def log(msg, level=logging.INFO):
    """Compatibility shim: every existing `log(...)` call still works."""
    try:
        if _listener is None:
            setup()
        _logger.log(level, "%s", msg)
    except Exception:
        pass


def log_transcript(kind, text, enabled=False):
    """Log a transcript only when the user opted in; otherwise log its shape."""
    text = text or ""
    if enabled:
        log(f"{kind}: {text}")
    else:
        log(f"{kind}: <{len(text)} chars, {len(text.split())} words>")


def shutdown():
    """Stop the listener (flushing queued records) and drop the handler."""
    global _listener, _queue
    try:
        if _listener is not None:
            _listener.stop()
    except Exception:
        pass
    _listener = None
    _queue = None
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)


def clear_log_files(log_path=LOG_PATH):
    """Delete the log and its rotations (used by 'Clear history & logs')."""
    shutdown()
    for path in [log_path] + [f"{log_path}.{i}" for i in range(1, BACKUPS + 1)]:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
