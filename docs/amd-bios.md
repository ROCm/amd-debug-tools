# BIOS log parser
`amd-bios` is a tool that can be used to enable or disable BIOS AML debug logging
and to parse a kernel log that contains BIOS logs.

Without any command, `amd-bios` starts the interactive Textual terminal
user interface (TUI) by default.

The following commands are supported.

## `amd-bios tui`
This will launch an interactive Textual terminal user interface (TUI) wrapping
the `trace` and `parse` commands.
This is also the default mode when `amd-bios` is invoked without any command.

The TUI presents a welcome screen to choose between managing BIOS AML tracing
and parsing a kernel log, and provides a tab for each. Tracing requires
root privileges; when the TUI is not running as root the trace command is
re-launched elevated (via `pkexec` or `sudo`) and its output is streamed back
into the TUI log.

The following optional arguments are supported for this command:

        --tool-debug   Enable tool debug logging

## `amd-bios trace`
Modify BIOS AML trace debug logging.

One of the following arguments must be set for this command:

        --enable       Enable BIOS AML tracing
        --disable      Disable BIOS AML tracing

The following optional arguments are supported for this command:

        --tool-debug   Enable tool debug logging

## `amd-bios parse`
Parses a kernel log that contains BIOS AML debug logging and produces a report.

The following optional arguments are supported for this command:

        --input INPUT  Optional input file to parse
        --tool-debug   Enable tool debug logging

## `amd-bios --version`
This will print the version of the tool and exit.
