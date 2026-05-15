# LANA Zenduty Monitor

Small local monitor for Zenduty incidents that uses Codex only when Drua/skills are needed.

The monitor keeps durable state in SQLite, writes a GNOME-readable `status.json`, invokes Codex from `~/source/repos/lana-bank` to list or investigate incidents and Concourse failures, and uses `gh` to report open Dependabot pull requests.

## Quick Start

```sh
cd ~/source/repos/lana-zenduty-monitor
make init
make install-gnome-extension
make enable-gnome-extension
make enable-user-service
```

The Dependabot PR monitor requires the GitHub CLI to be authenticated:

```sh
gh auth status
```

Start the background monitor and check that the timer is active:

```sh
make enable-user-service
systemctl --user status lana-zenduty-monitor.timer
systemctl --user list-timers lana-zenduty-monitor.timer
```

Run one poll immediately:

```sh
systemctl --user start lana-zenduty-monitor.service
```

Check status from the command line:

```sh
make status
systemctl --user status lana-zenduty-monitor.service
```

Watch monitor logs:

```sh
journalctl --user -u lana-zenduty-monitor.service -f
```

Open the details UI and inspect runtime files:

```sh
make details
ls -la ~/.local/state/lana-zenduty-monitor
ls -la ~/.local/state/lana-zenduty-monitor/logs
```

The assignee filter is read from `.env` by default:

```sh
LANA_ZENDUTY_ASSIGNEE_USER_IDS=zenduty-user-id-1,zenduty-user-id-2
LANA_ZENDUTY_ASSIGNEE_ALIASES=zenduty-user-id-1=person@example.com,zenduty-user-id-2=other@example.com
```

Edit `~/.config/lana-zenduty-monitor/config.toml` to change the filter, polling interval, Codex command, or prompt behavior.

When an assignee filter is configured, `make poll` also checks Zenduty schedules through Drua before reporting Zenduty incidents. If the configured user is off schedule, Zenduty incident reporting and auto-triage are suppressed until the user is back on call. By default only `Primary Schedule` is authoritative; change `monitor.zenduty_schedule_name` if that schedule name changes. Set `monitor.zenduty_team_id` if Drua does not have a default Zenduty team configured.

Dependabot pull request monitoring uses `gh pr list` against `GaloyMoney/lana-bank` by default. A PR is marked ready when it is open, not draft, and all reported checks completed successfully.

Concourse monitoring uses Drua's Concourse MCP tools against `https://ci.galoy.io/`. It lists active pipelines, finds failed/errored/aborted jobs, and records latest build metadata, resources, CI links, and compact log summaries.

## Status Colors

- Red: at least one filtered `triggered` incident or failed Concourse job
- Yellow: no triggered incidents, but at least one filtered `acknowledged` incident
- Blue: no filtered open incidents, and at least one Dependabot PR is ready to merge
- Green: no filtered open incidents, or Zenduty incident reporting is off schedule
- Gray: monitor has not produced status yet or the status file cannot be read

## Files

Runtime files live in `~/.local/state/lana-zenduty-monitor/`:

- `monitor.db`: incident and run state
- `status.json`: current state for the GNOME extension
- `details.html`: local details UI opened from the GNOME extension
- `logs/`: Codex poll, triage, and fix transcripts
- `fixes/`: per-incident local workspaces for bug-fix attempts

## Commands

```sh
make poll
make daemon
make triage INCIDENT_ID=<zenduty-unique-id>
make enqueue-triage INCIDENT=<zenduty-unique-id-or-number>
make work-triage
make enqueue-fix INCIDENT=<zenduty-unique-id-or-number>
make work-fixes
make enqueue-drua-triage INCIDENT=<zenduty-unique-id-or-number>
make work-drua-triage
make enqueue-drua-fix INCIDENT=<zenduty-unique-id-or-number>
make work-drua-fixes
make status
make details
```

`make poll` asks Codex to use Drua from the Lana Bank checkout and return machine-readable JSON. Newly seen triggered incidents are stored and queued for triage when auto-triage is enabled.

`make poll` also checks open Dependabot PRs with the local `gh` CLI and records how many have successful checks.

For Zenduty incidents whose title starts with `[HoneyComb]`, `make poll` fetches the Zenduty detail summary, extracts Honeycomb query URLs, and asks Drua's Honeycomb MCP tools for query results when a URL is available.

For Concourse, `make poll` uses `concourse_list_pipelines`, `concourse_list_jobs`, `concourse_get_build_status`, `concourse_get_build_resources`, and `concourse_get_build_logs`.

`make work-triage` processes one queued triage job. A triage worker writes a Zenduty note tagged `[lana-monitor:triage]`, optionally acknowledges the incident, classifies it as `false_positive`, `bug_possible`, or `needs_human`, and never resolves it. False positives are marked for review instead of being closed.

`make work-fixes` processes one queued bug-fix job. A fix worker creates an incident-specific workspace under `fix_root_dir`, works on a branch named for the incident, records reproduction and fix evidence, commits local fixes when it makes code changes, and writes a Zenduty note tagged `[lana-monitor:fix]`. It does not push or resolve incidents.

`make enqueue-triage` and `make enqueue-fix` manually queue jobs by Zenduty unique id or by incident number if the incident is already in local state. Pass `ARGS=--force` to retry an existing job.

When `monitor.drua_shadow_enabled` is true, each newly triggered incident is also queued for a Drua shadow triage job in parallel with the normal worker. Shadow jobs may use read-only Drua tools but do not write Zenduty notes or change incident status. They record the would-have-written note, classification, evidence, and any debug/fix result locally for comparison. If `monitor.auto_drua_debug_bug_possible` is true, `bug_possible` shadow triage results queue a shadow fix job under `fix_root_dir/drua-shadow/`.

## Details UI

The GNOME extension popup shows current counts, schedule state, up to eight open incidents, Concourse failures, Dependabot PR readiness, and actions:

- `Open details`: opens `~/.local/state/lana-zenduty-monitor/details.html`
- `Open logs`: opens the transcript directory
- `Open status file`: opens the raw status JSON
- `Refresh now`: starts the systemd user service once

Incident rows open their triage log when available, otherwise the details page.
Workflow history rows open the fix workspace when available, otherwise the triage log or details page.

## systemd User Timer

```sh
make enable-user-service
systemctl --user status lana-zenduty-monitor.timer
journalctl --user -u lana-zenduty-monitor.service -f
journalctl --user -u lana-zenduty-triage-worker.service -f
journalctl --user -u lana-zenduty-fix-worker.service -f
journalctl --user -u lana-zenduty-drua-triage-worker.service -f
journalctl --user -u lana-zenduty-drua-fix-worker.service -f
```

The timer runs `poll` periodically. Queue workers run as long-lived user services and process triage and fix jobs independently from polling. The interval is controlled by `systemd/lana-zenduty-monitor.timer`; the monitor also has its own daemon mode if you prefer a foreground loop.

## GNOME Extension

```sh
make install-gnome-extension
make enable-gnome-extension
```

If the extension does not appear, restart GNOME Shell or log out/in. On X11, `Alt+F2`, `r`, Enter usually reloads GNOME Shell. On Wayland, log out/in.
