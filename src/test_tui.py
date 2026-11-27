# SPDX-License-Identifier: MIT

"""
Unit tests for the Textual TUI in amd-debug-tools.
"""

import io
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from amd_debug.s2idle_tui import (
    S2idleApp,
    WelcomeScreen,
    get_s2idle_cmd,
)
from amd_debug.triage import Stage, Status
from amd_debug.tui import (
    AskQuestionModal,
    ConfirmModal,
    DatePickerModal,
    FileSavedModal,
    LogStream,
    build_elevated_cmd,
    default_elevation_tool,
    get_elevation_env_vars,
    run_elevated_cmd,
)


class TestHelpers(unittest.TestCase):
    """Test helper functions in tui module"""

    def test_get_s2idle_cmd_exists(self):
        cmd = get_s2idle_cmd()
        self.assertTrue(len(cmd) >= 1)

    @patch.dict(
        os.environ,
        {
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "XAUTHORITY": "/tmp/xauth",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus",
            "XDG_SESSION_TYPE": "wayland",
            "XDG_DATA_DIRS": "/usr/share",
            "KDE_SESSION_VERSION": "5",
            "XDG_DATA_HOME": "/tmp/data",
            "AMD_S2IDLE_DB": "/tmp/db.sqlite",
            "SSH_CLIENT": "1.2.3.4 5678 22",
        },
    )
    def test_get_elevation_env_vars(self):
        vars_list = get_elevation_env_vars()
        self.assertNotIn("DISPLAY=:0", vars_list)
        self.assertNotIn("WAYLAND_DISPLAY=wayland-0", vars_list)
        self.assertNotIn("XDG_RUNTIME_DIR=/run/user/1000", vars_list)
        self.assertNotIn("XAUTHORITY=/tmp/xauth", vars_list)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/bus", vars_list)
        self.assertNotIn("XDG_SESSION_TYPE=wayland", vars_list)
        self.assertNotIn("XDG_DATA_DIRS=/usr/share", vars_list)
        self.assertNotIn("KDE_SESSION_VERSION=5", vars_list)
        self.assertIn("XDG_DATA_HOME=/tmp/data", vars_list)
        self.assertIn("AMD_S2IDLE_DB=/tmp/db.sqlite", vars_list)
        self.assertIn("SSH_CLIENT=1.2.3.4 5678 22", vars_list)

    @patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/data"})
    def test_build_elevated_cmd_sudo(self):
        cmd = build_elevated_cmd(["test_cmd", "arg1"], "sudo")
        self.assertEqual(cmd[0], "sudo")
        self.assertIn("--preserve-env=XDG_DATA_HOME", cmd)
        self.assertEqual(cmd[-2:], ["test_cmd", "arg1"])

    @patch("pwd.getpwuid")
    @patch("os.getuid", return_value=1000)
    @patch("os.getgid", return_value=1000)
    @patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/data"})
    def test_build_elevated_cmd_pkexec(self, _mock_gid, _mock_uid, mock_pwd):
        mock_pwd.return_value.pw_name = "testuser"
        cmd = build_elevated_cmd(["test_cmd", "arg1"], "pkexec")
        self.assertEqual(cmd[0], "pkexec")
        self.assertEqual(cmd[1], "env")
        self.assertIn("XDG_DATA_HOME=/tmp/data", cmd)
        self.assertIn("SUDO_USER=testuser", cmd)
        self.assertIn("SUDO_UID=1000", cmd)
        self.assertIn("SUDO_GID=1000", cmd)
        self.assertIn("PYTHONUNBUFFERED=1", cmd)
        self.assertEqual(cmd[-2:], ["test_cmd", "arg1"])

    def test_run_elevated_cmd_tees_and_captures_output(self):
        """run_elevated_cmd streams stdout/stderr live and clones them to result"""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        cmd = [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.write('prompt: '); sys.stdout.flush(); "
                "sys.stdout.write('hello\\n'); "
                "sys.stderr.write('warn\\n')"
            ),
        ]
        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf):
            result = run_elevated_cmd(cmd)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "prompt: hello\n")
        self.assertEqual(result.stderr, "warn\n")
        self.assertEqual(out_buf.getvalue(), "prompt: hello\n")
        self.assertEqual(err_buf.getvalue(), "warn\n")

    def test_run_elevated_cmd_inherits_stdin_and_exit_code(self):
        """run_elevated_cmd passes stdin=None for terminal access and preserves exit code"""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        cmd = [sys.executable, "-c", "import sys; sys.exit(42)"]
        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf), patch(
            "subprocess.Popen", wraps=__import__("subprocess").Popen
        ) as mock_popen:
            result = run_elevated_cmd(cmd)

        self.assertEqual(result.returncode, 42)
        self.assertIsNone(mock_popen.call_args.kwargs.get("stdin"))

    def test_default_elevation_tool_requires_gui_and_local_session(self):
        """default_elevation_tool uses pkexec only in a local graphical session"""
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), patch.dict(
            os.environ, {"DISPLAY": ":0"}, clear=True
        ):
            self.assertEqual(default_elevation_tool(), "pkexec")

        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), patch.dict(
            os.environ, {}, clear=True
        ):
            self.assertEqual(default_elevation_tool(), "sudo")

        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), patch.dict(
            os.environ,
            {"DISPLAY": ":0", "SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"},
            clear=True,
        ):
            self.assertEqual(default_elevation_tool(), "sudo")

    def test_run_elevated_cmd_falls_back_to_sudo_on_no_session_for_cookie(self):
        """run_elevated_cmd retries with fallback_cmd if pkexec has no polkit session"""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        pkexec_cmd = ["pkexec", "dummy"]
        fallback_cmd = [sys.executable, "-c", "print('sudo ok')"]

        real_popen = __import__("subprocess").Popen

        def fake_popen(cmd, **kwargs):
            if cmd == pkexec_cmd:
                return real_popen(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import sys; "
                            "sys.stderr.write('No session for cookie\\n'); "
                            "sys.exit(127)"
                        ),
                    ],
                    **kwargs,
                )
            return real_popen(cmd, **kwargs)

        with patch("sys.stdout", out_buf), patch("sys.stderr", err_buf), patch(
            "subprocess.Popen", side_effect=fake_popen
        ):
            result = run_elevated_cmd(pkexec_cmd, fallback_cmd=fallback_cmd)

        self.assertEqual(result.returncode, 0)
        self.assertIn("sudo ok", result.stdout)
        self.assertIn("No session for cookie", result.stderr)
        self.assertIn("falling back to sudo", result.stderr)


