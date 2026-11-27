# SPDX-License-Identifier: MIT
"""Textual TUI for amd-bios"""

from __future__ import annotations

import os
import shutil
import sys
from contextlib import suppress
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widgets._option_list import Option

from amd_debug.bios import AmdBios
from amd_debug.common import is_root, running_ssh
from amd_debug.tui import (
    ErrorModal,
    build_elevated_cmd,
    capture_output_to_log,
    run_elevated_cmd,
)


def get_bios_cmd() -> list[str]:
    """Return command list to execute amd-bios CLI"""
    prog = sys.argv[0]
    if os.path.isabs(prog) and os.path.exists(prog):
        return [sys.executable, prog]
    elif os.path.exists(prog):
        return [sys.executable, os.path.abspath(prog)]
    which_prog = shutil.which(prog)
    if which_prog:
        return [which_prog]
    return [sys.executable, "-m", "amd_debug.bios"]


class BiosWelcomeScreen(ModalScreen[str]):
    """Welcome screen presented on TUI startup to choose an action"""

    CSS = """
    BiosWelcomeScreen {
        align: center middle;
    }

    BiosWelcomeScreen > .modal-dialog {
        width: 85;
        height: 13;
    }

    .welcome-title {
        text-style: bold;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }

    .welcome-subtitle {
        text-align: center;
        width: 100%;
        margin-bottom: 1;
        color: $text-muted;
    }

    OptionList {
        width: 100%;
    }
    """

    def __init__(self, app_name: str | None = None) -> None:
        super().__init__()
        self.app_name = app_name or "amd-bios"

    def on_mount(self) -> None:
        self.query_one("#welcome-options", OptionList).focus()

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(f"Welcome to {self.app_name}", classes="welcome-title")
            yield Label("What would you like to do?", classes="welcome-subtitle")
            yield OptionList(
                Option("Manage BIOS AML tracing", id="tab-trace"),
                Option("Parse a kernel log", id="tab-parse"),
                id="welcome-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss("tab-trace")


class BiosApp(App):
    TITLE = "amd-bios"
    SUB_TITLE = "BIOS AML Debug & Log Parser"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("c", "clear_log", "Clear Log"),
        Binding("t", "toggle_log", "Toggle Log"),
    ]

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-container {
        height: 1fr;
        layout: vertical;
    }

    #tab-content {
        height: 1fr;
    }

    Screen.log-visible #tab-content {
        height: auto;
    }

    .form-row {
        height: 3;
        layout: horizontal;
        align-vertical: middle;
    }

    .form-row-switch {
        height: 3;
        layout: horizontal;
        align-vertical: middle;
    }

    .form-label {
        width: 22;
        text-align: right;
        padding-right: 1;
    }

    .form-input {
        width: 1fr;
    }

    .form-select {
        width: 1fr;
    }

    .action-button-row {
        height: 3;
        margin-top: 1;
        layout: horizontal;
        align: center middle;
    }

    .action-button-row Button {
        margin-right: 2;
    }

    #output-container {
        height: 1fr;
        border: solid $accent;
        layout: vertical;
        padding: 0 1;
        display: none;
    }

    #output-container.show-log {
        display: block;
    }

    #output-header-row {
        height: 3;
        layout: horizontal;
        align-vertical: middle;
    }

    #output-title {
        width: 1fr;
        text-style: bold;
    }

    #output-log {
        height: 1fr;
        background: $surface;
        color: $text;
        overflow-y: scroll;
    }

    #status-bar {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding-left: 1;
    }

    .modal-dialog {
        padding: 1 2;
        width: 70;
        height: auto;
        border: thick $primary;
        background: $surface;
        align: center middle;
    }

    .modal-title {
        text-style: bold;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }

    .modal-message {
        width: 100%;
        text-align: center;
        margin-bottom: 1;
    }

    .modal-buttons {
        height: 3;
        layout: horizontal;
        align: center middle;
        margin-bottom: 1;
    }

    .modal-buttons Button {
        margin: 0 2;
    }
    """

    def __init__(
        self,
        tool_debug: bool = False,
        app_name: str | None = None,
    ):
        super().__init__()
        os.environ["AMD_BIOS_TUI"] = "1"
        self.tool_debug = tool_debug
        self.app_name = app_name or "amd-bios"
        self.title = self.app_name
        if shutil.which("pkexec") and not running_ssh():
            self.default_elevation = "pkexec"
        else:
            self.default_elevation = "sudo"
        self._is_busy = False

    def on_mount(self) -> None:
        self._welcome_prompt()
        self.log_message(f"[bold green]{self.app_name} TUI started.[/bold green]")
        if not is_root():
            self.log_message(
                f"[cyan]Running as unprivileged user. "
                f"{self.default_elevation} is required for elevated operations (trace).[/cyan]"
            )
        else:
            self.log_message("[cyan]Running as root.[/cyan]")

    def _welcome_prompt(self) -> None:
        """Show the welcome screen and switch to the selected tab"""
        welcome = BiosWelcomeScreen(self.app_name)
        self.push_screen(
            welcome,
            lambda tab_id: self._switch_to_tab(tab_id),
        )

    def _switch_to_tab(self, tab_id: str) -> None:
        """Switch the active tab to the given tab ID"""
        self.log_message(f"[cyan]Switching to tab: {tab_id}[/cyan]")
        try:
            tabbed = self.query_one("#tab-content", TabbedContent)
            tabbed.active = tab_id
        except Exception as e:  # noqa: BLE001
            self.log_message(f"[red]Failed to switch tab: {e}[/red]")

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main-container"):
            with TabbedContent(initial="tab-trace", id="tab-content"):
                with TabPane("Trace", id="tab-trace"), VerticalScroll():
                    with Horizontal(classes="form-row"):
                        yield Label("Action:", classes="form-label")
                        yield Select(
                            options=[
                                ("Enable", "enable"),
                                ("Disable", "disable"),
                            ],
                            value="enable",
                            id="trace-action",
                            classes="form-select",
                        )
                        yield Label(
                            "Enable or disable BIOS AML tracing.",
                            classes="form-label",
                        )
                    with Collapsible(title="Advanced", id="trace-advanced"), Horizontal(
                        classes="form-row-switch"
                    ):
                        yield Label("Tool Debug:", classes="form-label")
                        yield Switch(value=self.tool_debug, id="trace-tool-debug")
                        yield Label(
                            "Enable verbose tool debugging.",
                            classes="form-label",
                        )
                    with Horizontal(classes="action-button-row"):
                        yield Button(
                            "Run Trace",
                            variant="primary",
                            id="btn-run-trace",
                        )

                with TabPane("Parse", id="tab-parse"), VerticalScroll():
                    with Horizontal(classes="form-row"):
                        yield Label("Input File:", classes="form-label")
                        yield Input(
                            placeholder="Leave blank for current kernel log",
                            id="parse-input",
                            classes="form-input",
                        )
                        yield Label("Kernel log file to parse.", classes="form-label")
                    with Collapsible(title="Advanced", id="parse-advanced"), Horizontal(
                        classes="form-row-switch"
                    ):
                        yield Label("Tool Debug:", classes="form-label")
                        yield Switch(value=self.tool_debug, id="parse-tool-debug")
                        yield Label(
                            "Enable verbose tool debugging.",
                            classes="form-label",
                        )
                    with Horizontal(classes="action-button-row"):
                        yield Button(
                            "Parse Log",
                            variant="primary",
                            id="btn-run-parse",
                        )

            with Container(id="output-container"):
                with Horizontal(id="output-header-row"):
                    yield Label("Execution Log", id="output-title")
                    yield Button("Clear Log", variant="default", id="btn-clear-log")
                yield RichLog(id="output-log", highlight=True, markup=True, wrap=True)

            yield Label("Status: Ready", id="status-bar")

        yield Footer()

    def log_message(self, message: str) -> None:
        log_widget = self.query_one("#output-log", RichLog)
        try:
            rendered = Text.from_markup(message)
        except Exception:  # noqa: BLE001
            rendered = Text(message)
        log_widget.write(rendered)

    def set_status(self, text: str) -> None:
        self.query_one("#status-bar", Label).update(f"Status: {text}")

    def show_error_modal(self, title: str, message: str) -> None:
        """Show an error modal with a dismiss button"""
        modal = ErrorModal(title, message)
        self.push_screen(modal)

    def _log_subprocess_output(self, result) -> None:
        """Write subprocess stdout/stderr to the execution log"""
        try:
            log_widget = self.query_one("#output-log", RichLog)
        except Exception:  # noqa: BLE001
            return
        combined = []
        stdout = getattr(result, "stdout", None)
        if isinstance(stdout, str):
            combined.append(stdout)
        stderr = getattr(result, "stderr", None)
        if isinstance(stderr, str):
            combined.append(stderr)
        full_text = "\n".join(combined).strip()
        if not full_text:
            return
        for line in full_text.splitlines():
            line = line.rstrip()
            if line:
                try:
                    rendered = Text.from_ansi(line)
                except Exception:  # noqa: BLE001
                    rendered = Text(line)
                log_widget.write(rendered)

    def action_clear_log(self) -> None:
        self.query_one("#output-log", RichLog).clear()

    def action_toggle_log(self) -> None:
        container = self.query_one("#output-container")
        container.set_class(not container.has_class("show-log"), "show-log")

    def _ensure_log_shown(self) -> None:
        """Make sure the log panel is visible"""
        with suppress(Exception):
            container = self.query_one("#output-container")
            if not container.has_class("show-log"):
                container.add_class("show-log")

    def _set_busy(self, busy: bool, operation: str = "") -> None:
        self._is_busy = busy
        self.set_status(f"Running {operation}..." if busy else "Ready")
        for btn_id in [
            "#btn-run-trace",
            "#btn-run-parse",
        ]:
            with suppress(Exception):
                self.query_one(btn_id, Button).disabled = busy

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-clear-log":
            self.action_clear_log()
        elif button_id == "btn-run-trace":
            self.handle_run_trace()
        elif button_id == "btn-run-parse":
            self.handle_run_parse()

    # -- Trace command ----------------------------------------------------

    def handle_run_trace(self) -> None:
        if self._is_busy:
            return

        action = self.query_one("#trace-action", Select).value
        tool_debug = self.query_one("#trace-tool-debug", Switch).value

        if not is_root():
            # Build the elevated command
            base_cmd = get_bios_cmd() + ["trace", f"--{action}"]
            if tool_debug:
                base_cmd += ["--tool-debug"]

            full_cmd = build_elevated_cmd(
                base_cmd,
                tool=self.default_elevation,
                extra_env=["AMD_BIOS_TUI=1"],
            )
            with self.suspend():
                result = run_elevated_cmd(full_cmd)
                ret = result.returncode
            self._ensure_log_shown()
            self._log_subprocess_output(result)
            if ret == 0:
                self.log_message("[green]Trace command completed successfully.[/green]")
            else:
                self.log_message(
                    f"[bold red]Trace command failed (exit code {ret}).[/bold red]"
                )
                self.call_later(
                    self.show_error_modal,
                    "Trace failed",
                    f"Trace command failed with exit code {ret}. "
                    "Check execution log for details.",
                )
            return

        # Already running as root
        self._set_busy(True, "Trace")
        self.run_worker(
            lambda: self._worker_trace(action, tool_debug),
            thread=True,
            exclusive=True,
        )

    def _worker_trace(self, action, tool_debug):
        log_widget = self.query_one("#output-log", RichLog)
        try:
            with capture_output_to_log(self, log_widget, self.set_status):
                app = AmdBios(None, tool_debug)
                app.set_tracing(action == "enable")
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(
                self.log_message, f"[bold red]Error running trace: {e}[/bold red]"
            )
        finally:
            self.call_from_thread(self._set_busy, False)

    # -- Parse command (No root/elevation needed) -----------------------

    def handle_run_parse(self) -> None:
        if self._is_busy:
            return

        input_file = self.query_one("#parse-input", Input).value.strip()
        tool_debug = self.query_one("#parse-tool-debug", Switch).value

        self._set_busy(True, "Parse")
        self.run_worker(
            lambda: self._worker_parse(input_file, tool_debug),
            thread=True,
            exclusive=True,
        )

    def _worker_parse(self, input_file, tool_debug):
        log_widget = self.query_one("#output-log", RichLog)
        try:
            with capture_output_to_log(self, log_widget, self.set_status):
                app = AmdBios(input_file or None, tool_debug)
                app.run()
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(
                self.log_message, f"[bold red]Error parsing log: {e}[/bold red]"
            )
        finally:
            self.call_from_thread(self._set_busy, False)
