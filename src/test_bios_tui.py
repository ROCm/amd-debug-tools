#!/usr/bin/python3
# SPDX-License-Identifier: MIT

"""
Unit tests for the amd-bios Textual TUI.
"""

import unittest
from unittest.mock import Mock, patch

from amd_debug.bios_tui import (
    BiosApp,
    BiosWelcomeScreen,
    get_bios_cmd,
)


class TestBiosHelpers(unittest.TestCase):
    """Test helper functions in the bios_tui module"""

    def test_get_bios_cmd_exists(self):
        cmd = get_bios_cmd()
        self.assertTrue(len(cmd) >= 1)


class TestBiosWelcomeScreen(unittest.TestCase):
    """Test the BiosWelcomeScreen"""

    def test_welcome_screen_app_name(self):
        screen = BiosWelcomeScreen()
        self.assertEqual(screen.app_name, "amd-bios")

        screen_explicit = BiosWelcomeScreen("custom-name")
        self.assertEqual(screen_explicit.app_name, "custom-name")


class TestBiosApp(unittest.TestCase):
    """Test BiosApp methods and handlers"""

    def setUp(self):
        self.app = BiosApp(tool_debug=True)

    def test_app_initialization(self):
        self.assertTrue(self.app.tool_debug)
        self.assertFalse(self.app._is_busy)
        self.assertEqual(self.app.TITLE, "amd-bios")
        self.assertEqual(self.app.app_name, "amd-bios")

    def test_app_name_override(self):
        app = BiosApp(app_name="custom")
        self.assertEqual(app.app_name, "custom")
        self.assertEqual(app.title, "custom")

    def test_default_tab_is_trace(self):
        """Verify that the TUI defaults to the trace tab"""
        import asyncio

        from textual.widgets import TabbedContent

        async def _run():
            app = BiosApp()
            async with app.run_test():
                tabbed = app.query_one(TabbedContent)
                self.assertEqual(tabbed.active, "tab-trace")

        asyncio.run(_run())

    def test_welcome_screen_shown_on_mount(self):
        """Verify the welcome screen is pushed on mount"""
        import asyncio

        async def _run():
            app = BiosApp()
            async with app.run_test():
                self.assertIn(BiosWelcomeScreen, [type(s) for s in app.screen_stack])

        asyncio.run(_run())

    @patch("amd_debug.bios_tui.is_root", return_value=True)
    def test_on_mount_root(self, _mock_root):
        self.app.log_message = Mock()
        self.app.push_screen = Mock()

        self.app.on_mount()
        self.app.push_screen.assert_called_once()
        self.app.log_message.assert_called()

    @patch("amd_debug.bios_tui.is_root", return_value=False)
    def test_on_mount_non_root(self, _mock_root):
        self.app.log_message = Mock()
        self.app.push_screen = Mock()

        self.app.on_mount()
        self.app.push_screen.assert_called_once()
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

    def test_set_busy(self):
        mock_btns = {
            b: Mock()
            for b in [
                "#btn-run-trace",
                "#btn-run-parse",
                "#status-bar",
            ]
        }
        self.app.query_one = Mock(
            side_effect=lambda s, c=None: mock_btns.get(s, Mock())
        )
        self.app._set_busy(True, "Tracing")
        self.assertTrue(self.app._is_busy)
        self.assertTrue(mock_btns["#btn-run-trace"].disabled)
        self.assertTrue(mock_btns["#btn-run-parse"].disabled)

        self.app._set_busy(False)
        self.assertFalse(self.app._is_busy)
        self.assertFalse(mock_btns["#btn-run-trace"].disabled)

    def test_switch_to_tab(self):
        mock_tabbed = Mock()
        self.app.query_one = Mock(return_value=mock_tabbed)
        self.app.log_message = Mock()

        self.app._switch_to_tab("tab-parse")
        self.assertEqual(mock_tabbed.active, "tab-parse")

    def test_switch_to_tab_failure(self):
        self.app.query_one = Mock(side_effect=Exception("no tab"))
        self.app.log_message = Mock()

        self.app._switch_to_tab("tab-bogus")
        self.assertIn("Failed to switch tab", self.app.log_message.call_args[0][0])

    def test_log_message(self):
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.log_message("[green]hello[/green]")
        mock_log.write.assert_called_once()

    def test_show_error_modal(self):
        self.app.push_screen = Mock()
        self.app.show_error_modal("Title", "Message")
        self.app.push_screen.assert_called_once()

    def test_log_subprocess_output(self):
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        result = Mock()
        result.stdout = "line1\nline2\n"
        result.stderr = "err\n"

        self.app._log_subprocess_output(result)
        self.assertGreaterEqual(mock_log.write.call_count, 3)

    def test_log_subprocess_output_empty(self):
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        result = Mock()
        result.stdout = ""
        result.stderr = ""

        self.app._log_subprocess_output(result)
        mock_log.write.assert_not_called()

    def test_ensure_log_shown(self):
        mock_container = Mock()
        mock_container.has_class.return_value = False
        self.app.query_one = Mock(return_value=mock_container)

        self.app._ensure_log_shown()
        mock_container.add_class.assert_called_once_with("show-log")

    def test_ensure_log_shown_already_visible(self):
        mock_container = Mock()
        mock_container.has_class.return_value = True
        self.app.query_one = Mock(return_value=mock_container)

        self.app._ensure_log_shown()
        mock_container.add_class.assert_not_called()

    def test_on_button_pressed_routing(self):
        self.app.action_clear_log = Mock()
        self.app.handle_run_trace = Mock()
        self.app.handle_run_parse = Mock()

        for btn_id, handler in [
            ("btn-clear-log", self.app.action_clear_log),
            ("btn-run-trace", self.app.handle_run_trace),
            ("btn-run-parse", self.app.handle_run_parse),
        ]:
            event = Mock()
            event.button.id = btn_id
            self.app.on_button_pressed(event)
            handler.assert_called_once()

    # -- Trace command ----------------------------------------------------

    @patch("amd_debug.bios_tui.is_root", return_value=False)
    @patch("amd_debug.bios_tui.run_elevated_cmd")
    def test_handle_run_trace_elevated_sudo(self, mock_subproc, _mock_root):
        self.app.default_elevation = "sudo"
        mock_select = Mock()
        mock_select.value = "enable"
        mock_switch = Mock()
        mock_switch.value = False

        def query_side(selector, cls=None):
            if selector == "#trace-action":
                return mock_select
            if selector == "#trace-tool-debug":
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()
        self.app._ensure_log_shown = Mock()
        self.app._log_subprocess_output = Mock()
        self.app.log_message = Mock()
        self.app.call_later = Mock()

        self.app.handle_run_trace()

        self.app.suspend.assert_called_once()
        mock_subproc.assert_called_once()
        self.assertEqual(mock_subproc.call_args[0][0][0], "sudo")
        self.assertIn("AMD_BIOS_TUI=1", mock_subproc.call_args[0][0])
        self.assertIn("trace", mock_subproc.call_args[0][0])
        self.assertIn("--enable", mock_subproc.call_args[0][0])

    @patch("amd_debug.bios_tui.is_root", return_value=False)
    @patch("amd_debug.bios_tui.run_elevated_cmd")
    def test_handle_run_trace_elevated_pkexec(self, mock_subproc, _mock_root):
        self.app.default_elevation = "pkexec"
        mock_result = Mock()
        mock_result.returncode = 0
        mock_subproc.return_value = mock_result

        mock_select = Mock()
        mock_select.value = "disable"
        mock_switch = Mock()
        mock_switch.value = True

        def query_side(selector, cls=None):
            if selector == "#trace-action":
                return mock_select
            if selector == "#trace-tool-debug":
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()
        self.app._ensure_log_shown = Mock()
        self.app._log_subprocess_output = Mock()
        self.app.log_message = Mock()
        self.app.call_later = Mock()

        self.app.handle_run_trace()

        self.app.suspend.assert_called_once()
        mock_subproc.assert_called_once()
        self.assertEqual(mock_subproc.call_args[0][0][0], "pkexec")
        self.assertIn("AMD_BIOS_TUI=1", mock_subproc.call_args[0][0])
        self.assertIn("--disable", mock_subproc.call_args[0][0])
        self.assertIn("--tool-debug", mock_subproc.call_args[0][0])

    @patch("amd_debug.bios_tui.is_root", return_value=False)
    @patch("amd_debug.bios_tui.run_elevated_cmd")
    def test_handle_run_trace_elevated_failure(self, mock_subproc, _mock_root):
        self.app.default_elevation = "pkexec"
        mock_result = Mock()
        mock_result.returncode = 1
        mock_subproc.return_value = mock_result

        mock_select = Mock()
        mock_select.value = "enable"
        mock_switch = Mock()
        mock_switch.value = False

        def query_side(selector, cls=None):
            if selector == "#trace-action":
                return mock_select
            if selector == "#trace-tool-debug":
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app.suspend = Mock()
        self.app.suspend.return_value.__enter__ = Mock()
        self.app.suspend.return_value.__exit__ = Mock()
        self.app._ensure_log_shown = Mock()
        self.app._log_subprocess_output = Mock()
        self.app.log_message = Mock()
        self.app.call_later = Mock()

        self.app.handle_run_trace()

        self.app.call_later.assert_called_once()
        self.assertIn("failed", self.app.log_message.call_args_list[-1][0][0])

    @patch("amd_debug.bios_tui.is_root", return_value=True)
    def test_handle_run_trace_as_root(self, _mock_root):
        mock_select = Mock()
        mock_select.value = "enable"
        mock_switch = Mock()
        mock_switch.value = False

        def query_side(selector, cls=None):
            if selector == "#trace-action":
                return mock_select
            if selector == "#trace-tool-debug":
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app._set_busy = Mock()
        self.app.run_worker = Mock()

        self.app.handle_run_trace()

        self.app._set_busy.assert_called_once_with(True, "Trace")
        self.app.run_worker.assert_called_once()

    @patch("amd_debug.bios_tui.is_root", return_value=True)
    def test_handle_run_trace_busy(self, _mock_root):
        self.app._is_busy = True
        self.app.run_worker = Mock()

        self.app.handle_run_trace()

        self.app.run_worker.assert_not_called()

    @patch("amd_debug.bios_tui.AmdBios")
    @patch("amd_debug.bios_tui.capture_output_to_log")
    def test_worker_trace(self, mock_capture, mock_bios):
        mock_bios.return_value.set_tracing.return_value = True
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.log_message = Mock()
        self.app._set_busy = Mock()

        self.app._worker_trace("enable", False)

        mock_bios.assert_called_once_with(None, False)
        mock_bios.return_value.set_tracing.assert_called_once_with(True)
        self.app._set_busy.assert_called_once_with(False)

    @patch("amd_debug.bios_tui.AmdBios")
    @patch("amd_debug.bios_tui.capture_output_to_log")
    def test_worker_trace_disable(self, mock_capture, mock_bios):
        mock_bios.return_value.set_tracing.return_value = True
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.log_message = Mock()
        self.app._set_busy = Mock()

        self.app._worker_trace("disable", True)

        mock_bios.assert_called_once_with(None, True)
        mock_bios.return_value.set_tracing.assert_called_once_with(False)

    @patch("amd_debug.bios_tui.AmdBios")
    @patch("amd_debug.bios_tui.capture_output_to_log")
    def test_worker_trace_error(self, mock_capture, mock_bios):
        mock_bios.side_effect = Exception("boom")
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.log_message = Mock()
        self.app._set_busy = Mock()

        self.app._worker_trace("enable", False)

        self.assertIn("Error running trace", self.app.log_message.call_args[0][0])
        self.app._set_busy.assert_called_once_with(False)

    # -- Parse command ----------------------------------------------------

    def test_handle_run_parse(self):
        mock_input = Mock()
        mock_input.value = "/var/log/kern.log"
        mock_switch = Mock()
        mock_switch.value = False

        def query_side(selector, cls=None):
            if selector == "#parse-input":
                return mock_input
            if selector == "#parse-tool-debug":
                return mock_switch
            return Mock()

        self.app.query_one = Mock(side_effect=query_side)
        self.app._set_busy = Mock()
        self.app.run_worker = Mock()

        self.app.handle_run_parse()

        self.app._set_busy.assert_called_once_with(True, "Parse")
        self.app.run_worker.assert_called_once()

    def test_handle_run_parse_busy(self):
        self.app._is_busy = True
        self.app.run_worker = Mock()

        self.app.handle_run_parse()

        self.app.run_worker.assert_not_called()

    @patch("amd_debug.bios_tui.AmdBios")
    @patch("amd_debug.bios_tui.capture_output_to_log")
    def test_worker_parse(self, mock_capture, mock_bios):
        mock_bios.return_value.run.return_value = True
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.log_message = Mock()
        self.app._set_busy = Mock()

        self.app._worker_parse("/var/log/kern.log", False)

        mock_bios.assert_called_once_with("/var/log/kern.log", False)
        mock_bios.return_value.run.assert_called_once()
        self.app._set_busy.assert_called_once_with(False)

    @patch("amd_debug.bios_tui.AmdBios")
    @patch("amd_debug.bios_tui.capture_output_to_log")
    def test_worker_parse_no_input(self, mock_capture, mock_bios):
        mock_bios.return_value.run.return_value = True
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.log_message = Mock()
        self.app._set_busy = Mock()

        self.app._worker_parse("", False)

        mock_bios.assert_called_once_with(None, False)
        mock_bios.return_value.run.assert_called_once()

    @patch("amd_debug.bios_tui.AmdBios")
    @patch("amd_debug.bios_tui.capture_output_to_log")
    def test_worker_parse_error(self, mock_capture, mock_bios):
        mock_bios.side_effect = Exception("boom")
        mock_log = Mock()
        self.app.query_one = Mock(return_value=mock_log)
        self.app.call_from_thread = Mock(
            side_effect=lambda f, *args, **kwargs: f(*args, **kwargs)
        )
        self.app.log_message = Mock()
        self.app._set_busy = Mock()

        self.app._worker_parse("/var/log/kern.log", False)

        self.assertIn("Error parsing log", self.app.log_message.call_args[0][0])
        self.app._set_busy.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