class TestLogStream(unittest.TestCase):
    """Test the LogStream class"""

    def setUp(self):
        self.mock_app = Mock()
        self.mock_app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.mock_log = Mock()
        self.status_updates = []

        def status_cb(msg):
            self.status_updates.append(msg)

        self.stream = LogStream(self.mock_app, self.mock_log, status_cb)

    def test_write_empty(self):
        self.stream.write("")
        self.mock_app.call_from_thread.assert_not_called()

    def test_write_lines(self):
        self.stream.write("Hello World\nSecond Line\n")
        # Both lines should be dispatched to log widget via call_from_thread
        self.assertEqual(self.mock_app.call_from_thread.call_count, 2)

    def test_write_carriage_return_updates_status(self):
        self.stream.write("Countdown 5\rCountdown 4\rCountdown 3\n")
        self.assertIn("Countdown 5", self.status_updates)
        self.assertIn("Countdown 4", self.status_updates)

    def test_flush_remaining_buffer(self):
        self.stream.write("Incomplete line without newline")
        self.mock_app.call_from_thread.assert_not_called()
        self.stream.flush()
        self.mock_app.call_from_thread.assert_called_once()


class TestModals(unittest.TestCase):
    """Test modal dialog screens"""

    def test_confirm_modal_actions(self):
        modal = ConfirmModal("Test message", "Test Title")
        modal.dismiss = Mock()

        modal.action_confirm(True)
        modal.dismiss.assert_called_with(True)

        modal.action_confirm(False)
        modal.dismiss.assert_called_with(False)

    def test_confirm_modal_buttons(self):
        modal = ConfirmModal("Test message")
        modal.dismiss = Mock()

        event_yes = Mock()
        event_yes.button.id = "btn-yes"
        modal.on_button_pressed(event_yes)
        modal.dismiss.assert_called_with(True)

        event_no = Mock()
        event_no.button.id = "btn-no"
        modal.on_button_pressed(event_no)
        modal.dismiss.assert_called_with(False)

    def test_ask_question_modal_is_yn(self):
        modal_yn = AskQuestionModal("Do you want to proceed (y/n)? ")
        self.assertTrue(modal_yn.is_yn)

        modal_text = AskQuestionModal("What format to output the report in? ")
        self.assertFalse(modal_text.is_yn)

    def test_ask_question_modal_button_yn(self):
        modal = AskQuestionModal("Proceed (y/n)?")
        modal.dismiss = Mock()

        event_yes = Mock()
        event_yes.button.id = "btn-yn-yes"
        modal.on_button_pressed(event_yes)
        modal.dismiss.assert_called_with("y")

        event_no = Mock()
        event_no.button.id = "btn-yn-no"
        modal.on_button_pressed(event_no)
        modal.dismiss.assert_called_with("n")

    def test_ask_question_modal_dismiss_default(self):
        modal = AskQuestionModal("Enter value", default="default_val")
        modal.dismiss = Mock()
        modal.action_dismiss_default()
        modal.dismiss.assert_called_with("default_val")

    @patch("subprocess.Popen")
    def test_file_saved_modal_open_report(self, mock_popen):
        modal = FileSavedModal("/path/report.html", "html", "Report")
        modal.dismiss = Mock()

        event_open = Mock()
        event_open.button.id = "fs-open"
        modal.on_button_pressed(event_open)

        mock_popen.assert_called_once()
        self.assertEqual(mock_popen.call_args[0][0], ["xdg-open", "/path/report.html"])
        modal.dismiss.assert_called_once_with(None)

    @patch("subprocess.Popen")
    def test_file_saved_modal_close_dialog(self, mock_popen):
        modal = FileSavedModal("/path/report.html", "html", "Report")
        modal.dismiss = Mock()

        event_close = Mock()
        event_close.button.id = "fs-close"
        modal.on_button_pressed(event_close)

        mock_popen.assert_not_called()
        modal.dismiss.assert_called_once_with(None)

    def test_file_saved_modal_button_labels(self):
        import asyncio

        from textual.app import App
        from textual.widgets import Button

        class _TestApp(App):
            pass

        async def _check():
            app = _TestApp()
            async with app.run_test() as pilot:
                modal = FileSavedModal("/path/report.html", "html", "Report")
                app.push_screen(modal)
                await pilot.pause()
                close_btn = modal.query_one("#fs-close", Button)
                open_btn = modal.query_one("#fs-open", Button)
                self.assertEqual(str(close_btn.label), "Close Dialog")
                self.assertEqual(str(open_btn.label), "Open Report")

        asyncio.run(_check())


