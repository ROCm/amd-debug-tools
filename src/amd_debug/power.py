# SPDX-License-Identifier: MIT
"""Per-rail power capture and plotting tool"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from amd_debug import power_db as db
from amd_debug import power_service as service
from amd_debug.common import print_color, systemd_in_use, version

logger = logging.getLogger(__name__)

DURATION_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*"
    r"(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours|d|day|days)?\s*$",
    re.IGNORECASE,
)
_UNIT_S = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
METRICS = ["power", "energy", "aggregate"]
SYSTEMCTL_VERBS = ["start", "stop", "restart", "status"]


def parse_when(value: str | None, *, now: float, default: float) -> float:
    """Parse a duration (``2h``, ``30m``; meaning ``now - duration``) or an
    ISO-8601 timestamp (naive timestamps are UTC) into epoch seconds."""
    if value is None:
        return default
    m = DURATION_RE.match(value)
    if m:
        unit = (m.group(2) or "s").lower()
        return now - float(m.group(1)) * _UNIT_S[unit]
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"cannot parse time/duration {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _positive_float(value: str) -> float:
    val = float(value)
    if val < 0.1:
        raise argparse.ArgumentTypeError("must be at least 0.1")
    return val


def _add_run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path (default: $AMD_POWER_DB or ~/.local/share/amd-debug-tools/power.db)",
    )
    p.add_argument(
        "--interval",
        type=_positive_float,
        default=5.0,
        help="Poll interval in seconds (default: 5)",
    )
    p.add_argument("--note", default=None, help="Note attached to this capture run")
    p.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default="INFO",
        help="Capture log level (default: INFO)",
    )


def parse_args(argv=None):
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Capture and plot per-rail power from IIO power monitors"
    )
    parser.add_argument(
        "--version", action="store_true", help="Show version information"
    )
    parser.add_argument(
        "--tool-debug", action="store_true", help="Enable tool debug logging"
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the capture loop in the foreground")
    _add_run_options(run)

    inst = sub.add_parser("install", help="Write the user systemd unit")
    _add_run_options(inst)
    inst.add_argument("--force", action="store_true", help="Overwrite existing unit")
    inst.add_argument(
        "--now", action="store_true", help="Enable and start the unit after install"
    )
    inst.add_argument(
        "--exec",
        dest="exec_path",
        type=Path,
        default=None,
        help="Path to amd-power (default: alongside the current python)",
    )

    uninst = sub.add_parser("uninstall", help="Stop, disable and remove the unit")
    uninst.add_argument(
        "--keep-unit", action="store_true", help="Leave the unit file in place"
    )

    enable = sub.add_parser("enable", help="Enable the unit")
    enable.add_argument("--now", action="store_true", help="Also start the unit")
    disable = sub.add_parser("disable", help="Disable the unit")
    disable.add_argument("--now", action="store_true", help="Also stop the unit")

    for verb in SYSTEMCTL_VERBS:
        sub.add_parser(verb, help=f"{verb.capitalize()} the unit")

    logs = sub.add_parser("logs", help="Show the unit's journal")
    logs.add_argument("-f", "--follow", action="store_true", help="Follow the journal")
    logs.add_argument(
        "-n", "--lines", type=int, default=100, help="Lines to show (default: 100)"
    )

    plot = sub.add_parser("plot", help="Plot captured data to an HTML file")
    plot.add_argument("output", type=Path, help="Output HTML file")
    plot.add_argument("--db", type=Path, default=None, help="SQLite path")
    plot.add_argument(
        "--since",
        default="10m",
        help="Window start as duration ('2h') or ISO timestamp (default: 10m)",
    )
    plot.add_argument(
        "--until", default=None, help="Window end as duration or ISO timestamp"
    )
    plot.add_argument(
        "--rails",
        action="append",
        default=[],
        help="Rail label glob filter (repeatable)",
    )
    plot.add_argument(
        "--metric",
        dest="metrics",
        action="append",
        type=str.lower,
        choices=METRICS,
        help="Metric to plot (repeatable; default: power)",
    )
    plot.add_argument(
        "--resample", default=None, help="Pandas resample frequency ('10s', '1min')"
    )
    plot.add_argument(
        "--energy-unit",
        type=lambda s: {"wh": "Wh", "j": "J"}.get(s.lower(), s),
        choices=["Wh", "J"],
        default="Wh",
        help="Unit for cumulative energy (default: Wh)",
    )
    return parser.parse_args(argv)


def cmd_run(args) -> int:
    """Foreground capture loop."""
    path = args.db or db.default_db_path()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("opening database at %s", path)
    conn = db.connect(path)
    db.init_schema(conn)
    svc = service.CaptureService(conn, note=args.note)
    svc.install_signal_handlers()
    try:
        svc.run(interval_s=args.interval)
    except RuntimeError as e:
        print_color(str(e), "❌")
        return 1
    return 0


def cmd_install(args) -> int:
    """Write the user unit."""
    if not systemd_in_use():
        print_color("systemd is not in use on this system", "❌")
        return 1
    exec_path = args.exec_path or service.default_exec_path()
    if not exec_path.exists():
        print_color(f"executable not found: {exec_path} (override with --exec)", "❌")
        return 1
    unit = service.render_unit(
        exec_path,
        db_path=args.db,
        interval_s=args.interval,
        note=args.note,
        log_level=args.log_level,
    )
    try:
        path = service.write_unit(unit, force=args.force)
    except FileExistsError as e:
        print_color(str(e), "❌")
        return 1
    print_color(f"wrote {path}", "🐧")
    service.daemon_reload()
    if args.now:
        service.systemctl("enable", "--now", service.UNIT_NAME)
        print_color(f"enabled and started {service.UNIT_NAME}", "🐧")
    else:
        print_color(
            f"installed; run 'amd-power enable --now' or "
            f"'systemctl --user start {service.UNIT_NAME}' to start it.",
            "○",
        )
    return 0


def cmd_uninstall(args) -> int:
    """Stop, disable and remove the unit."""
    if service.is_unit_loaded():
        service.systemctl("disable", "--now", service.UNIT_NAME, check=False)
    if args.keep_unit:
        print_color("left unit file in place (--keep-unit)", "○")
    elif service.remove_unit():
        print_color(f"removed {service.unit_path()}", "🐧")
        service.daemon_reload()
    else:
        print_color(f"{service.unit_path()} not present", "○")
    return 0


def cmd_enable_disable(args) -> int:
    """Enable or disable the unit."""
    cmd = [args.command]
    if args.now:
        cmd.append("--now")
    cmd.append(service.UNIT_NAME)
    return service.systemctl(*cmd, check=False).returncode


def cmd_systemctl(args) -> int:
    """Passthrough start/stop/restart/status."""
    return service.systemctl(args.command, service.UNIT_NAME, check=False).returncode


def cmd_logs(args) -> int:
    """Show the journal."""
    cmd = ["-n", str(args.lines)]
    if args.follow:
        cmd.append("-f")
    return service.journalctl(*cmd)


def cmd_plot(args) -> int:
    """Render the requested metrics to an HTML file."""
    # pylint: disable=import-outside-toplevel
    try:
        import plotly  # noqa: F401
        from plotly.subplots import make_subplots
    except ModuleNotFoundError:
        print_color("plotting requires the `plotly` python package", "❌")
        return 1
    from amd_debug.power_query import METRIC_BUILDERS, load_window, resample, to_si

    path = args.db or db.default_db_path()
    if not path.exists():
        print_color(f"database {path} does not exist", "❌")
        return 1

    now = time.time()
    try:
        end_ts = parse_when(args.until, now=now, default=now)
        start_ts = parse_when(args.since, now=end_ts, default=end_ts - 600)
    except ValueError as e:
        print_color(str(e), "❌")
        return 1

    conn = db.connect(path)
    raw = load_window(conn, start_ts, end_ts, rail_patterns=args.rails or None)
    if raw.empty:
        print_color(
            f"no samples in window {start_ts:.0f}..{end_ts:.0f} for the chosen rails",
            "❌",
        )
        return 1
    df = to_si(raw)
    if args.resample:
        df = resample(df, args.resample)

    figures = []
    for m in args.metrics or ["power"]:
        title, builder = METRIC_BUILDERS[m]
        fig = builder(df, unit=args.energy_unit) if m == "energy" else builder(df)
        figures.append((title, fig))

    if len(figures) == 1:
        figures[0][1].write_html(str(args.output), include_plotlyjs="cdn")
    else:
        combined = make_subplots(
            rows=len(figures),
            cols=1,
            vertical_spacing=0.08,
            subplot_titles=[t for t, _ in figures],
        )
        for i, (_, fig) in enumerate(figures, start=1):
            for tr in fig.data:
                combined.add_trace(tr, row=i, col=1)
        combined.update_layout(height=350 * len(figures), hovermode="x unified")
        combined.write_html(str(args.output), include_plotlyjs="cdn")

    print_color(f"wrote {args.output}", "🐧")
    return 0


def main() -> int | None:
    """Main function"""
    args = parse_args()
    if args.version:
        print(version())
        return None
    if args.tool_debug:
        logging.basicConfig(level=logging.DEBUG)

    handlers = {
        "run": cmd_run,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "enable": cmd_enable_disable,
        "disable": cmd_enable_disable,
        "logs": cmd_logs,
        "plot": cmd_plot,
    }
    for verb in SYSTEMCTL_VERBS:
        handlers[verb] = cmd_systemctl

    if args.command is None:
        parse_args(["--help"])
        return 1
    ret = handlers[args.command](args)
    return ret or None


if __name__ == "__main__":
    sys.exit(main())
