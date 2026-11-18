# SPDX-License-Identifier: MIT
"""Long-running capture loop and user systemd unit management for amd-power."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Sequence

from amd_debug import power_db as db
from amd_debug.power_rails import IIO_DEVICES_ROOT, PowerRail, discover_rails

UNIT_NAME = "amd-power-capture.service"
SERVICE_DESCRIPTION = "AMD per-rail power capture"


class CaptureService:
    """Poll every discovered rail's energy accumulator into SQLite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        note: Optional[str] = None,
        iio_root: Path = IIO_DEVICES_ROOT,
    ):
        self.conn = conn
        self.note = note
        self.iio_root = iio_root
        self._stop = threading.Event()

    def stop(self) -> None:
        """Request the loop to terminate."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGTERM/SIGINT."""

        def _handler(signum, _frame):
            logging.info("received signal %s, stopping", signum)
            self.stop()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def poll_once(
        self, rails: Sequence[PowerRail], rail_ids: Dict[PowerRail, int]
    ) -> int:
        """Read all rails once; returns the number of rows written."""
        ts = time.time()
        rows = []
        for rail in rails:
            try:
                energy = rail.read_energy_raw()
            except (OSError, ValueError) as e:
                logging.warning("read failed for %s: %s", rail.label, e)
                continue
            rows.append((rail_ids[rail], ts, energy, rail.energy_scale))
        db.insert_samples(self.conn, rows)
        return len(rows)

    def run(self, interval_s: float = 5.0) -> None:
        """Run until :meth:`stop` is called."""
        rails = discover_rails(self.iio_root)
        if not rails:
            raise RuntimeError(
                f"no power monitor rails discovered under {self.iio_root}"
            )
        logging.info(
            "discovered %d rails across %d chips",
            len(rails),
            len({r.device for r in rails}),
        )
        rail_ids = db.upsert_rails(self.conn, rails)
        run_id = db.start_run(self.conn, note=self.note)
        logging.info("capture run %d started; polling every %.3fs", run_id, interval_s)

        next_tick = time.monotonic()
        try:
            while not self._stop.is_set():
                n = self.poll_once(rails, rail_ids)
                logging.debug("wrote %d rows", n)
                next_tick += interval_s
                now = time.monotonic()
                if next_tick <= now:
                    missed = int((now - next_tick) // interval_s) + 1
                    logging.warning("falling behind, skipping %d tick(s)", missed)
                    next_tick += missed * interval_s
                self._stop.wait(timeout=max(0.0, next_tick - time.monotonic()))
        finally:
            db.end_run(self.conn, run_id)
            logging.info("capture run %d ended", run_id)


def user_unit_dir() -> Path:
    """``~/.config/systemd/user`` (honors ``XDG_CONFIG_HOME``)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def unit_path() -> Path:
    """Location of the user unit file."""
    return user_unit_dir() / UNIT_NAME


def default_exec_path() -> Path:
    """Path to the ``amd-power`` executable alongside the current python."""
    return Path(sys.executable).parent / "amd-power"


def _systemd_quote(arg: str) -> str:
    """Quote one ExecStart word using systemd's rules (not the shell's).

    systemd expands ``%`` specifiers and ``$`` variables inside ExecStart, so
    both must be doubled to stay literal.
    """
    escaped = (
        arg.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def render_unit(
    exec_path: Path,
    *,
    db_path: Optional[Path],
    interval_s: float,
    note: Optional[str],
    log_level: str,
) -> str:
    """Render the user unit with the capture options baked into ExecStart."""
    args = []
    if db_path is not None:
        args += ["--db", str(db_path)]
    args += ["--interval", str(interval_s)]
    if note:
        args += ["--note", note]
    args += ["--log-level", log_level]
    exec_start = " ".join(_systemd_quote(a) for a in [str(exec_path), "run", *args])

    return (
        "[Unit]\n"
        f"Description={SERVICE_DESCRIPTION}\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def write_unit(contents: str, *, force: bool = False) -> Path:
    """Write the unit file; refuses to overwrite unless ``force``."""
    path = unit_path()
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; use --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path


def remove_unit() -> bool:
    """Remove the unit file; returns False if it wasn't present."""
    path = unit_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``systemctl --user <args>``."""
    return subprocess.run(["systemctl", "--user", *args], check=check)


def journalctl(*args: str) -> int:
    """``journalctl --user -u <unit> <args>``; returns the exit code."""
    return subprocess.call(["journalctl", "--user", "-u", UNIT_NAME, *args])


def daemon_reload() -> None:
    """Reload the user systemd manager."""
    systemctl("daemon-reload")


def is_unit_loaded() -> bool:
    """Whether systemd knows about the unit."""
    res = subprocess.run(
        ["systemctl", "--user", "cat", UNIT_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return res.returncode == 0
