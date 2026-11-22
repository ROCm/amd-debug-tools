# SPDX-License-Identifier: MIT
"""Unit tests for the amd-power tool."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amd_debug import power
from amd_debug import power_db as db
from amd_debug.power_rails import PowerRail


def args(*argv):
    """Parse argv"""
    return power.parse_args(list(argv))


class TestParseWhen:
    """parse_when"""

    def test_default(self):
        """None gives default"""
        assert power.parse_when(None, now=100, default=7) == 7

    @pytest.mark.parametrize(
        "text,secs",
        [
            ("30", 30),
            ("30s", 30),
            ("5m", 300),
            ("2 hours", 7200),
            ("1d", 86400),
            ("1.5h", 5400),
            ("10 MIN", 600),
        ],
    )
    def test_duration(self, text, secs):
        """Durations are relative to now"""
        assert power.parse_when(text, now=100000, default=0) == 100000 - secs

    def test_iso(self):
        """Naive ISO is UTC, aware is respected"""
        assert power.parse_when("1970-01-01T00:01:00", now=0, default=0) == 60
        assert power.parse_when("1970-01-01T01:00:00+01:00", now=0, default=0) == 0

    def test_bad(self):
        """Garbage is rejected"""
        with pytest.raises(ValueError):
            power.parse_when("yesterday-ish", now=0, default=0)


class TestParseArgs:
    """Argument parsing"""

    def test_run_defaults(self):
        """run defaults"""
        a = args("run")
        assert a.command == "run" and a.interval == 5.0
        assert a.log_level == "INFO" and a.db is None

    def test_log_level_case(self):
        """Case-insensitive log level"""
        assert args("run", "--log-level", "debug").log_level == "DEBUG"

    def test_interval_min(self):
        """Interval below 0.1s is rejected"""
        with pytest.raises(SystemExit):
            args("run", "--interval", "0.01")

    def test_plot(self):
        """plot options"""
        a = args(
            "plot",
            "o.html",
            "--rails",
            "A*",
            "--rails",
            "B*",
            "--metric",
            "Power",
            "--metric",
            "energy",
            "--energy-unit",
            "j",
        )
        assert a.rails == ["A*", "B*"]
        assert a.metrics == ["power", "energy"]
        assert a.energy_unit == "J"

    def test_install(self):
        """install options"""
        a = args("install", "--force", "--now", "--exec", "/x")
        assert a.force and a.now and a.exec_path == Path("/x")


class TestMain:
    """main() dispatch"""

    @patch.object(sys, "argv", new=["amd-power", "--version"])
    def test_version(self):
        """--version"""
        with patch("builtins.print") as p:
            assert power.main() is None
        p.assert_called_once()

    @patch.object(sys, "argv", new=["amd-power"])
    def test_no_command(self):
        """Bare invocation prints help and fails"""
        with pytest.raises(SystemExit) as e:
            power.main()
        assert e.value.code == 0

    @patch.object(sys, "argv", new=["amd-power", "--tool-debug", "status"])
    def test_dispatch(self):
        """Return code propagated"""
        with patch("amd_debug.power.service.systemctl") as sc:
            sc.return_value = MagicMock(returncode=3)
            assert power.main() == 3
            sc.assert_called_with("status", power.service.UNIT_NAME, check=False)

    @patch.object(sys, "argv", new=["amd-power", "stop"])
    def test_dispatch_ok(self):
        """Success returns None"""
        with patch("amd_debug.power.service.systemctl") as sc:
            sc.return_value = MagicMock(returncode=0)
            assert power.main() is None


class TestRun:
    """run subcommand"""

    @pytest.fixture
    def iio(self, tmp_path):
        """Fake IIO tree"""
        d = tmp_path / "iio" / "iio:device0"
        d.mkdir(parents=True)
        (d / "name").write_text("pac1954\n")
        (d / "in_power1_label").write_text("R\n")
        (d / "in_energy1_raw").write_text("1\n")
        (d / "in_energy1_scale").write_text("1\n")
        return tmp_path / "iio"

    def test_no_hardware(self, tmp_path):
        """Missing hardware gives rc 1"""
        a = args("run", "--db", str(tmp_path / "p.db"))
        with patch(
            "amd_debug.power_service.IIO_DEVICES_ROOT", tmp_path / "none"
        ), patch(
            "amd_debug.power_service.CaptureService.install_signal_handlers"
        ), patch("amd_debug.power_service.discover_rails", return_value=[]):
            assert power.cmd_run(a) == 1

    def test_ok(self, tmp_path):
        """Loop runs and returns 0"""
        a = args("run", "--db", str(tmp_path / "p.db"))
        with patch("amd_debug.power.service.CaptureService") as cs:
            assert power.cmd_run(a) == 0
            cs.return_value.run.assert_called_once_with(interval_s=5.0)


class TestInstall:
    """install / uninstall"""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(power, "systemd_in_use", lambda: True)
        self.exe = tmp_path / "amd-power"
        self.exe.write_text("")

    def test_no_systemd(self):
        """Refuse without systemd"""
        with patch("amd_debug.power.systemd_in_use", return_value=False):
            assert power.cmd_install(args("install")) == 1

    def test_missing_exec(self, tmp_path):
        """Missing executable"""
        a = args("install", "--exec", str(tmp_path / "nope"))
        assert power.cmd_install(a) == 1

    @patch("amd_debug.power.service.systemctl")
    @patch("amd_debug.power.service.daemon_reload")
    def test_install_now_and_exists(self, reload, sc):
        """Install, start, then refuse to overwrite"""
        a = args("install", "--exec", str(self.exe), "--now")
        assert power.cmd_install(a) == 0
        reload.assert_called_once()
        sc.assert_called_once_with("enable", "--now", power.service.UNIT_NAME)
        assert power.service.unit_path().exists()
        assert power.cmd_install(a) == 1

    @patch("amd_debug.power.service.systemctl")
    @patch("amd_debug.power.service.daemon_reload")
    def test_install_no_start(self, _reload, sc):
        """Install without --now"""
        a = args("install", "--exec", str(self.exe))
        assert power.cmd_install(a) == 0
        sc.assert_not_called()

    @patch("amd_debug.power.service.daemon_reload")
    @patch("amd_debug.power.service.systemctl")
    @patch("amd_debug.power.service.is_unit_loaded", return_value=True)
    def test_uninstall(self, _loaded, sc, reload):
        """Uninstall variants"""
        assert power.cmd_uninstall(args("uninstall")) == 0  # not present
        power.service.write_unit("x")
        assert power.cmd_uninstall(args("uninstall", "--keep-unit")) == 0
        assert power.service.unit_path().exists()
        assert power.cmd_uninstall(args("uninstall")) == 0
        assert not power.service.unit_path().exists()
        reload.assert_called_once()
        assert sc.call_count == 3


class TestMisc:
    """enable/disable/logs"""

    @patch("amd_debug.power.service.systemctl")
    def test_enable_disable(self, sc):
        """--now handling"""
        sc.return_value = MagicMock(returncode=0)
        power.cmd_enable_disable(args("enable", "--now"))
        sc.assert_called_with("enable", "--now", power.service.UNIT_NAME, check=False)
        power.cmd_enable_disable(args("disable"))
        sc.assert_called_with("disable", power.service.UNIT_NAME, check=False)

    @patch("amd_debug.power.service.journalctl", return_value=0)
    def test_logs(self, jc):
        """logs options"""
        power.cmd_logs(args("logs", "-f", "-n", "5"))
        jc.assert_called_with("-n", "5", "-f")
        power.cmd_logs(args("logs"))
        jc.assert_called_with("-n", "100")


class TestPlot:
    """plot subcommand"""

    @pytest.fixture
    def dbfile(self, tmp_path):
        """Database with recent samples"""
        import time  # pylint: disable=import-outside-toplevel

        path = tmp_path / "p.db"
        c = db.connect(path)
        db.init_schema(c)
        r = PowerRail("iio:device0", 1, "CPU", Path("/x"), 1.0)
        rid = db.upsert_rails(c, [r])[r]
        now = time.time()
        db.insert_samples(c, [(rid, now - 60 + 5 * i, 3.0 * i, 1.0) for i in range(10)])
        c.close()
        return path

    def test_missing_db(self, tmp_path):
        """No database"""
        pytest.importorskip("plotly")
        a = args("plot", str(tmp_path / "o.html"), "--db", str(tmp_path / "no.db"))
        assert power.cmd_plot(a) == 1

    def test_no_plotly(self, tmp_path):
        """Missing plotly is reported"""
        with patch.dict(sys.modules, {"plotly": None}):
            assert power.cmd_plot(args("plot", str(tmp_path / "o.html"))) == 1

    def test_bad_time(self, dbfile, tmp_path):
        """Unparseable window"""
        pytest.importorskip("plotly")
        a = args(
            "plot", str(tmp_path / "o.html"), "--db", str(dbfile), "--since", "bogus"
        )
        assert power.cmd_plot(a) == 1

    def test_empty_window(self, dbfile, tmp_path):
        """No samples"""
        pytest.importorskip("plotly")
        a = args(
            "plot", str(tmp_path / "o.html"), "--db", str(dbfile), "--rails", "NOPE"
        )
        assert power.cmd_plot(a) == 1

    def test_single(self, dbfile, tmp_path):
        """Single metric"""
        pytest.importorskip("plotly")
        out = tmp_path / "o.html"
        a = args(
            "plot", str(out), "--db", str(dbfile), "--since", "5m", "--resample", "10s"
        )
        assert power.cmd_plot(a) == 0
        assert out.stat().st_size > 0

    def test_multi(self, dbfile, tmp_path):
        """Stacked metrics"""
        pytest.importorskip("plotly")
        out = tmp_path / "o.html"
        a = args(
            "plot",
            str(out),
            "--db",
            str(dbfile),
            "--since",
            "5m",
            "--metric",
            "power",
            "--metric",
            "energy",
            "--metric",
            "aggregate",
        )
        assert power.cmd_plot(a) == 0
        assert out.stat().st_size > 0
