"""startup.py (W4.2) + logging_setup.py (W4.6) unit tests — fake registry, no
real Run key, no file I/O on the hot path."""
import os
import time

import pytest

import logging_setup
import startup


class FakeRegistryPlatform:
    """In-memory stand-in for the Win32 registry calls startup.py makes."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.writes = []
        self.deletes = []

    def registry_get(self, path, name):
        return self.entries.get((path, name))

    def registry_set(self, path, name, value):
        self.entries[(path, name)] = value
        self.writes.append((path, name, value))

    def registry_delete(self, path, name):
        existed = (path, name) in self.entries
        self.entries.pop((path, name), None)
        if existed:
            self.deletes.append((path, name))
        return existed


class TestStartupCommand:
    def test_command_contains_hidden(self):
        assert "--hidden" in startup.startup_command()

    def test_command_can_omit_hidden(self):
        assert "--hidden" not in startup.startup_command(hidden=False)

    def test_command_is_quoted(self):
        cmd = startup.startup_command()
        assert cmd.startswith('"') and '"' in cmd[1:]


class TestStartupState:
    def test_is_enabled_false_when_absent(self):
        assert startup.is_enabled(FakeRegistryPlatform()) is False

    def test_enable_then_disable(self):
        platform = FakeRegistryPlatform()
        assert startup.enable(platform)
        assert startup.is_enabled(platform)
        assert startup.disable(platform)
        assert startup.is_enabled(platform) is False

    def test_sync_once_respects_settings(self):
        platform = FakeRegistryPlatform()
        startup.sync({"run_at_startup": True}, platform)
        assert startup.is_enabled(platform)
        # The user removes it manually; sync with run_at_startup False must NOT
        # re-add it (W4.2).
        startup.disable(platform)
        startup.sync({"run_at_startup": False}, platform)
        assert startup.is_enabled(platform) is False
        assert platform.writes and len(platform.writes) == 1

    def test_user_choice_records_opt_in(self):
        platform = FakeRegistryPlatform()
        settings = {"run_at_startup": False, "startup_prompted": False}
        startup.apply_user_choice(settings, True, platform)
        assert settings["run_at_startup"] is True
        assert settings["startup_prompted"] is True
        assert startup.is_enabled(platform)

    def test_migrate_legacy_removes_voicetype(self):
        platform = FakeRegistryPlatform({(startup.RUN_KEY, "VoiceType"): "old"})
        assert startup.migrate_legacy(platform) is True
        assert (startup.RUN_KEY, "VoiceType") not in platform.entries

    def test_registry_failure_never_raises(self):
        class Broken:
            def registry_get(self, path, name):
                raise OSError("denied")

            def registry_set(self, *a):
                raise OSError("denied")

            def registry_delete(self, *a):
                raise OSError("denied")
        assert startup.is_enabled(Broken()) is False
        assert startup.enable(Broken()) is False
        assert startup.disable(Broken()) is False
        assert startup.migrate_legacy(Broken()) is False


class TestLoggingSetup:
    def test_setup_is_idempotent(self, tmp_path):
        logging_setup.shutdown()
        log_path = tmp_path / "r.log"
        logging_setup.setup(str(log_path))
        handlers_first = len(logging_setup._logger.handlers)
        logging_setup.setup(str(log_path))
        assert len(logging_setup._logger.handlers) == handlers_first
        logging_setup.shutdown()

    def test_repeated_logging_does_not_rewrite_the_file(self, tmp_path):
        logging_setup.shutdown()
        log_path = tmp_path / "r.log"
        logging_setup.setup(str(log_path))
        for i in range(50):
            logging_setup.log(f"line {i}")
        logging_setup.shutdown()
        size = os.path.getsize(log_path)
        time.sleep(0.05)
        assert size < 20_000                 # no per-call rewrite storm (W4.6)

    def test_transcript_gated_by_setting(self, tmp_path):
        logging_setup.shutdown()
        log_path = tmp_path / "r.log"
        logging_setup.setup(str(log_path))
        logging_setup.log_transcript("Heard", "secret words", enabled=False)
        logging_setup.log_transcript("Heard", "secret words", enabled=True)
        logging_setup.shutdown()
        text = log_path.read_text(encoding="utf-8")
        assert "secret words" in text        # only the opted-in line
        assert text.count("secret words") == 1
        assert "chars" in text

    def test_clear_log_files_removes_log(self, tmp_path):
        logging_setup.shutdown()
        log_path = tmp_path / "r.log"
        logging_setup.setup(str(log_path))
        logging_setup.log("hello")
        logging_setup.shutdown()
        assert log_path.exists()
        logging_setup.clear_log_files(str(log_path))
        assert not log_path.exists()


class TestQuietLogging:
    def test_log_never_raises_on_broken_path(self, tmp_path):
        logging_setup.shutdown()
        logging_setup.setup(str(tmp_path / "sub" / "missing" / "x.log"))
        logging_setup.log("still fine")
        logging_setup.shutdown()


@pytest.mark.parametrize("value,expected", [
    ("30m", 1800), ("1h", 3600), ("45s", 45), ("600", 600), (None, 1800),
])
def test_keep_alive_parsing(value, expected):
    import resonant_engine
    assert resonant_engine._keep_alive_seconds(value) == expected


if __name__ == "__main__":
    pytest.main([__file__])
