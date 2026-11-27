# SPDX-License-Identifier: MIT
"""Textual TUI for amd-s2idle"""

from __future__ import annotations

import calendar
import os
import selectors
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)


def get_elevation_env_vars() -> list[str]:
    """Collect session environment variables to preserve across elevation"""
    env_vars = []
    for var in [
        "XDG_DATA_HOME",
        "AMD_S2IDLE_DB",
        "SSH_CLIENT",
        "SSH_CONNECTION",
        "SSH_TTY",
    ]:
        value = os.environ.get(var)
        if value:
            env_vars.append(f"{var}={value}")
    return env_vars


def build_elevated_cmd(
    base_cmd: list[str], tool: str = "pkexec", extra_env: list[str] | None = None
) -> list[str]:
    """Wrap command with pkexec or sudo preserving environment variables"""
    env_vars = get_elevation_env_vars()
    if extra_env:
        env_vars.extend(extra_env)
    if not any(v.startswith("PYTHONUNBUFFERED=") for v in env_vars):
        env_vars.append("PYTHONUNBUFFERED=1")
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


def _write_stream_bytes(target, data: bytes) -> None:
    """Write raw bytes to a text or binary output stream and flush immediately"""
    buffer = getattr(target, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        target.write(data.decode("utf-8", errors="replace"))
        target.flush()


def run_elevated_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an elevated command with terminal input access while cloning output.

    Inherits stdin directly so interactive authentication agents (such as
    pkexec's text agent or sudo) and interactive subcommands can read from the
    controlling terminal.  stdout and stderr are streamed live to the terminal
    and simultaneously captured so callers can clone them into the TUI
    execution log.
    """
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    with subprocess.Popen(
        cmd,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None
        assert proc.stderr is not None
        selector.register(
            proc.stdout, selectors.EVENT_READ, (sys.stdout, stdout_chunks)
        )
        selector.register(
            proc.stderr, selectors.EVENT_READ, (sys.stderr, stderr_chunks)
        )

        try:
            while selector.get_map():
                try:
                    events = selector.select()
                except KeyboardInterrupt:
                    if proc.poll() is not None:
                        break
                    continue
                for key, _ in events:
                    stream = key.fileobj
                    target, chunks = key.data
                    try:
                        data = os.read(stream.fileno(), 4096)
                    except OSError:
                        data = b""
                    if not data:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    chunks.append(data)
                    _write_stream_bytes(target, data)
        finally:
            selector.close()

        returncode = proc.wait()

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


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
                except Exception:  # noqa: BLE001
                    rendered = Text(line)
                self.app.call_from_thread(self.log_widget.write, rendered)

    def flush(self) -> None:
        if self._buffer:
            line = self._buffer.rstrip()
            self._buffer = ""
            if line:
                try:
                    rendered = Text.from_ansi(line)
                except Exception:  # noqa: BLE001
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

    BINDINGS: ClassVar[list[Binding]] = [
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

    BINDINGS: ClassVar[list[Binding]] = [
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
                yield Button("Close Dialog", variant="default", id="fs-close")
                yield Button("Open Report", variant="primary", id="fs-open")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fs-open":
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
