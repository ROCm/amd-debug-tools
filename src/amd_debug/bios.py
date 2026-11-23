# SPDX-License-Identifier: MIT
"""s2idle analysis tool"""

from __future__ import annotations

import argparse
import re
import sys

from amd_debug.acpi import AcpicaTracer
from amd_debug.common import (
    AmdTool,
    fatal_error,
    get_log_priority,
    minimum_kernel,
    print_color,
    relaunch_sudo,
    show_log_info,
    version,
)
from amd_debug.kernel import get_kernel_log, sscanf_bios_args


class AmdBios(AmdTool):
    """
    AmdBios is a class which fetches the BIOS events from kernel logs.
    """

    def __init__(self, inf, debug):
        log_prefix = "bios" if debug else None
        super().__init__(log_prefix)
        self.kernel_log = get_kernel_log(inf)

    def set_tracing(self, enable):
        """Run the action"""
        relaunch_sudo()

        if not minimum_kernel(6, 16):
            print_color(
                "Support for BIOS debug logging was merged in mainline 6.16, "
                "this tool may not work correctly unless support is manually "
                "backported",
                "🚦",
            )

        tracer = AcpicaTracer()
        ret = tracer.trace_bios() if enable else tracer.disable()
        if ret:
            action = "enabled" if enable else "disabled"
            print_color(f"Set BIOS tracing to {action}", "✅")
        else:
            fatal_error(
                "BIOS tracing not supported, please check your kernel for CONFIG_ACPI_DEBUG"
            )

        return True

    def _analyze_kernel_log_line(self, line, priority):
        """Analyze a line from the kernel log"""
        bios_args = sscanf_bios_args(line)
        if bios_args:
            if isinstance(bios_args, str):
                print_color(bios_args, "🖴")
            else:
                return
        else:
            # strip timestamp
            t = re.sub(r"^\[\s*\d+\.\d+\]", "", line).strip()
            print_color(t, get_log_priority(priority))

    def run(self):
        """Exfiltrate from the kernel log"""
        self.kernel_log.process_callback(self._analyze_kernel_log_line)
        return True


def parse_args():
    """Parse command line arguments

    Without any arguments the tool starts the TUI.
    """
    parser = argparse.ArgumentParser(
        description="Parse a combined kernel/BIOS log.",
        epilog="Without any command the TUI is started, which provides a "
        "graphical front-end for BIOS AML tracing and log parsing.\n"
        "The 'trace' command enables or disables BIOS AML debug logging.\n"
        "The 'parse' command parses a kernel log that contains BIOS logs.\n"
        "To use non-interactively, please populate all optional arguments.",
    )
    subparsers = parser.add_subparsers(
        help="Possible commands (default: tui)", dest="command"
    )
    parse_cmd = subparsers.add_parser(
        "parse", help="Parse log for kernel and BIOS messages"
    )
    parse_cmd.add_argument(
        "--input",
        help="Optional input file to parse",
    )
    parse_cmd.add_argument(
        "--tool-debug",
        action="store_true",
        help="Enable tool debug logging",
    )
    trace_cmd = subparsers.add_parser("trace", help="Enable or disable tracing")
    trace_cmd.add_argument(
        "--enable",
        action="store_true",
        help="Enable BIOS AML tracing",
    )
    trace_cmd.add_argument(
        "--disable",
        action="store_true",
        help="Disable BIOS AML tracing",
    )
    trace_cmd.add_argument(
        "--tool-debug",
        action="store_true",
        help="Enable tool debug logging",
    )

    # 'tui' command
    tui_cmd = subparsers.add_parser(
        "tui", help="Launch interactive Textual TUI (default)"
    )
    tui_cmd.add_argument(
        "--tool-debug",
        action="store_true",
        help="Enable tool debug logging",
    )

    parser.add_argument(
        "--version", action="store_true", help="Show version information"
    )

    if len(sys.argv) == 1:
        args = parser.parse_args(["tui"])
        return args

    args = parser.parse_args()
    if args.command is None and not args.version:
        args.command = "tui"

    if args.command == "trace":
        if args.enable and args.disable:
            sys.exit("can't set both enable and disable")
        if not args.enable and not args.disable:
            sys.exit("must set either enable or disable")

    return args


def run_tui(args) -> bool:
    """Launch the Textual TUI"""
    from amd_debug.bios_tui import BiosApp  # pylint: disable=import-outside-toplevel

    app = BiosApp(
        tool_debug=getattr(args, "tool_debug", False),
    )
    app.run()
    return True


def main() -> None | int:
    """Main function"""
    args = parse_args()
    ret = False
    if args.command == "trace":
        app = AmdBios(None, args.tool_debug)
        ret = app.set_tracing(bool(args.enable))
    elif args.command == "parse":
        app = AmdBios(args.input, args.tool_debug)
        ret = app.run()
    elif args.command == "tui":
        ret = run_tui(args)
    elif args.version:
        print(version())
    show_log_info()
    if ret is False:
        return 1
    return
