# LANA Zenduty Monitor

Small local monitor for Zenduty incidents that uses Codex only when Drua/skills are needed.

The monitor keeps durable state in SQLite, writes a GNOME-readable `status.json`, and can invoke Codex from `~/source/repos/lana-bank` to list or investigate incidents.

## Quick Start

```sh
make init
make poll
make install-gnome-extension
make enable-gnome-extension
make enable-user-service
```

The default assignee filter is `a7eecf16-e8fa-4903-90ea-8`.

Edit `~/.config/lana-zenduty-monitor/config.toml` to change the filter, polling interval, Codex command, or prompt behavior.

## Status Colors

- Red: at least one filtered `triggered` incident
- Yellow: no triggered incidents, but at least one filtered `acknowledged` incident
- Green: no filtered open incidents
- Gray: monitor has not produced status yet or the status file cannot be read

## Files

Runtime files live in `~/.local/state/lana-zenduty-monitor/`:

- `monitor.db`: incident and run state
- `status.json`: current state for the GNOME extension
- `details.html`: local details UI opened from the GNOME extension
- `logs/`: Codex poll and triage transcripts

## Commands

```sh
make poll
make daemon
make triage INCIDENT_ID=<zenduty-unique-id>
make status
make details
```

`make poll` asks Codex to use Drua from the Lana Bank checkout and return machine-readable JSON. Newly seen incidents are stored and optionally triaged.

`make triage` asks Codex to use the `lana-alert-fixer` workflow, inspect Zenduty/Honeycomb/code, and write a summary. The default prompt forbids auto-resolution.

## Details UI

The GNOME extension popup shows current counts, up to eight open incidents, and actions:

- `Open details`: opens `~/.local/state/lana-zenduty-monitor/details.html`
- `Open logs`: opens the transcript directory
- `Open status file`: opens the raw status JSON
- `Refresh now`: starts the systemd user service once

Incident rows open their triage log when available, otherwise the details page.

## systemd User Timer

```sh
make enable-user-service
systemctl --user status lana-zenduty-monitor.timer
journalctl --user -u lana-zenduty-monitor.service -f
```

The timer runs `poll` periodically. The interval is controlled by `systemd/lana-zenduty-monitor.timer`; the monitor also has its own daemon mode if you prefer a foreground loop.

## GNOME Extension

```sh
make install-gnome-extension
make enable-gnome-extension
```

If the extension does not appear, restart GNOME Shell or log out/in. On X11, `Alt+F2`, `r`, Enter usually reloads GNOME Shell. On Wayland, log out/in.
