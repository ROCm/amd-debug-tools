# SPDX-License-Identifier: MIT
"""Textual TUI for amd-s2idle"""

import calendar
import json
import os
import sys
import queue
import shutil
import logging
import subprocess
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta
from typing import List, Optional

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
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widgets._option_list import Option

from amd_debug.common import (
    is_root,
    running_ssh,
)
from amd_debug.database import SleepDatabase
from amd_debug.installer import Installer
from amd_debug.prereq import get_validator
from amd_debug.s2idle import (
    Defaults,
    Headers,
    get_report_file,
    get_report_format,
)
from amd_debug.sleep_report import SleepReport
from amd_debug.triage import (
    CYCLE_DURATION,
    CYCLE_WAIT,
    INSTRUCTIONS,
    STAGE_TITLES,
    Stage,
    Status,
    Triage,
    format_ts,
    tool_name,
)
from amd_debug.validator import SleepValidator


def get_s2idle_cmd() -> List[str]:
    """Return command list to execute amd-s2idle CLI"""
    prog = sys.argv[0]
    if os.path.isabs(prog) and os.path.exists(prog):
        return [sys.executable, prog]
    elif os.path.exists(prog):
        return [sys.executable, os.path.abspath(prog)]
    which_prog = shutil.which(prog)
    if which_prog:
        return [which_prog]
    return [sys.executable, "-m", "amd_debug.s2idle"]


def get_elevation_env_vars() -> List[str]:
    """Collect session environment variables to preserve across elevation"""
    env_vars = []
    for var in [
        "XDG_DATA_HOME",
        "AMD_S2IDLE_DB",
        "SSH_CLIENT",
        "SSH_TTY",
    ]:
        value = os.environ.get(var)
        if value:
            env_vars.append(f"{var}={value}")
    return env_vars


def build_elevated_cmd(
    base_cmd: List[str], tool: str = "pkexec", extra_env: Optional[List[str]] = None
) -> List[str]:
    """Wrap command with pkexec or sudo preserving environment variables"""
    env_vars = get_elevation_env_vars()
    if extra_env:
        env_vars.extend(extra_env)
    if tool == "pkexec":
        import pwd

        uid = os.getuid()
        gid = os.getgid()
        try:
            user = pwd.getpwuid(uid).pw_name
        except (KeyError, ValueError, ImportError):
            user = os.environ.get("USER", "")
        if user:
            env_vars.append(f"SUDO_USER={user}")
        env_vars.append(f"SUDO_UID={uid}")
        env_vars.append(f"SUDO_GID={gid}")

        return ["pkexec", "env"] + env_vars + base_cmd
    else:
        # sudo
        preserve_flags = [f"--preserve-env={var.split('=')[0]}" for var in env_vars]
        return ["sudo"] + preserve_flags + ["env"] + env_vars + base_cmd


class LogStream:
    """Stream wrapper to capture stdout/stderr and write to Textual RichLog"""

    def __init__(self, app: App, log_widget: RichLog, status_callback=None):
        self.app = app
        self.log_widget = log_widget
        self.status_callback = status_callback
        self._buffer = ""

    def write(self, text: str) -> None:
        if not text:
            return
        if "\r" in text:
            parts = text.split("\r")
            for p in parts[:-1]:
                line = p.strip()
                if line and self.status_callback:
                    self.app.call_from_thread(self.status_callback, line)
            text = parts[-1]

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                try:
                    rendered = Text.from_ansi(line)
                except Exception:
                    rendered = Text(line)
                self.app.call_from_thread(self.log_widget.write, rendered)

    def flush(self) -> None:
        if self._buffer:
            line = self._buffer.rstrip()
            self._buffer = ""
            if line:
                try:
                    rendered = Text.from_ansi(line)
                except Exception:
                    rendered = Text(line)
                self.app.call_from_thread(self.log_widget.write, rendered)


@contextmanager
def capture_output_to_log(app: App, log_widget: RichLog, status_callback=None):
    """Context manager to route sys.stdout and sys.stderr to the TUI RichLog"""
    stream = LogStream(app, log_widget, status_callback)
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = stream
    sys.stderr = stream
    try:
        yield stream
    finally:
        stream.flush()
        sys.stdout = old_stdout
        sys.stderr = old_stderr


class ConfirmModal(ModalScreen[bool]):
    """Modal dialog asking for user confirmation (Yes/No)"""

    CSS = """
    ConfirmModal {
        align: center middle;
    }
    """

    BINDINGS = [
        Binding("y", "confirm(True)", "Yes"),
        Binding("n", "confirm(False)", "No"),
        Binding("escape", "confirm(False)", "Cancel"),
    ]

    def __init__(self, message: str, title: str = "Confirmation"):
        super().__init__()
        self.message = message
        self.dialog_title = title

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(self.dialog_title, classes="modal-title")
            yield Label(self.message, classes="modal-message")
            with Horizontal(classes="modal-buttons"):
                yield Button("Yes", variant="primary", id="btn-yes")
                yield Button("No", variant="default", id="btn-no")

    def action_confirm(self, result: bool) -> None:
        self.dismiss(result)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-yes":
            self.dismiss(True)
        else:
            self.dismiss(False)