class TestDatePickerModal(unittest.TestCase):
    """Test DatePickerModal validation"""

    def test_invalid_date_shows_error(self):
        from datetime import date

        modal = DatePickerModal(date(2026, 2, 1))
        mock_month = Mock(value=2)
        mock_day = Mock(value=31)
        mock_year = Mock(value=2026)
        mock_error = Mock()

        def query_side(selector, cls=None):
            if selector == "#dp-month":
                return mock_month
            elif selector == "#dp-day":
                return mock_day
            elif selector == "#dp-year":
                return mock_year
            elif selector == "#dp-error":
                return mock_error
            return Mock()

        modal.query_one = Mock(side_effect=query_side)
        modal.dismiss = Mock()

        event = Mock()
        event.button.id = "dp-ok"
        modal.on_button_pressed(event)

        modal.dismiss.assert_not_called()
        mock_error.update.assert_called_once()
        self.assertIn("Invalid date", mock_error.update.call_args[0][0])

    def test_blank_selection_shows_error(self):
        from datetime import date

        from textual.widgets import Select

        modal = DatePickerModal(date(2026, 2, 1))
        mock_month = Mock(value=Select.BLANK)
        mock_day = Mock(value=1)
        mock_year = Mock(value=2026)
        mock_error = Mock()

        def query_side(selector, cls=None):
            if selector == "#dp-month":
                return mock_month
            elif selector == "#dp-day":
                return mock_day
            elif selector == "#dp-year":
                return mock_year
            elif selector == "#dp-error":
                return mock_error
            return Mock()

        modal.query_one = Mock(side_effect=query_side)
        modal.dismiss = Mock()

        event = Mock()
        event.button.id = "dp-ok"
        modal.on_button_pressed(event)

        modal.dismiss.assert_not_called()
        mock_error.update.assert_called_once()
        self.assertIn("Please select", mock_error.update.call_args[0][0])

    def test_valid_date_dismisses(self):
        from datetime import date

        modal = DatePickerModal(date(2026, 2, 1))
        mock_month = Mock(value=2)
        mock_day = Mock(value=28)
        mock_year = Mock(value=2026)
        mock_error = Mock()

        def query_side(selector, cls=None):
            if selector == "#dp-month":
                return mock_month
            elif selector == "#dp-day":
                return mock_day
            elif selector == "#dp-year":
                return mock_year
            elif selector == "#dp-error":
                return mock_error
            return Mock()

        modal.query_one = Mock(side_effect=query_side)
        modal.dismiss = Mock()

        event = Mock()
        event.button.id = "dp-ok"
        modal.on_button_pressed(event)

        modal.dismiss.assert_called_once_with(date(2026, 2, 28))


