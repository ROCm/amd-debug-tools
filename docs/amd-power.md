# Per-rail power capture and plotting tool
`amd-power` records per-rail energy from IIO power monitor chips.
Data is saved into a SQLite database and the tool renders the result
as interactive plots. It has been validated with the Microchip PAC1954.

The tool reads the energy accumulator (`in_energyN_raw`, scaled by
`in_energyN_scale`; IIO scales yield milli-units, so joules = raw × scale / 1000) exposed under `/sys/bus/iio/devices/iio:deviceN`. Average
power for any interval is derived at query time as `ΔE/Δt`, which is the only
meaningful value for an accumulating sensor sampled at a finite cadence.
All of the sysfs files used are world-readable, so no root privileges are
needed.

The same discovery code is used by the [s2idle tool](amd-s2idle.md) to report
power consumed while suspended.

## Capturing
Run the capture loop in the foreground (default interval is 5 seconds):

```
❯ amd-power run --interval 5 --note "idle baseline"
```

* `--db` selects the database. The default is `$AMD_POWER_DB` if set, otherwise
  `$XDG_DATA_HOME/amd-debug-tools/power.db` (`~/.local/share/amd-debug-tools/power.db`).
* `--note` attaches free text to the capture run.
* `--log-level` sets the capture log level.

Stop it with Ctrl-C or SIGTERM; the run is closed cleanly.

## Running as a user service
`amd-power` manages its own user-level systemd unit. The capture options are
baked into the generated `ExecStart` line, so configure them at install time:

```
❯ amd-power install --interval 5 --now
```

Lifecycle commands:

```
amd-power status
amd-power stop
amd-power start
amd-power restart
amd-power enable [--now]
amd-power disable [--now]
amd-power logs [-f] [-n LINES]
amd-power uninstall [--keep-unit]
```

`install --force` overwrites an existing unit; `install --exec PATH` selects
the `amd-power` executable (default: alongside the running Python).
To keep capturing without an active login, run once:

```
sudo loginctl enable-linger "$USER"
```

## Plotting
```
# Last 10 minutes of per-rail average power
❯ amd-power plot power.html

# Last 2 hours, CPU and GPU rails only, several metrics stacked
❯ amd-power plot cpu_gpu.html --since 2h --rails 'CPU_*' --rails 'GPU_*' \
      --metric power --metric energy --metric aggregate \
      --resample 30s --energy-unit Wh

# Explicit ISO timestamps (naive timestamps are UTC)
❯ amd-power plot window.html --since 2026-05-20T08:00 --until 2026-05-20T09:00
```

`--since` and `--until` accept either a duration relative to the window end
(`30s`, `10m`, `2h`, `1d`) or an ISO-8601 timestamp.

Available metrics:

| Metric      | Description                                        |
|-------------|----------------------------------------------------|
| `power`     | Average power per rail (ΔE/Δt)                     |
| `energy`    | Cumulative energy per rail (`Wh` or `J`)           |
| `aggregate` | Total system power plus per-chip subtotals         |

Gaps longer than ten times the median sample interval (for example while the
capture service was stopped) reset the energy delta, so traces don't jump
across restarts.

## Database schema
* `rails(id, device, channel, label, energy_scale)`
* `samples(rail_id, ts, energy_raw)` with primary key `(rail_id, ts)`
* `capture_runs(id, started_ts, ended_ts, note)`

Raw accumulator values are stored as read; the scale is applied at query time.