class AskQuestionModal(ModalScreen[str]):
    """Modal dialog for interactive triage questions and user input"""

    CSS = """
    AskQuestionModal {
        align: center middle;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_default", "Cancel"),
    ]

    def __init__(self, prompt: str, default: str = ""):
        super().__init__()
        self.prompt = prompt
        self.default_val = default
        self.is_yn = any(
            x in prompt.lower() for x in ["(y/n)", "(yes/no)", "y/n?", "y/n"]
        )

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label("Question", classes="modal-title")
            yield Label(self.prompt, classes="modal-message")
            if self.is_yn:
                with Horizontal(classes="modal-buttons"):
                    yield Button("Yes (y)", variant="primary", id="btn-yn-yes")
                    yield Button("No (n)", variant="error", id="btn-yn-no")
            with Horizontal(classes="modal-input-row"):
                yield Input(
                    value=self.default_val,
                    placeholder="Type response and press Enter...",
                    id="modal-input",
                )
                yield Button("Submit", variant="success", id="btn-submit")

    def on_mount(self) -> None:
        if not self.is_yn:
            self.query_one("#modal-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-yn-yes":
            self.dismiss("y")
        elif event.button.id == "btn-yn-no":
            self.dismiss("n")
        elif event.button.id == "btn-submit":
            val = self.query_one("#modal-input", Input).value.strip()
            self.dismiss(val if val else self.default_val)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        self.dismiss(val if val else self.default_val)

    def action_dismiss_default(self) -> None:
        self.dismiss(self.default_val)


class DatePickerModal(ModalScreen):
    """Modal screen with a simple date picker (month/day/year selects)"""

    CSS = """
    DatePickerModal {
        align: center middle;
        background: $surface;
    }
    .dp-row {
        width: 100%;
        height: 3;
    }
    .dp-select {
        width: 1fr;
    }
    """

    def __init__(self, initial_date):
        super().__init__()
        self.initial_date = initial_date

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label("Select Date", classes="modal-title")
            with Horizontal(classes="dp-row"):
                yield Label("Month:", classes="form-label")
                months = [(calendar.month_name[m], m) for m in range(1, 13)]
                yield Select(
                    options=months,
                    value=self.initial_date.month,
                    id="dp-month",
                    classes="dp-select",
                )
            with Horizontal(classes="dp-row"):
                yield Label("Day:", classes="form-label")
                days = [(f"{d:02d}", d) for d in range(1, 32)]
                yield Select(
                    options=days,
                    value=self.initial_date.day,
                    id="dp-day",
                    classes="dp-select",
                )
            with Horizontal(classes="dp-row"):
                yield Label("Year:", classes="form-label")
                years = [(str(y), y) for y in range(2020, 2035)]
                yield Select(
                    options=years,
                    value=self.initial_date.year,
                    id="dp-year",
                    classes="dp-select",
                )
            yield Label("", id="dp-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", variant="default", id="dp-cancel")
                yield Button("OK", variant="primary", id="dp-ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dp-cancel":
            self.dismiss(None)
        elif event.button.id == "dp-ok":
            month = self.query_one("#dp-month", Select).value
            day = self.query_one("#dp-day", Select).value
            year = self.query_one("#dp-year", Select).value
            error_lbl = self.query_one("#dp-error", Label)
            if (
                month is None
                or day is None
                or year is None
                or month == Select.BLANK
                or day == Select.BLANK
                or year == Select.BLANK
            ):
                error_lbl.update(
                    "[bold red]Please select a valid month, day, and year.[/bold red]"
                )
                return
            try:
                selected_date = date(int(year), int(month), int(day))
            except (ValueError, TypeError) as e:
                error_lbl.update(f"[bold red]Invalid date: {e}[/bold red]")
                return
            self.dismiss(selected_date)


class FileSavedModal(ModalScreen):
    """Modal shown when a report or test completes to show the generated file"""

    CSS = """
    FileSavedModal {
        align: center middle;
        background: $surface;
    }
    .file-path {
        width: 100%;
        padding: 1 2;
        margin: 1 0;
        height: auto;
        background: $boost;
        content-align: center middle;
    }
    """

    def __init__(self, fname: str, fmt: str, label: str):
        super().__init__()
        self.fname = fname
        self.fmt = fmt
        self.label = label

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(f"{self.label} Complete", classes="modal-title")
            yield Label("Saved to:", classes="modal-message")
            yield Static(self.fname, classes="file-path")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", variant="default", id="fs-cancel")
                yield Button("OK", variant="primary", id="fs-ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in ("fs-ok", "fs-open"):
            import subprocess

            subprocess.Popen(
                ["xdg-open", self.fname],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        self.dismiss(None)


class ErrorModal(ModalScreen):
    """Modal shown when triage or other operation fails"""

    CSS = """
    ErrorModal {
        align: center middle;
        background: $surface;
    }
    .error-message {
        width: 100%;
        padding: 1 2;
        margin: 1 0;
        height: auto;
        color: $error;
        content-align: center middle;
    }
    """

    def __init__(self, title: str, message: str):
        super().__init__()
        self.title_text = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(self.title_text, classes="modal-title")
            yield Label(self.message, classes="error-message")
            with Horizontal(classes="modal-buttons"):
                yield Button("OK", variant="primary", id="err-ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "err-ok":
            self.dismiss(None)


class WelcomeScreen(ModalScreen[str]):
    """Welcome screen presented on TUI startup to choose an action"""

    CSS = """
    WelcomeScreen {
        align: center middle;
    }

    WelcomeScreen > .modal-dialog {
        width: 85;
        height: 15;
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

    def __init__(self, app_name: Optional[str] = None) -> None:
        super().__init__()
        self.app_name = app_name or tool_name()

    def on_mount(self) -> None:
        self.query_one("#welcome-options", OptionList).focus()

    def focus_left(self) -> None:
        self.focus_previous()

    def focus_right(self) -> None:
        self.focus_next()

    def compose(self) -> ComposeResult:
        with Container(classes="modal-dialog"):
            yield Label(f"Welcome to {self.app_name}", classes="welcome-title")
            yield Label("What would you like to do?", classes="welcome-subtitle")
            yield OptionList(
                Option("Diagnose a s2idle issue", id="tab-triage"),
                Option("Stress test s2idle", id="tab-test"),
                Option("Collect previous reports", id="tab-report"),
                id="welcome-options",
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss("tab-triage")


class S2idleApp(App):
    TITLE = "amd-s2idle"
    SUB_TITLE = "Linux s2idle Debug & Analysis Tool"
    BINDINGS = [
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

    .modal-input-row {
        height: 3;
        layout: horizontal;
        align-vertical: middle;
    }

    .modal-input-row Input {
        width: 1fr;
        margin-right: 1;
    }

    #triage-status-info {
        padding: 1;
        border: dashed $secondary;
        margin-bottom: 1;
        height: auto;
    }
    """

    def __init__(
        self,
        tool_debug: bool = False,
        app_name: Optional[str] = None,
    ):
        super().__init__()
        os.environ["AMD_S2IDLE_TUI"] = "1"
        self.tool_debug = tool_debug
        self.app_name = app_name or tool_name()
        self.title = self.app_name
        if shutil.which("pkexec") and not running_ssh():
            self.default_elevation = "pkexec"
        else:
            self.default_elevation = "sudo"
        self._is_busy = False

    def on_mount(self) -> None:
        self.refresh_triage_status()
        self._welcome_prompt()
        self.log_message(f"[bold green]{self.app_name} TUI started.[/bold green]")
        if not is_root():
            self.log_message(
                "[cyan]Running as unprivileged user. "
                "pkexec is required for elevated operations (test and triage).[/cyan]"
            )
        else:
            self.log_message("[cyan]Running as root.[/cyan]")

    def _welcome_prompt(self) -> None:
        """Show the welcome screen and switch to the selected tab"""
        welcome = WelcomeScreen(self.app_name)
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
        except Exception as e:
            self.log_message(f"[red]Failed to switch tab: {e}[/red]")

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main-container"):
            with TabbedContent(initial="tab-triage", id="tab-content"):
                with TabPane("Triage", id="tab-triage"):
                    with VerticalScroll():
                        yield Static(id="triage-status-info")
                        with Horizontal(classes="form-row"):
                            yield Label("Format:", classes="form-label")
                            triage_fmts = [
                                ("txt", "txt"),
                                ("md", "md"),
                                ("html", "html"),
                            ]
                            yield Select(
                                options=triage_fmts,
                                value="html" if not running_ssh() else "txt",
                                id="triage-format",
                                classes="form-select",
                            )
                            yield Label("Report File:", classes="form-label")
                            yield Input(
                                placeholder="Leave blank for auto-generated file",
                                id="triage-report-file",
                                classes="form-input",
                            )
                        with Collapsible(title="Advanced", id="triage-advanced"):
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Restart Triage:", classes="form-label")
                                yield Switch(value=False, id="triage-restart")
                                yield Label(
                                    "Restart from the beginning of the guided session.",
                                    classes="form-label",
                                )
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Tool Debug:", classes="form-label")
                                yield Switch(
                                    value=self.tool_debug, id="triage-tool-debug"
                                )
                                yield Label(
                                    "Enable verbose tool debugging.",
                                    classes="form-label",
                                )
                        with Horizontal(classes="action-button-row"):
                            yield Button(
                                "Export Triage",
                                variant="default",
                                id="btn-export-triage",
                            )
                            yield Button(
                                "Start Triage",
                                variant="primary",
                                id="btn-run-triage",
                            )
                            yield Button(
                                "Abandon Triage",
                                variant="error",
                                id="btn-abandon-triage",
                            )

                with TabPane("Test", id="tab-test"):
                    with VerticalScroll():
                        with Horizontal(classes="form-row"):
                            yield Label("Duration (s):", classes="form-label")
                            yield Input(
                                value=str(Defaults.duration),
                                id="test-duration",
                                classes="form-input",
                            )
                            yield Label(
                                "Length of each suspend/resume cycle.",
                                classes="form-label",
                            )
                        with Horizontal(classes="form-row"):
                            yield Label("Wait (s):", classes="form-label")
                            yield Input(
                                value=str(Defaults.wait),
                                id="test-wait",
                                classes="form-input",
                            )
                            yield Label(
                                "Pause between suspend cycles.", classes="form-label"
                            )
                        with Horizontal(classes="form-row"):
                            yield Label("Count:", classes="form-label")
                            yield Input(
                                value=str(Defaults.count),
                                id="test-count",
                                classes="form-input",
                            )
                            yield Label(
                                "Number of suspend cycles.", classes="form-label"
                            )
                        with Horizontal(classes="form-row"):
                            yield Label("Format:", classes="form-label")
                            fmt_opts = [
                                (c, c) for c in Defaults.format_choices if c != "stdout"
                            ]
                            yield Select(
                                options=fmt_opts,
                                value=get_report_format(),
                                id="test-format",
                                classes="form-select",
                            )
                            yield Label("Output report format.", classes="form-label")
                        with Horizontal(classes="form-row"):
                            yield Label("Report File:", classes="form-label")
                            yield Input(
                                placeholder="Leave blank for auto-generated file",
                                id="test-report-file",
                                classes="form-input",
                            )
                            yield Label(
                                "Save path (auto if empty).", classes="form-label"
                            )
                        with Collapsible(title="Advanced", id="test-advanced"):
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Random:", classes="form-label")
                                yield Switch(value=False, id="test-random")
                                yield Label(
                                    "Randomize suspend timing.", classes="form-label"
                                )
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Logind:", classes="form-label")
                                yield Switch(value=False, id="test-logind")
                                yield Label(
                                    "Use logind to manage suspend.",
                                    classes="form-label",
                                )
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Force:", classes="form-label")
                                yield Switch(value=False, id="test-force")
                                yield Label(
                                    "Run test even if prerequisite checks fail.",
                                    classes="form-label",
                                )
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Tool Debug:", classes="form-label")
                                yield Switch(
                                    value=self.tool_debug, id="test-tool-debug"
                                )
                                yield Label(
                                    "Enable verbose tool debugging.",
                                    classes="form-label",
                                )
                            with Horizontal(classes="form-row-switch"):
                                yield Label("BIOS Debug:", classes="form-label")
                                yield Switch(value=False, id="test-bios-debug")
                                yield Label(
                                    "Enable BIOS-level debug output.",
                                    classes="form-label",
                                )
                        with Horizontal(classes="action-button-row"):
                            yield Button(
                                "Run Suspend Test",
                                variant="primary",
                                id="btn-run-test",
                            )

                with TabPane("Report", id="tab-report"):
                    with VerticalScroll():
                        with Horizontal(classes="form-row"):
                            yield Label("Since:", classes="form-label")
                            yield Input(
                                value=Defaults.since.strftime("%Y-%m-%d"),
                                id="report-since",
                                classes="form-input",
                                placeholder="YYYY-MM-DD",
                            )
                            yield Button("📅", variant="default", id="btn-since-picker")
                            yield Label(
                                "Include entries after this datetime.",
                                classes="form-label",
                            )
                        with Horizontal(classes="form-row"):
                            yield Label("Until:", classes="form-label")
                            yield Input(
                                value=Defaults.until.strftime("%Y-%m-%d"),
                                id="report-until",
                                classes="form-input",
                                placeholder="YYYY-MM-DD",
                            )
                            yield Button("📅", variant="default", id="btn-until-picker")
                            yield Label(
                                "Include entries before this datetime.",
                                classes="form-label",
                            )
                        with Horizontal(classes="form-row"):
                            yield Label("Format:", classes="form-label")
                            fmt_opts = [
                                (c, c) for c in Defaults.format_choices if c != "stdout"
                            ]
                            yield Select(
                                options=fmt_opts,
                                value=get_report_format(),
                                id="report-format",
                                classes="form-select",
                            )
                            yield Label("Output report format.", classes="form-label")
                        with Horizontal(classes="form-row"):
                            yield Label("Report File:", classes="form-label")
                            yield Input(
                                placeholder="Leave blank for auto-generated file",
                                id="report-report-file",
                                classes="form-input",
                            )
                            yield Label(
                                "Save path (auto if empty).", classes="form-label"
                            )
                        with Collapsible(title="Advanced", id="report-advanced"):
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Report Debug:", classes="form-label")
                                yield Switch(value=True, id="report-report-debug")
                                yield Label(
                                    "Include detailed report sections.",
                                    classes="form-label",
                                )
                            with Horizontal(classes="form-row-switch"):
                                yield Label("Tool Debug:", classes="form-label")
                                yield Switch(
                                    value=self.tool_debug, id="report-tool-debug"
                                )
                                yield Label(
                                    "Enable verbose tool debugging.",
                                    classes="form-label",
                                )
                        with Horizontal(classes="action-button-row"):
                            yield Button(
                                "Generate Report",
                                variant="primary",
                                id="btn-run-report",
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
        except Exception:
            rendered = Text(message)
        log_widget.write(rendered)

    def set_status(self, text: str) -> None:
        self.query_one("#status-bar", Label).update(f"Status: {text}")

    def show_file_notification(self, fname: str, fmt: str, label: str) -> None:
        """Show a modal with the generated file path and an open button"""
        if not fname:
            return

        modal = FileSavedModal(fname, fmt, label)
        self.push_screen(modal)

    def show_error_modal(self, title: str, message: str) -> None:
        """Show an error modal with a dismiss button"""

        def on_dismiss(_event: Button.Pressed) -> None:
            self.dismiss(None)

        modal = ErrorModal(title, message)
        self.push_screen(modal)

    def _log_subprocess_output(self, result) -> None:
        """Write subprocess stdout/stderr to the execution log"""
        try:
            log_widget = self.query_one("#output-log", RichLog)
        except Exception:
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
                except Exception:
                    rendered = Text(line)
                log_widget.write(rendered)

    def action_clear_log(self) -> None:
        self.query_one("#output-log", RichLog).clear()

    def action_toggle_log(self) -> None:
        container = self.query_one("#output-container")
        container.set_class(not container.has_class("show-log"), "show-log")

    def _ensure_log_shown(self) -> None:
        """Make sure the log panel is visible"""
        try:
            container = self.query_one("#output-container")
            if not container.has_class("show-log"):
                container.add_class("show-log")
        except Exception:
            pass  # May fail in test environment

    def refresh_triage_status(self) -> None:
        static = self.query_one("#triage-status-info", Static)
        export_btn = self.query_one("#btn-export-triage", Button)
        run_btn = self.query_one("#btn-run-triage", Button)
        abandon_btn = self.query_one("#btn-abandon-triage", Button)
        try:
            db = SleepDatabase()
            session = db.triage_get()
            if session:
                stage = session.get("stage", "Unknown")
                status = session.get("status", "Unknown")
                started = format_ts(session.get("started", ""))
                stage_title = STAGE_TITLES.get(stage, stage)
                static.update(
                    f"Active Triage Session:\n"
                    f"  • Stage: {stage_title} ({stage})\n"
                    f"  • Status: {status}\n"
                    f"  • Started: {started}\n"
                    f"Click 'Resume Triage' to continue this session."
                )
                export_btn.display = True
                export_btn.visible = True
                run_btn.label = "Resume Triage"
                abandon_btn.display = True
                abandon_btn.visible = True
            else:
                static.update(
                    "No active triage session. "
                    "Click 'Start Triage' to begin a new guided triage session."
                )
                export_btn.display = False
                export_btn.visible = False
                run_btn.label = "Start Triage"
                abandon_btn.display = False
                abandon_btn.visible = False
        except Exception as e:
            static.update(f"Could not read triage database: {e}")

    def _set_busy(self, busy: bool, operation: str = "") -> None:
        self._is_busy = busy
        self.set_status(f"Running {operation}..." if busy else "Ready")
        for btn_id in [
            "#btn-run-test",
            "#btn-run-report",
            "#btn-run-triage",
            "#btn-abandon-triage",
        ]:
            try:
                self.query_one(btn_id, Button).disabled = busy
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-clear-log":
            self.action_clear_log()
        elif button_id == "btn-run-test":
            self.handle_run_test()
        elif button_id == "btn-run-report":
            self.handle_run_report()
        elif button_id == "btn-run-triage":
            self.handle_run_triage()
        elif button_id == "btn-abandon-triage":
            self.handle_abandon_triage()
        elif button_id == "btn-export-triage":
            self.handle_export_triage()
        elif button_id == "btn-since-picker":
            self._open_date_picker("report-since")
        elif button_id == "btn-until-picker":
            self._open_date_picker("report-until")

    def _open_date_picker(self, input_id: str) -> None:
        """Open the date picker modal for the given input field"""
        current_val = self.query_one(f"#{input_id}", Input).value.strip()
        try:
            initial = datetime.fromisoformat(current_val)
        except (ValueError, TypeError):
            initial = datetime.now()
        self.push_screen(DatePickerModal(initial), self._on_date_picked)
        self._current_picker_input = input_id

    def _on_date_picked(self, selected_date: date | None) -> None:
        """Callback when a date is selected from the date picker modal"""
        if hasattr(self, "_current_picker_input"):
            self.query_one(f"#{self._current_picker_input}", Input).value = (
                selected_date.strftime("%Y-%m-%d") if selected_date else ""
            )

    # -- Dialog helpers --------------------------------------------------

    def ask_confirm_thread(self, message: str) -> bool:
        """Ask confirmation modal from a worker thread"""
        res_queue: queue.Queue = queue.Queue()

        def _show():
            self.push_screen(ConfirmModal(message), lambda ans: res_queue.put(ans))

        self.call_from_thread(_show)
        return res_queue.get()

    def ask_question_thread(self, prompt: str, default: str = "") -> str:
        """Ask interactive question modal from a worker thread"""
        res_queue: queue.Queue = queue.Queue()

        def _show():
            self.push_screen(
                AskQuestionModal(prompt, default), lambda ans: res_queue.put(ans)
            )

        self.call_from_thread(_show)
        return res_queue.get()

    # -- Test command ----------------------------------------------------

    def handle_run_test(self) -> None:
        if self._is_busy:
            return

        try:
            duration = int(self.query_one("#test-duration", Input).value)
            wait = int(self.query_one("#test-wait", Input).value)
            count = int(self.query_one("#test-count", Input).value)
        except ValueError as e:
            self.log_message(
                f"[bold red]Invalid number in test parameters: {e}[/bold red]"
            )
            return

        fmt = self.query_one("#test-format", Select).value
        report_file_input = self.query_one("#test-report-file", Input).value.strip()
        fname = get_report_file(report_file_input, fmt)
        force = self.query_one("#test-force", Switch).value
        rand = self.query_one("#test-random", Switch).value
        logind = self.query_one("#test-logind", Switch).value
        tool_debug = self.query_one("#test-tool-debug", Switch).value
        bios_debug = self.query_one("#test-bios-debug", Switch).value

        if fname and os.path.exists(fname):

            def _after_confirm(confirmed: bool):
                if not confirmed:
                    self.log_message(
                        "[yellow]Suspend test cancelled (overwrite declined).[/yellow]"
                    )
                    return
                if fname and os.path.exists(fname):
                    try:
                        os.unlink(fname)
                    except OSError as e:
                        self.log_message(
                            f"[bold red]Failed to remove existing report file {fname}: {e}[/bold red]"
                        )
                        return
                self._dispatch_test(
                    duration,
                    wait,
                    count,
                    fmt,
                    fname,
                    force,
                    rand,
                    logind,
                    tool_debug,
                    bios_debug,
                )

            self.push_screen(
                ConfirmModal(f"Report file already exists: {fname}.\nOverwrite it?"),
                _after_confirm,
            )
        else:
            self._dispatch_test(
                duration,
                wait,
                count,
                fmt,
                fname,
                force,
                rand,
                logind,
                tool_debug,
                bios_debug,
            )

    def _dispatch_test(
        self,
        duration,
        wait,
        count,
        fmt,
        fname,
        force,
        rand,
        logind,
        tool_debug,
        bios_debug,
    ):
        if not is_root():
            # Build the elevated command
            base_cmd = get_s2idle_cmd() + [
                "test",
                "--duration",
                str(duration),
                "--wait",
                str(wait),
                "--count",
                str(count),
                "--format",
                fmt,
            ]
            if fname:
                base_cmd += ["--report-file", fname]
            if force:
                base_cmd += ["--force"]
            if rand:
                base_cmd += ["--random"]
            if logind:
                base_cmd += ["--logind"]
            if tool_debug:
                base_cmd += ["--tool-debug"]
            if bios_debug:
                base_cmd += ["--bios-debug"]

            full_cmd = build_elevated_cmd(
                base_cmd,
                tool=self.default_elevation,
                extra_env=["AMD_S2IDLE_TUI=1"],
            )
            elevation_ctx = (
                self.suspend()
                if self.default_elevation == "sudo" or running_ssh()
                else nullcontext()
            )
            with elevation_ctx:
                result = subprocess.run(full_cmd, capture_output=True, text=True)
                ret = result.returncode
            self._ensure_log_shown()
            self._log_subprocess_output(result)
            if ret == 0:
                self.log_message("[green]Test completed successfully.[/green]")
                if fname:
                    self.call_later(self.show_file_notification, fname, fmt, "Test")
            else:
                self.log_message(f"[bold red]Test failed (exit code {ret}).[/bold red]")
                self.call_later(
                    self.show_error_modal,
                    "Test failed",
                    f"Test cycle failed with exit code {ret}. Check execution log for details.",
                )
            return

        # Already running as root
        self._set_busy(True, "Suspend Test")
        self.run_worker(
            lambda: self._worker_test(
                duration,
                wait,
                count,
                fmt,
                fname,
                force,
                rand,
                logind,
                tool_debug,
                bios_debug,
            ),
            thread=True,
            exclusive=True,
        )

    def _worker_test(
        self,
        duration,
        wait,
        count,
        fmt,
        fname,
        force,
        rand,
        logind,
        tool_debug,
        bios_debug,
    ):
        log_widget = self.query_one("#output-log", RichLog)
        try:
            with capture_output_to_log(self, log_widget, self.set_status):
                # This worker is only called when running as root
                installer = Installer(tool_debug=tool_debug)
                installer.set_requirements("iasl", "ethtool", "edid-decode")
                if not installer.install_dependencies():
                    print("Failed to install dependencies")
                    return

                try:
                    validator = get_validator(tool_debug)
                    run = validator.run()
                except PermissionError as e:
                    print(f"Failed to run prerequisite check: {e}")
                    return

                validator.report()

                if run or force:
                    sv = SleepValidator(tool_debug=tool_debug, bios_debug=bios_debug)
                    total_seconds = (duration + wait) * count
                    until_time = datetime.now() + timedelta(seconds=total_seconds)
                    since_time = datetime.now()
                    sv.run(
                        duration=duration,
                        wait=wait,
                        count=count,
                        rand=rand,
                        logind=logind,
                    )
                    until_time = datetime.now()
                else:
                    since_time = None
                    until_time = None

                rep = SleepReport(
                    since=since_time,
                    until=until_time,
                    fname=fname,
                    fmt=fmt,
                    tool_debug=tool_debug,
                    report_debug=True,
                )
                rep.run()
                if fname:
                    print(f"Test completed. Report saved to: {fname}")
                    self.call_from_thread(
                        self.show_file_notification, fname, fmt, "Test"
                    )
        except Exception as e:
            self.call_from_thread(
                self.log_message, f"[bold red]Error running test: {e}[/bold red]"
            )
        finally:
            self.call_from_thread(self._set_busy, False)

    # -- Report command (No root/elevation needed) -----------------------

    def handle_run_report(self) -> None:
        if self._is_busy:
            return

        since_str = self.query_one("#report-since", Input).value.strip()
        until_str = self.query_one("#report-until", Input).value.strip()
        try:
            since = datetime.fromisoformat(since_str)
            until = datetime.fromisoformat(until_str)
        except ValueError as e:
            self.log_message(f"[bold red]Invalid date format: {e}[/bold red]")
            return

        fmt = self.query_one("#report-format", Select).value
        report_file_input = self.query_one("#report-report-file", Input).value.strip()
        fname = get_report_file(report_file_input, fmt)
        report_debug = self.query_one("#report-report-debug", Switch).value
        tool_debug = self.query_one("#report-tool-debug", Switch).value

        if fname and os.path.exists(fname):

            def _after_confirm(confirmed: bool):
                if not confirmed:
                    self.log_message(
                        "[yellow]Report generation cancelled (overwrite declined).[/yellow]"
                    )
                    return
                # Delete existing file so build_template can create it fresh
                try:
                    os.unlink(fname)
                except OSError as e:
                    self.call_from_thread(
                        self.log_message,
                        f"[bold red]Failed to remove existing report file {fname}: {e}[/bold red]",
                    )
                    return
                self._start_report_worker(
                    since, until, fname, fmt, tool_debug, report_debug
                )

            self.push_screen(
                ConfirmModal(f"Report file already exists: {fname}.\nOverwrite it?"),
                _after_confirm,
            )
        else:
            self._start_report_worker(
                since, until, fname, fmt, tool_debug, report_debug
            )

    def _start_report_worker(self, since, until, fname, fmt, tool_debug, report_debug):
        self._set_busy(True, "Report Generation")
        self.run_worker(
            lambda: self._worker_report(
                since, until, fname, fmt, tool_debug, report_debug
            ),
            thread=True,
            exclusive=True,
        )

    def _worker_report(self, since, until, fname, fmt, tool_debug, report_debug):
        log_widget = self.query_one("#output-log", RichLog)
        try:
            with capture_output_to_log(self, log_widget, self.set_status):
                rep = SleepReport(
                    since=since,
                    until=until,
                    fname=fname,
                    fmt=fmt,
                    tool_debug=tool_debug,
                    report_debug=report_debug,
                )
                rep.run()
                if fname:
                    print(f"Report generated: {fname}")
                    self.call_from_thread(
                        self.show_file_notification, fname, fmt, "Report"
                    )
        except Exception as e:
            self.call_from_thread(
                self.log_message, f"[bold red]Error generating report: {e}[/bold red]"
            )
        finally:
            self.call_from_thread(self._set_busy, False)

    # -- Triage command --------------------------------------------------

    def handle_run_triage(self) -> None:
        if self._is_busy:
            return

        fmt = self.query_one("#triage-format", Select).value
        report_file_input = self.query_one("#triage-report-file", Input).value.strip()
        if report_file_input:
            fname = os.path.abspath(report_file_input)
        else:
            fname = os.path.abspath(f"{self.app_name}-triage-{date.today()}.{fmt}")

        # Check if the file already exists and prompt to overwrite
        if os.path.lexists(fname):

            def _confirm_overwrite(confirmed: bool):
                if not confirmed:
                    self.log_message(
                        "[yellow]Triage cancelled — keeping existing report.[/yellow]"
                    )
                    return
                # Delete the old file so pick_filename() won't append -N
                try:
                    os.unlink(fname)
                except OSError:
                    pass
                self._do_run_triage(fmt, fname, restart, tool_debug)
                return

            restart = self.query_one("#triage-restart", Switch).value
            tool_debug = self.query_one("#triage-tool-debug", Switch).value
            self.push_screen(
                ConfirmModal(f"Report file already exists:\n{fname}\n\nOverwrite it?"),
                _confirm_overwrite,
            )
            return

        restart = self.query_one("#triage-restart", Switch).value
        tool_debug = self.query_one("#triage-tool-debug", Switch).value

        self._do_run_triage(fmt, fname, restart, tool_debug)

    def _do_run_triage(self, fmt, fname, restart, tool_debug):
        """Execute the triage flow (after overwrite confirmation)"""
        if not is_root():
            # Triage is an interactive guided interview requiring terminal prompts and root elevation
            base_cmd = get_s2idle_cmd() + [
                "triage",
                "--format",
                fmt,
                "--accepted",
            ]
            if fname:
                base_cmd += ["--report-file", fname]
            if restart:
                base_cmd += ["--restart"]
            if tool_debug:
                base_cmd += ["--tool-debug"]

            result_fd, result_file = tempfile.mkstemp(prefix="amd_s2idle_triage_")
            os.close(result_fd)
            try:
                os.chmod(result_file, 0o666)
            except OSError:
                pass
            extra_env = [
                "AMD_S2IDLE_TUI=1",
                f"AMD_S2IDLE_RESULT_FILE={result_file}",
            ]
            full_cmd = build_elevated_cmd(
                base_cmd,
                tool=self.default_elevation,
                extra_env=extra_env,
            )
            elevation_ctx = (
                self.suspend()
                if self.default_elevation == "sudo" or running_ssh()
                else nullcontext()
            )
            with elevation_ctx:
                result = subprocess.run(full_cmd, capture_output=True, text=True)
                ret = result.returncode
            self._ensure_log_shown()
            self._log_subprocess_output(result)
            self.refresh_triage_status()
            report_info = None
            if os.path.exists(result_file):
                try:
                    if os.path.getsize(result_file) > 0:
                        with open(result_file, "r", encoding="utf-8") as rf:
                            report_info = json.load(rf)
                except Exception:
                    report_info = None
                finally:
                    try:
                        os.unlink(result_file)
                    except OSError:
                        pass

            report_path = None
            report_fmt = fmt
            if (
                report_info
                and report_info.get("report_path")
                and os.path.exists(report_info["report_path"])
            ):
                report_path = report_info["report_path"]
                report_fmt = report_info.get("fmt", fmt)
            else:
                try:
                    db = SleepDatabase()
                    cur = db.db.cursor()
                    cur.execute(
                        "SELECT id FROM triage WHERE status='done' ORDER BY id DESC LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row:
                        events = db.triage_events(row[0])
                        for ev in reversed(events):
                            if ev[1] == "report" and ev[2] and os.path.exists(ev[2]):
                                report_path = ev[2]
                                report_fmt = ev[3] or fmt
                                break
                except Exception:
                    pass

            if ret == 0:
                if report_path:
                    self.log_message(
                        f"[green]Triage completed. Report saved to: {report_path}[/green]"
                    )
                    self.call_later(
                        self.show_file_notification,
                        report_path,
                        report_fmt,
                        "Triage",
                    )
                else:
                    self.log_message("[green]Triage step completed.[/green]")
                    try:
                        db = SleepDatabase()
                        active_session = db.triage_get()
                        if (
                            active_session
                            and active_session.get("status") == Status.CHANGE_PENDING
                        ):
                            stage = active_session.get("stage")
                            instructions = INSTRUCTIONS.get(
                                stage, "Please make the requested changes and reboot."
                            )
                            self.log_message(
                                f"[yellow]Triage paused: reboot required for {STAGE_TITLES.get(stage, stage)}.[/yellow]"
                            )
                            self.call_later(
                                self.show_error_modal,
                                f"Action Required: {STAGE_TITLES.get(stage, stage)}",
                                f"{instructions}\n\nAfter rebooting, launch {self.app_name} to continue triage.",
                            )
                    except Exception:
                        pass
                return

            self.log_message(
                f"[bold red]Triage did not complete successfully (exit code {ret}).[/bold red]"
            )
            self.call_later(
                self.show_error_modal,
                "Triage did not complete",
                f"Triage did not complete successfully (exit code {ret}). Check execution log for details.",
            )
            return

        # If already root, run inside the worker thread
        self._set_busy(True, "Triage")
        self.run_worker(
            lambda: self._worker_triage(fmt, fname, restart, tool_debug),
            thread=True,
            exclusive=True,
        )

    def _worker_triage(self, fmt, fname, restart, tool_debug):
        log_widget = self.query_one("#output-log", RichLog)
        try:
            with capture_output_to_log(self, log_widget, self.set_status):

                def triage_ask(prompt: str) -> str:
                    return self.ask_question_thread(prompt)

                triage_app = Triage(
                    fmt=fmt,
                    fname=fname,
                    debug=tool_debug,
                    accepted=True,
                    ask=triage_ask,
                    implicit=False,
                )
                if restart:
                    triage_app.db.triage_abandon_all()
                    print("Abandoned previous triage session(s)")

                triage_app.run()
                current_session = (
                    triage_app.db.triage_get(triage_app.session["id"])
                    if triage_app.session
                    else None
                )
                if triage_app.report_path:
                    self.call_from_thread(
                        self.log_message,
                        "[green]Triage completed successfully.[/green]",
                    )
                    print(f"Triage report written to: {triage_app.report_path}")
                    self.call_from_thread(
                        self.show_file_notification,
                        triage_app.report_path,
                        triage_app.fmt,
                        "Triage",
                    )
                elif triage_app.declined:
                    self.call_from_thread(
                        self.show_error_modal,
                        "Triage declined",
                        "You declined to continue with the triage session.",
                    )
                elif (
                    current_session
                    and current_session.get("status") == Status.CHANGE_PENDING
                ):
                    stage = current_session.get("stage")
                    instructions = INSTRUCTIONS.get(
                        stage, "Please make the requested changes and reboot."
                    )
                    self.call_from_thread(
                        self.log_message,
                        f"[yellow]Triage paused: reboot required for {STAGE_TITLES.get(stage, stage)}.[/yellow]",
                    )
                    self.call_from_thread(
                        self.show_error_modal,
                        f"Action Required: {STAGE_TITLES.get(stage, stage)}",
                        f"{instructions}\n\nAfter rebooting, launch {self.app_name} to continue triage.",
                    )
                else:
                    self.call_from_thread(
                        self.show_error_modal,
                        "Triage failed",
                        "Triage did not complete successfully. Check the log for details.",
                    )
        except Exception as e:
            self.call_from_thread(
                self.log_message, f"[bold red]Error during triage: {e}[/bold red]"
            )
        finally:
            self.call_from_thread(self.refresh_triage_status)
            self.call_from_thread(self._set_busy, False)

    def handle_abandon_triage(self) -> None:
        def _confirm_abandon(confirmed: bool):
            if not confirmed:
                return
            try:
                db = SleepDatabase()
                db.triage_abandon_all()
                self.log_message(
                    "[yellow]All active triage sessions have been abandoned.[/yellow]"
                )
                self.refresh_triage_status()
            except Exception as e:
                self.log_message(f"[bold red]Failed to abandon triage: {e}[/bold red]")

        self.push_screen(
            ConfirmModal(
                "Are you sure you want to abandon all triage sessions in progress?"
            ),
            _confirm_abandon,
        )

    def handle_export_triage(self) -> None:
        """Export an in-progress triage session in the selected format"""
        try:
            fmt = self.query_one("#triage-format", Select).value
            db = SleepDatabase()
            session = db.triage_get()
            if session is None:
                self.log_message(
                    "[yellow]No active triage session found to export.[/yellow]"
                )
                return

            # Generate export filename
            export_fname = f"triage-export-{session['id']}-{date.today()}.{fmt}"
            export_path = os.path.abspath(export_fname)

            # Use the Triage class to generate a properly formatted report
            triage_app = Triage(
                fmt=fmt,
                fname=export_path,
                debug=False,
                accepted=True,
                implicit=False,
            )
            # Load the existing session data so build_report can use it
            triage_app.session = session
            # build_report writes to the filename and returns it
            report_path = triage_app.build_report()
            if report_path is None:
                self.log_message(
                    "[bold red]Failed to build triage report for export.[/bold red]"
                )
                return

            self.log_message(
                f"[green]Triage session exported to: {report_path}[/green]"
            )
            self.show_file_notification(report_path, fmt, "Triage Export")
        except Exception as e:
            self.log_message(
                f"[bold red]Failed to export triage session: {e}[/bold red]"
            )