class TestS2idleApp(unittest.TestCase):
    """Test S2idleApp methods and handlers"""

    def setUp(self):
        self.app = S2idleApp(tool_debug=True)
        # Disable the post-dispatch "Press Enter" pause so tests never block on input()
        self._no_pause = patch.dict(os.environ, {"AMD_S2IDLE_TUI_NO_PAUSE": "1"})
        self._no_pause.start()

    def tearDown(self):
        self._no_pause.stop()
        super().tearDown()

    def test_app_initialization(self):
        self.assertTrue(self.app.tool_debug)
        self.assertFalse(self.app._is_busy)
        self.assertEqual(self.app.TITLE, "amd-s2idle")

    def test_default_tab_is_triage(self):
        """Verify that the TUI defaults to the triage tab"""
        import asyncio

        from textual.widgets import TabbedContent

        async def _run():
            app = S2idleApp()
            async with app.run_test():
                tabbed = app.query_one(TabbedContent)
                self.assertEqual(tabbed.active, "tab-triage")

        asyncio.run(_run())

    @patch("amd_debug.s2idle_tui.is_root", return_value=True)
    def test_on_mount_root(self, _mock_root):
        self.app.refresh_triage_status = Mock()
        self.app.log_message = Mock()
        self.app.push_screen = Mock()

        self.app.on_mount()
        self.app.refresh_triage_status.assert_called_once()
        self.app.log_message.assert_called()

    @patch("amd_debug.s2idle_tui.is_root", return_value=False)
    def test_on_mount_non_root(self, _mock_root):
        self.app.refresh_triage_status = Mock()
        self.app.log_message = Mock()
        self.app.push_screen = Mock()

        self.app.on_mount()
        self.app.refresh_triage_status.assert_called_once()
        self.app.log_message.assert_called()

    def test_clear_log_action(self):
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.action_clear_log()
        mock_log.clear.assert_called_once()

    def test_set_status(self):
        mock_status = Mock()
        self.app.query_one = Mock(return_value=mock_status)
        self.app.set_status("Running something")
        mock_status.update.assert_called_once_with("Status: Running something")

    @patch("amd_debug.s2idle_tui.SleepDatabase")
    def test_refresh_triage_status_active(self, mock_db_cls):
        mock_db = mock_db_cls.return_value
        mock_db.triage_get.return_value = {
            "stage": "baseline",
            "status": "ready",
            "started": 20261121120000,
        }
        mock_static = Mock()
        mock_export = Mock()
        mock_run = Mock()
        mock_abandon = Mock()

        def query_side(selector, cls=None):
            if selector == "#triage-status-info":
                return mock_static
            elif selector == "#btn-export-triage":
                return mock_export
            elif selector == "#btn-run-triage":
                return mock_run
            elif selector == "#btn-abandon-triage":
                return mock_abandon
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)

        self.app.refresh_triage_status()
        mock_static.update.assert_called_once()
        text = mock_static.update.call_args[0][0]
        self.assertIn("Active Triage Session", text)
        self.assertEqual(mock_run.label, "Resume Triage")
        self.assertTrue(mock_abandon.display)
        self.assertTrue(mock_abandon.visible)
        self.assertTrue(mock_export.display)

    @patch("amd_debug.s2idle_tui.SleepDatabase")
    def test_refresh_triage_status_none(self, mock_db_cls):
        mock_db = mock_db_cls.return_value
        mock_db.triage_get.return_value = None
        mock_static = Mock()
        mock_export = Mock()
        mock_run = Mock()
        mock_abandon = Mock()

        def query_side(selector, cls=None):
            if selector == "#triage-status-info":
                return mock_static
            elif selector == "#btn-export-triage":
                return mock_export
            elif selector == "#btn-run-triage":
                return mock_run
            elif selector == "#btn-abandon-triage":
                return mock_abandon
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)

        self.app.refresh_triage_status()
        mock_static.update.assert_called_once()
        text = mock_static.update.call_args[0][0]
        self.assertIn("No active triage session", text)
        self.assertEqual(mock_run.label, "Start Triage")
        self.assertFalse(mock_abandon.display)
        self.assertFalse(mock_abandon.visible)
        self.assertFalse(mock_export.display)

    def test_set_busy(self):
        mock_btns = {
            b: Mock()
            for b in [
                "#btn-run-test",
                "#btn-run-report",
                "#btn-run-triage",
                "#btn-abandon-triage",
                "#status-bar",
            ]
        }
        self.app.query_one = Mock(
            side_effect=lambda s, c=None: mock_btns.get(s, Mock())
        )
        self.app._set_busy(True, "Testing")
        self.assertTrue(self.app._is_busy)
        self.assertTrue(mock_btns["#btn-run-test"].disabled)

        self.app._set_busy(False)
        self.assertFalse(self.app._is_busy)

    def test_handle_run_test_invalid_inputs(self):
        mock_input = Mock()
        mock_input.value = "not_a_number"
        self.app.query_one = Mock(return_value=mock_input)
        self.app.log_message = Mock()

        self.app.handle_run_test()
        self.app.log_message.assert_called_once()
        self.assertIn("Invalid number", self.app.log_message.call_args[0][0])

    def test_handle_run_report_invalid_date(self):
        mock_input = Mock()
        mock_input.value = "invalid-date"
        self.app.query_one = Mock(return_value=mock_input)
        self.app.log_message = Mock()

        self.app.handle_run_report()
        self.app.log_message.assert_called_once()
        self.assertIn("Invalid date", self.app.log_message.call_args[0][0])

    @patch("amd_debug.s2idle_tui.run_elevated_cmd")
    @patch("amd_debug.s2idle_tui.is_root", return_value=False)
    def test_dispatch_test(self, _mock_root, mock_subproc):
        self.app.default_elevation = "sudo"
        self.app.log_message = Mock()
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()

        self.app._dispatch_test(
            duration=1,
            wait=1,
            count=1,
            fmt="txt",
            fname="",
            force=False,
            rand=False,
            logind=False,
            tool_debug=False,
            bios_debug=False,
        )

        # Should suspend TUI and run sudo directly on main thread
        self.app.suspend.assert_called_once()
        mock_subproc.assert_called_once()
        self.assertEqual(mock_subproc.call_args[0][0][0], "sudo")
        self.assertIn("AMD_S2IDLE_TUI=1", mock_subproc.call_args[0][0])

    @patch("amd_debug.s2idle_tui.is_root", return_value=False)
    @patch("amd_debug.s2idle_tui.run_elevated_cmd")
    def test_dispatch_test_pkexec(self, mock_subproc, _mock_root):
        self.app.default_elevation = "pkexec"
        mock_result = Mock()
        mock_result.returncode = 0
        mock_subproc.return_value = mock_result

        self.app.log_message = Mock()
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()

        self.app._dispatch_test(
            duration=1,
            wait=1,
            count=1,
            fmt="txt",
            fname="",
            force=False,
            rand=False,
            logind=False,
            tool_debug=False,
            bios_debug=False,
        )

        self.app.suspend.assert_called_once()
        mock_subproc.assert_called_once()
        self.assertEqual(mock_subproc.call_args[0][0][0], "pkexec")
        self.assertIn("AMD_S2IDLE_TUI=1", mock_subproc.call_args[0][0])

    @patch("amd_debug.s2idle_tui.is_root", return_value=True)
    @patch("amd_debug.s2idle_tui.Installer")
    @patch("amd_debug.s2idle_tui.get_validator")
    @patch("amd_debug.s2idle_tui.SleepValidator")
    @patch("amd_debug.s2idle_tui.SleepReport")
    def test_worker_test_as_root(
        self,
        mock_sleep_report,
        mock_sleep_validator,
        mock_validator,
        mock_installer,
        _mock_root,
    ):
        mock_installer.return_value.install_dependencies.return_value = True
        mock_validator.return_value.run.return_value = True
        mock_sleep_validator.return_value.run.return_value = True

        self.app.query_one = Mock(return_value=Mock())
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )

        self.app._worker_test(
            duration=1,
            wait=1,
            count=1,
            fmt="txt",
            fname="",
            force=False,
            rand=False,
            logind=False,
            tool_debug=False,
            bios_debug=False,
        )

        mock_installer.return_value.install_dependencies.assert_called_once()
        mock_validator.return_value.run.assert_called_once()
        mock_sleep_validator.return_value.run.assert_called_once()
        mock_sleep_report.return_value.run.assert_called_once()

    @patch("amd_debug.s2idle_tui.SleepReport")
    def test_worker_report(self, mock_sleep_report):
        self.app.query_one = Mock(return_value=Mock())
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.show_file_notification = Mock()

        since = datetime.now()
        until = datetime.now()
        self.app._worker_report(since, until, "report.txt", "txt", False, True)

        mock_sleep_report.assert_called_once_with(
            since=since,
            until=until,
            fname="report.txt",
            fmt="txt",
            tool_debug=False,
            report_debug=True,
        )
        mock_sleep_report.return_value.run.assert_called_once()
        self.app.show_file_notification.assert_called_once_with(
            "report.txt", "txt", "Report"
        )

    @patch("amd_debug.s2idle_tui.os.path.lexists", return_value=False)
    @patch("amd_debug.s2idle_tui.is_root", return_value=False)
    @patch("amd_debug.s2idle_tui.run_elevated_cmd")
    def test_handle_run_triage_elevated(self, mock_subproc, _mock_root, _mock_lexists):
        mock_select = Mock()
        mock_select.value = "html"
        mock_input = Mock()
        mock_input.value = ""
        mock_switch = Mock()
        mock_switch.value = False

        def query_side(selector, cls=None):
            if selector == "#triage-format":
                return mock_select
            elif selector == "#triage-report-file":
                return mock_input
            elif selector in (
                "#triage-restart",
                "#triage-tool-debug",
                "#triage-show-log",
            ):
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()
        self.app.refresh_triage_status = Mock()
        self.app.log_message = Mock()

        self.app.default_elevation = "pkexec"
        self.app.handle_run_triage()
        self.app.suspend.assert_called_once()
        self.assertEqual(mock_subproc.call_args_list[0][0][0][0], "pkexec")

    @patch("amd_debug.s2idle_tui.SleepDatabase")
    @patch("amd_debug.s2idle_tui.is_root", return_value=False)
    @patch("amd_debug.s2idle_tui.run_elevated_cmd")
    def test_handle_run_triage_completed_shows_modal(
        self, mock_subproc, _mock_root, mock_db_cls
    ):
        mock_select = Mock(value="html")
        mock_input = Mock(value="")
        mock_switch = Mock(value=False)

        def query_side(selector, cls=None):
            if selector == "#triage-format":
                return mock_select
            elif selector == "#triage-report-file":
                return mock_input
            elif selector in (
                "#triage-restart",
                "#triage-tool-debug",
                "#triage-show-log",
            ):
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()
        self.app.refresh_triage_status = Mock()
        self.app.log_message = Mock()
        self.app.show_file_notification = Mock()
        self.app.call_later = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.push_screen = Mock(
            side_effect=lambda screen, callback=None: (
                callback(True) if callback else None
            )
        )

        mock_subproc.return_value = Mock(returncode=0, stdout="done", stderr="")
        mock_db = mock_db_cls.return_value
        mock_cur = Mock()
        mock_cur.fetchone.return_value = (1,)
        mock_db.db.cursor.return_value = mock_cur
        with patch("os.path.exists", return_value=True):
            mock_db.triage_events.return_value = [
                (
                    "20261122100000",
                    "report",
                    "/path/to/triage-report.html",
                    "html",
                    None,
                )
            ]
            self.app.handle_run_triage()

        self.app.show_file_notification.assert_called_once_with(
            "/path/to/triage-report.html", "html", "Triage"
        )

    @patch("amd_debug.s2idle_tui.os.path.lexists", return_value=False)
    @patch("amd_debug.s2idle_tui.is_root", return_value=False)
    @patch("amd_debug.s2idle_tui.run_elevated_cmd")
    def test_handle_run_triage_elevated_sudo(
        self, mock_subproc, _mock_root, _mock_lexists
    ):
        mock_select = Mock()
        mock_select.value = "html"
        mock_input = Mock()
        mock_input.value = ""
        mock_switch = Mock()
        mock_switch.value = False

        def query_side(selector, cls=None):
            if selector == "#triage-format":
                return mock_select
            elif selector == "#triage-report-file":
                return mock_input
            elif selector in (
                "#triage-restart",
                "#triage-tool-debug",
                "#triage-show-log",
            ):
                return mock_switch
            return Mock()

        self.app.default_elevation = "sudo"
        self.app.query_one = Mock(side_effect=query_side)
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()
        self.app.refresh_triage_status = Mock()
        self.app.log_message = Mock()

        self.app.handle_run_triage()
        self.app.suspend.assert_called_once()
        self.assertEqual(mock_subproc.call_args_list[0][0][0][0], "sudo")

    def test_log_subprocess_output(self):
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        mock_res = Mock()
        mock_res.stdout = "line 1\nline 2\n"
        mock_res.stderr = "error line\n"

        self.app._log_subprocess_output(mock_res)
        self.assertEqual(mock_log.write.call_count, 3)

    @patch("amd_debug.s2idle_tui.Triage")
    def test_worker_triage(self, mock_triage_cls):
        mock_triage = mock_triage_cls.return_value
        mock_triage.report_path = "triage-report.txt"
        mock_triage.fmt = "txt"

        self.app.query_one = Mock(return_value=Mock())
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.show_file_notification = Mock()

        self.app._worker_triage("txt", "triage-report.txt", True, False)

        mock_triage.db.triage_abandon_all.assert_called_once()
        mock_triage.run.assert_called_once()
        self.app.show_file_notification.assert_called_once_with(
            "triage-report.txt", "txt", "Triage"
        )

    @patch("amd_debug.s2idle_tui.Triage")
    def test_worker_triage_change_pending(self, mock_triage_cls):
        mock_triage = mock_triage_cls.return_value
        mock_triage.report_path = None
        mock_triage.declined = False
        mock_triage.session = {"id": 1}
        mock_triage.db.triage_get.return_value = {
            "status": Status.CHANGE_PENDING,
            "stage": Stage.BIOS_DEFAULTS,
        }

        self.app.query_one = Mock(return_value=Mock())
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.show_error_modal = Mock()
        self.app.log_message = Mock()

        self.app._worker_triage("txt", "triage-report.txt", False, False)

        mock_triage.run.assert_called_once()
        self.app.show_error_modal.assert_called_once()
        self.assertIn("Action Required", self.app.show_error_modal.call_args[0][0])

    @patch("amd_debug.s2idle_tui.os.path.exists", return_value=True)
    @patch("amd_debug.s2idle_tui.os.unlink")
    def test_handle_run_test_overwrite_removes_file(self, mock_unlink, _mock_exists):
        mock_input = Mock(value="1")
        mock_select = Mock(value="txt")
        mock_switch = Mock(value=False)
        mock_file_input = Mock(value="/path/report.txt")

        def query_side(selector, cls=None):
            if selector in ("#test-duration", "#test-wait", "#test-count"):
                return mock_input
            elif selector == "#test-format":
                return mock_select
            elif selector == "#test-report-file":
                return mock_file_input
            return mock_switch

        self.app.query_one = Mock(side_effect=query_side)
        self.app._dispatch_test = Mock()

        def push_side(screen, callback=None):
            if callback:
                callback(True)

        self.app.push_screen = Mock(side_effect=push_side)
        self.app.handle_run_test()

        mock_unlink.assert_called_once_with(os.path.abspath("/path/report.txt"))
        self.app._dispatch_test.assert_called_once()

    def test_app_name_from_launch(self):
        for argv0, expected in (
            ("/usr/bin/s2idle", "s2idle"),
            ("s2idle", "s2idle"),
            ("/usr/bin/amd-s2idle", "amd-s2idle"),
            ("amd_s2idle.py", "amd-s2idle"),
        ):
            with patch("sys.argv", [argv0, "tui"]):
                app = S2idleApp()
                self.assertEqual(app.app_name, expected)
                self.assertEqual(app.title, expected)

    def test_app_name_explicit(self):
        app = S2idleApp(app_name="custom-s2idle")
        self.assertEqual(app.app_name, "custom-s2idle")
        self.assertEqual(app.title, "custom-s2idle")

    def test_welcome_screen_app_name(self):
        with patch("sys.argv", ["/usr/bin/s2idle"]):
            screen = WelcomeScreen()
            self.assertEqual(screen.app_name, "s2idle")

        screen_explicit = WelcomeScreen("custom-name")
        self.assertEqual(screen_explicit.app_name, "custom-name")


if __name__ == "__main__":
    unittest.main()
