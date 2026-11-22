# SPDX-License-Identifier: MIT
"""Unit tests for the power_service module."""

import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amd_debug import power_db as db
from amd_debug import power_service as svc


@pytest.fixture
def iio_root(tmp_path):
    """One pac1954 device with two rails"""
    root = tmp_path / "iio"
    dev = root / "iio:device0"
    dev.mkdir(parents=True)
    (dev / "name").write_text("pac1954\n")
    for n, label in ((1, "RAIL_1"), (2, "RAIL_2")):
        (dev / f"in_power{n}_label").write_text(label + "\n")
        (dev / f"in_energy{n}_raw").write_text("100\n")
        (dev / f"in_energy{n}_scale").write_text("2.0\n")
    return root


@pytest.fixture
def conn(tmp_path):
    """Database connection"""
    c = db.connect(tmp_path / "p.db")
    db.init_schema(c)
    yield c
    c.close()


class TestCaptureService:
    """Capture loop"""

    def test_poll_once(self, conn, iio_root):
        """Rows written; unreadable rails skipped"""
        s = svc.CaptureService(conn, iio_root=iio_root)
        rails = svc.discover_rails(iio_root)
        ids = db.upsert_rails(conn, rails)
        assert s.poll_once(rails, ids) == 2
        (iio_root / "iio:device0" / "in_energy2_raw").unlink()
        assert s.poll_once(rails, ids) == 1
        (iio_root / "iio:device0" / "in_energy1_raw").write_text("garbage")
        assert s.poll_once(rails, ids) == 0

    def test_run_no_rails(self, conn, tmp_path):
        """No hardware is an error"""
        s = svc.CaptureService(conn, iio_root=tmp_path / "missing")
        with pytest.raises(RuntimeError):
            s.run()

    def test_run_stops(self, conn, iio_root):
        """Loop polls repeatedly and records the run end"""
        s = svc.CaptureService(conn, note="n", iio_root=iio_root)
        t = threading.Timer(0.35, s.stop)
        t.start()
        s.run(interval_s=0.1)
        t.join()
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 4
        note, ended = conn.execute("SELECT note, ended_ts FROM capture_runs").fetchone()
        assert note == "n" and ended is not None

    def test_run_falls_behind(self, conn, iio_root):
        """A slow poll logs a warning and skips ticks"""
        s = svc.CaptureService(conn, iio_root=iio_root)
        orig = s.poll_once
        calls = []

        def slow(rails, ids):
            calls.append(1)
            if len(calls) == 1:
                import time  # pylint: disable=import-outside-toplevel

                time.sleep(0.25)
            else:
                s.stop()
            return orig(rails, ids)

        s.poll_once = slow
        with patch("amd_debug.power_service.logger.warning") as warn:
            s.run(interval_s=0.1)
        assert any("falling behind" in c.args[0] for c in warn.call_args_list)

    def test_signal_handlers(self, conn):
        """Handlers stop the service"""
        s = svc.CaptureService(conn)
        with patch("amd_debug.power_service.signal.signal") as sig:
            s.install_signal_handlers()
        assert {c.args[0] for c in sig.call_args_list} == {
            signal.SIGTERM,
            signal.SIGINT,
        }
        sig.call_args_list[0].args[1](signal.SIGTERM, None)
        assert s._stop.is_set()  # pylint: disable=protected-access


class TestUnit:
    """systemd unit helpers"""

    @pytest.fixture(autouse=True)
    def _xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    def test_paths(self, monkeypatch):
        """Unit directories"""
        assert svc.unit_path() == (
            Path(svc.user_unit_dir()) / "amd-power-capture.service"
        )
        monkeypatch.delenv("XDG_CONFIG_HOME")
        assert svc.user_unit_dir() == Path.home() / ".config" / "systemd" / "user"
        assert svc.default_exec_path().name == "amd-power"

    def test_render(self):
        """Options are baked into ExecStart"""
        text = svc.render_unit(
            Path("/opt/amd-power"),
            db_path=Path("/d b.db"),
            interval_s=2.5,
            note="a note",
            log_level="DEBUG",
        )
        assert (
            'ExecStart="/opt/amd-power" "run" "--db" "/d b.db" "--interval" "2.5" '
            '"--note" "a note" "--log-level" "DEBUG"\n' in text
        )
        bare = svc.render_unit(
            Path("/x"), db_path=None, interval_s=5.0, note=None, log_level="INFO"
        )
        assert "--db" not in bare and "--note" not in bare

    def test_render_escapes_systemd_syntax(self):
        """Specifiers, variables, quotes and backslashes stay literal"""
        text = svc.render_unit(
            Path("/x"),
            db_path=Path("/%h/db"),
            interval_s=1.0,
            note='a $HOME "q" \\ 100%',
            log_level="INFO",
        )
        assert '"/%%h/db"' in text
        assert '"a $$HOME \\"q\\" \\\\ 100%%"' in text

    def test_write_remove(self):
        """Write, refuse overwrite, force, remove"""
        assert svc.remove_unit() is False
        path = svc.write_unit("a")
        assert path.read_text() == "a"
        with pytest.raises(FileExistsError):
            svc.write_unit("b")
        svc.write_unit("b", force=True)
        assert path.read_text() == "b"
        assert svc.remove_unit() is True
        assert not path.exists()

    @patch("amd_debug.power_service.subprocess")
    def test_commands(self, sub):
        """systemctl / journalctl wrappers"""
        svc.systemctl("start", "x", check=False)
        sub.run.assert_called_with(["systemctl", "--user", "start", "x"], check=False)
        sub.call.return_value = 3
        assert svc.journalctl("-n", "5") == 3
        sub.call.assert_called_with(
            ["journalctl", "--user", "-u", svc.UNIT_NAME, "-n", "5"]
        )
        svc.daemon_reload()
        sub.run.assert_called_with(["systemctl", "--user", "daemon-reload"], check=True)
        sub.run.return_value = MagicMock(returncode=0)
        assert svc.is_unit_loaded() is True
        sub.run.return_value = MagicMock(returncode=1)
        assert svc.is_unit_loaded() is False
