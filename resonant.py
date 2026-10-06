"""Resonant — application entry point.

Runs the dictation engine (background threads), the tray icon (background
thread) and the tkinter dashboard (main thread — tkinter must own the main
thread on Windows).

  python resonant.py            dashboard + tray + hold-to-talk
  python resonant.py --hidden   start hidden (used by the login entry)
  python resonant.py --no-startup   never touch the Run key / never kill another copy
  python resonant.py --smoke    construct settings + engine + UI modules only, exit 0

Everything heavy is imported INSIDE main(): with pythonw.exe a missing
dependency used to make the app silently never start (W4.1).
"""
import os
import sys
import threading
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)

CRASH_PATH = os.path.join(_BASE, "app_error.log")
LOCK_PATH = os.path.join(_BASE, ".vt.lock")
PID_PATH = os.path.join(_BASE, ".vt.pid")
SHOW_EVENT = "Local\\Resonant.Show"

SMOKE_MODE = "--smoke" in sys.argv
NO_STARTUP = "--no-startup" in sys.argv
DEV_MODE = NO_STARTUP or os.environ.get("RESONANT_DEV") == "1"

_LOCK_HANDLE = None      # module-global: a collected handle would FREE the lock
_SHUTTING_DOWN = threading.Event()


# ------------------------------------------------------------------ reporting
def write_error(message):
    """Fatal start-up errors must be visible under pythonw (W4.1)."""
    try:
        with open(CRASH_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"Resonant failed to start.\n\n{message}\n\nSee app_error.log.",
            "Resonant", 0x10)
    except Exception:
        pass


def install_excepthooks():
    """Uncaught worker/Tk exceptions currently vanish under pythonw (W4.1)."""
    import logging
    import traceback

    def _hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        write_error(text.strip().splitlines()[-1] if exc else "unknown error")
        logging.getLogger("resonant").error("UNCAUGHT: %s", text.strip())

    sys.excepthook = _hook
    try:
        threading.excepthook = lambda args: _hook(
            args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:
        pass


# ------------------------------------------------------------ single instance
def _read_prev_pid():
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _signal_running_instance():
    """Ask the already-running copy to show itself (W4.3). Returns True if it
    was reachable."""
    try:
        from platform_win import real_platform
        platform = real_platform()
        handle = platform.make_or_open_event(SHOW_EVENT)
        if not handle:
            return False
        return platform.signal_event(handle)
    except Exception:
        return False


def _is_verified_python(pid):
    """Only kill a PID we can prove is a python(w).exe that is not newer than
    the PID file — otherwise the PID was recycled (W4.3)."""
    try:
        from platform_win import real_platform
        platform = real_platform()
        image = os.path.basename(platform.process_image_name(pid)).lower()
        if image not in ("python.exe", "pythonw.exe"):
            return False
        created = platform.process_creation_time(pid)
        try:
            pid_mtime = os.path.getmtime(PID_PATH)
        except OSError:
            pid_mtime = None
        if created is not None and pid_mtime is not None and created > pid_mtime + 2:
            return False
        return True
    except Exception:
        return False


def _kill_verified(pid):
    import subprocess
    if not _is_verified_python(pid):
        return False
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True)
    return True


def acquire_lock():
    """Single instance. `.vt.lock` = one locked byte ('running'); the holder's
    PID lives in the never-locked `.vt.pid` (Windows byte-range locks block
    cross-process reads, so it cannot live inside the lock file).

    A second launch first *asks* the running copy to show (named event). Only
    if the lock is still held after ~3 s does it kill a VERIFIED python PID.
    """
    global _LOCK_HANDLE
    import msvcrt
    prev = _read_prev_pid()
    for attempt in range(3):
        lock = open(LOCK_PATH, "w")
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            _LOCK_HANDLE = lock
            with open(PID_PATH, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return
        except OSError:
            lock.close()
        if DEV_MODE:
            raise SystemExit("Another Resonant is running (dev mode: not killing it).")
        if attempt == 0:
            if _signal_running_instance():
                time.sleep(3)                      # give it time to exit
                continue
            if prev.isdigit() and int(prev) != os.getpid():
                if _kill_verified(prev):
                    time.sleep(1.5)
                    continue
                raise SystemExit("Another Resonant is running and could not be "
                                 "asked to quit — close it from the tray.")
            raise SystemExit("Another Resonant is running — use the tray icon.")
        time.sleep(1)
    raise SystemExit("Could not take the instance lock.")


# --------------------------------------------------------------------- main
def main():
    from logging_setup import log, setup
    setup()
    log(f"--- Resonant starting (pid {os.getpid()}) ---")

    if SMOKE_MODE:
        # W0.4: no lock, hooks, tray, registry, mic or window.
        from resonant_engine import ResonantEngine
        engine = ResonantEngine()
        import resonant_ui  # noqa: F401  (import == module initialised)
        del engine
        print("Resonant --smoke OK")
        log("--smoke OK: settings, engine and UI modules initialised.")
        return 0

    install_excepthooks()
    acquire_lock()

    import startup
    from resonant_engine import ResonantEngine

    engine = ResonantEngine()

    # --- Hide the console-less app's dashboard before Tk creates a window
    #     (DPI awareness must be set before the first window: W5.5) ---
    _set_dpi_awareness()

    import tkinter as tk

    from resonant_ui import ResonantUI, create_tray

    root = tk.Tk()
    ui = ResonantUI(root, engine)
    tray = create_tray(engine, ui)
    engine.on("log", lambda m: log(m))

    def request_quit():
        if _SHUTTING_DOWN.is_set():
            return
        _SHUTTING_DOWN.set()
        log("Shutting down…")
        try:
            engine.shutdown()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass

        def _destroy():
            try:
                root.quit()
                root.destroy()
            except Exception:
                os._exit(0)
        try:
            root.after(0, _destroy)
        except Exception:
            os._exit(0)
        watchdog = threading.Timer(2.0, lambda: os._exit(0))
        watchdog.daemon = True
        watchdog.start()

    engine.on_quit = request_quit
    ui.on_quit = request_quit

    # Start-up registration: ONCE here (W4.2), never per state emit.
    if not DEV_MODE:
        startup.migrate_legacy()
        if startup.is_enabled():
            # Existing entry is respected; adopt it as the user's choice.
            engine.settings["run_at_startup"] = True
            engine.settings["startup_prompted"] = True
        else:
            startup.sync(engine.settings)

    engine.start()

    if "--hidden" in sys.argv:
        ui.hide()
    elif not engine.settings.get("startup_prompted"):
        ui.show_startup_prompt()

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        request_quit()
    log("Resonant exited cleanly.")
    return 0


def _set_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # per-monitor aware
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:                      # pragma: no cover - fatal path
        import traceback
        write_error(f"{e}\n{traceback.format_exc()}")
        raise SystemExit(1) from None
