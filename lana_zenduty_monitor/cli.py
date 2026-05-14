from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ASSIGNEE_USER_IDS_ENV = "LANA_ZENDUTY_ASSIGNEE_USER_IDS"
ASSIGNEE_ALIASES_ENV = "LANA_ZENDUTY_ASSIGNEE_ALIASES"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "lana-zenduty-monitor" / "config.toml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "lana-zenduty-monitor"
DEFAULT_LANA_BANK_DIR = Path.home() / "source" / "repos" / "lana-bank"
DEFAULT_DEPENDABOT_REPO = "GaloyMoney/lana-bank"
DEFAULT_CONCOURSE_URL = "https://ci.galoy.io"
CONCOURSE_FAILURE_STATUSES = {"failed", "errored", "aborted"}
SUCCESSFUL_CHECK_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}


@dataclass(frozen=True)
class CodexConfig:
    command: str = "codex"
    model: str = ""
    sandbox: str = "danger-full-access"
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class MonitorConfig:
    lana_bank_dir: Path = DEFAULT_LANA_BANK_DIR
    state_dir: Path = DEFAULT_STATE_DIR
    poll_interval_seconds: int = 300
    codex_timeout_seconds: int = 600
    triage_new_incidents: bool = True
    assignee_user_id: tuple[str, ...] = ()
    assignee_aliases: dict[str, str] | None = None
    zenduty_team_id: str = ""
    zenduty_schedule_name: str = "Primary Schedule"
    dependabot_repo: str = DEFAULT_DEPENDABOT_REPO
    dependabot_pr_limit: int = 50
    concourse_url: str = DEFAULT_CONCOURSE_URL
    concourse_failure_limit: int = 20
    gh_timeout_seconds: int = 120
    codex: CodexConfig = CodexConfig()

    def __post_init__(self) -> None:
        if self.assignee_aliases is None:
            object.__setattr__(self, "assignee_aliases", {})


def split_env_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def env_assignee_user_ids() -> tuple[str, ...]:
    return split_env_list(os.environ.get(ASSIGNEE_USER_IDS_ENV, ""))


def env_assignee_aliases() -> dict[str, str]:
    raw = os.environ.get(ASSIGNEE_ALIASES_ENV, "").strip()
    if not raw:
        return {}
    aliases: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        user_id, sep, alias = item.partition("=")
        if not sep:
            raise ValueError(f"{ASSIGNEE_ALIASES_ENV} entries must use user_id=alias")
        aliases[user_id.strip()] = alias.strip()
    return aliases


def parse_assignee_user_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return env_assignee_user_ids()
    if isinstance(value, str):
        assignee = value.strip()
        return (assignee,) if assignee else ()
    if isinstance(value, list):
        return tuple(str(assignee).strip() for assignee in value if str(assignee).strip())
    raise TypeError("monitor.assignee_user_id must be a string or list of strings")


def parse_assignee_aliases(value: Any) -> dict[str, str]:
    aliases = env_assignee_aliases()
    if value is None:
        return aliases
    if not isinstance(value, dict):
        raise TypeError("monitor.assignee_aliases must be a table mapping user IDs to aliases")
    aliases.update({str(user_id): str(alias) for user_id, alias in value.items()})
    return aliases


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def concourse_snooze_path(config: MonitorConfig) -> Path:
    return config.state_dir / "concourse-snooze.json"


def read_concourse_snooze(config: MonitorConfig) -> dict[str, Any]:
    path = concourse_snooze_path(config)
    if not path.exists():
        return {"active": False, "until": None}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"active": False, "until": None}
    until = parse_utc(data.get("until"))
    active = bool(until and until > datetime.now(timezone.utc))
    return {"active": active, "until": data.get("until") if until else None}


def set_concourse_snooze(config: MonitorConfig, minutes: int) -> dict[str, Any]:
    until = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=minutes)
    snooze = {"active": True, "until": until.isoformat().replace("+00:00", "Z")}
    config.state_dir.mkdir(parents=True, exist_ok=True)
    concourse_snooze_path(config).write_text(json.dumps(snooze, indent=2, sort_keys=True))
    return snooze


def clear_concourse_snooze(config: MonitorConfig) -> None:
    concourse_snooze_path(config).unlink(missing_ok=True)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> MonitorConfig:
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text())

    monitor = data.get("monitor", {})
    codex = data.get("codex", {})

    return MonitorConfig(
        lana_bank_dir=Path(monitor.get("lana_bank_dir", DEFAULT_LANA_BANK_DIR)).expanduser(),
        state_dir=Path(monitor.get("state_dir", DEFAULT_STATE_DIR)).expanduser(),
        poll_interval_seconds=int(monitor.get("poll_interval_seconds", 300)),
        codex_timeout_seconds=int(monitor.get("codex_timeout_seconds", 600)),
        triage_new_incidents=bool(monitor.get("triage_new_incidents", True)),
        assignee_user_id=parse_assignee_user_ids(monitor.get("assignee_user_id")),
        assignee_aliases=parse_assignee_aliases(monitor.get("assignee_aliases")),
        zenduty_team_id=str(monitor.get("zenduty_team_id", "")).strip(),
        zenduty_schedule_name=str(monitor.get("zenduty_schedule_name", "Primary Schedule")).strip(),
        dependabot_repo=str(monitor.get("dependabot_repo", DEFAULT_DEPENDABOT_REPO)),
        dependabot_pr_limit=int(monitor.get("dependabot_pr_limit", 50)),
        concourse_url=str(monitor.get("concourse_url", DEFAULT_CONCOURSE_URL)).rstrip("/"),
        concourse_failure_limit=int(monitor.get("concourse_failure_limit", 20)),
        gh_timeout_seconds=int(monitor.get("gh_timeout_seconds", 120)),
        codex=CodexConfig(
            command=str(codex.get("command", "codex")),
            model=str(codex.get("model", "")),
            sandbox=str(codex.get("sandbox", "danger-full-access")),
            extra_args=tuple(str(arg) for arg in codex.get("extra_args", [])),
        ),
    )


class Store:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "logs").mkdir(exist_ok=True)
        self.db_path = self.state_dir / "monitor.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            create table if not exists incidents (
                unique_id text primary key,
                incident_number integer,
                title text not null,
                status text not null,
                assigned_to text,
                creation_date text,
                urgency integer,
                first_seen_at text not null,
                last_seen_at text not null,
                triage_status text not null default 'pending',
                triage_summary text,
                triage_log_path text,
                raw_json text not null
            );

            create table if not exists runs (
                id integer primary key autoincrement,
                kind text not null,
                started_at text not null,
                finished_at text,
                success integer not null default 0,
                log_path text,
                error text
            );
            """
        )
        self.conn.commit()

    def begin_run(self, kind: str, log_path: Path) -> int:
        cur = self.conn.execute(
            "insert into runs(kind, started_at, log_path) values (?, ?, ?)",
            (kind, utc_now(), str(log_path)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, success: bool, error: str | None = None) -> None:
        self.conn.execute(
            "update runs set finished_at = ?, success = ?, error = ? where id = ?",
            (utc_now(), 1 if success else 0, error, run_id),
        )
        self.conn.commit()

    def upsert_incidents(self, incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        new: list[dict[str, Any]] = []
        now = utc_now()
        for incident in incidents:
            unique_id = incident["unique_id"]
            existing = self.conn.execute(
                "select unique_id from incidents where unique_id = ?", (unique_id,)
            ).fetchone()
            if existing is None:
                new.append(incident)
                self.conn.execute(
                    """
                    insert into incidents(
                        unique_id, incident_number, title, status, assigned_to,
                        creation_date, urgency, first_seen_at, last_seen_at, raw_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        unique_id,
                        incident.get("incident_number"),
                        incident.get("title", ""),
                        incident.get("status", ""),
                        incident.get("assigned_to"),
                        incident.get("creation_date"),
                        incident.get("urgency"),
                        now,
                        now,
                        json.dumps(incident, sort_keys=True),
                    ),
                )
            else:
                self.conn.execute(
                    """
                    update incidents
                    set incident_number = ?, title = ?, status = ?, assigned_to = ?,
                        creation_date = ?, urgency = ?, last_seen_at = ?, raw_json = ?
                    where unique_id = ?
                    """,
                    (
                        incident.get("incident_number"),
                        incident.get("title", ""),
                        incident.get("status", ""),
                        incident.get("assigned_to"),
                        incident.get("creation_date"),
                        incident.get("urgency"),
                        now,
                        json.dumps(incident, sort_keys=True),
                        unique_id,
                    ),
                )
        self.conn.commit()
        return new

    def mark_triaged(self, unique_id: str, status: str, summary: str, log_path: Path) -> None:
        self.conn.execute(
            """
            update incidents
            set triage_status = ?, triage_summary = ?, triage_log_path = ?
            where unique_id = ?
            """,
            (status, summary, str(log_path), unique_id),
        )
        self.conn.commit()

    def incident(self, unique_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "select * from incidents where unique_id = ?", (unique_id,)
        ).fetchone()
        if not row:
            return None
        incident = dict(row)
        try:
            raw = json.loads(incident.get("raw_json") or "{}")
            if isinstance(raw, dict):
                raw.update(incident)
                return raw
        except json.JSONDecodeError:
            pass
        return incident

    def enrich_incidents(self, incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for incident in incidents:
            stored = self.incident(incident["unique_id"]) or {}
            merged = dict(incident)
            for key in [
                "first_seen_at",
                "last_seen_at",
                "triage_status",
                "triage_summary",
                "triage_log_path",
            ]:
                if stored.get(key) is not None:
                    merged[key] = stored[key]
            enriched.append(merged)
        return enriched

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            select kind, started_at, finished_at, success, log_path, error
            from runs
            order by id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def codex_command(config: MonitorConfig, prompt: str) -> list[str]:
    cmd = [config.codex.command, "exec"]
    if config.codex.model:
        cmd.extend(["--model", config.codex.model])
    if config.codex.sandbox:
        cmd.extend(["--sandbox", config.codex.sandbox])
    cmd.extend(config.codex.extra_args)
    cmd.append(prompt)
    return cmd


def run_codex(config: MonitorConfig, prompt: str, log_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    proc = subprocess.run(
        codex_command(config, prompt),
        cwd=config.lana_bank_dir,
        text=True,
        capture_output=True,
        timeout=config.codex_timeout_seconds,
        env=env,
    )
    log_path.write_text(
        "\n".join(
            [
                f"# Codex run at {utc_now()}",
                "",
                "## Prompt",
                "",
                prompt,
                "",
                "## stdout",
                "",
                proc.stdout,
                "",
                "## stderr",
                "",
                proc.stderr,
            ]
        )
    )
    return proc


def acquire_poll_lock(config: MonitorConfig):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (config.state_dir / "poll.lock").open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()
    return lock_file


def extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(stripped)

    fenced = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.S)
    for candidate in fenced:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("Codex output did not contain parseable JSON")


def assignee_display(user_id: str, aliases: dict[str, str]) -> str:
    alias = aliases.get(user_id)
    return f"{alias} ({user_id})" if alias else user_id


def assignee_filter_display(config: MonitorConfig) -> str:
    if not config.assignee_user_id:
        return "none"
    aliases = config.assignee_aliases or {}
    return ", ".join(assignee_display(user_id, aliases) for user_id in config.assignee_user_id)


def assignee_prompt_list(config: MonitorConfig) -> str:
    aliases = config.assignee_aliases or {}
    return "\n".join(
        f"- {user_id} ({aliases[user_id]})" if aliases.get(user_id) else f"- {user_id}"
        for user_id in config.assignee_user_id
    )


def zenduty_team_prompt(config: MonitorConfig) -> str:
    if config.zenduty_team_id:
        return f'Pass team_id exactly "{config.zenduty_team_id}" to schedule and incident tools.'
    return "Omit team_id so Drua uses its configured Zenduty default team."


def zenduty_schedule_prompt(config: MonitorConfig) -> str:
    if config.zenduty_schedule_name:
        return (
            f'Only evaluate the Zenduty schedule whose name exactly equals '
            f'"{config.zenduty_schedule_name}". Ignore all other schedules.'
        )
    return "Evaluate all returned Zenduty schedules."


def add_assignee_aliases(config: MonitorConfig, incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aliases = config.assignee_aliases or {}
    enriched: list[dict[str, Any]] = []
    for incident in incidents:
        copied = dict(incident)
        assigned_to = copied.get("assigned_to")
        if assigned_to is not None:
            copied["assignee_alias"] = aliases.get(str(assigned_to))
        enriched.append(copied)
    return enriched


def check_counts(checks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"total": len(checks), "success": 0, "pending": 0, "failure": 0}
    for check in checks:
        status = str(check.get("status", "")).upper()
        conclusion = str(check.get("conclusion", "")).upper()
        if status != "COMPLETED":
            counts["pending"] += 1
        elif conclusion in SUCCESSFUL_CHECK_CONCLUSIONS:
            counts["success"] += 1
        else:
            counts["failure"] += 1
    return counts


def normalize_dependabot_pr(raw: dict[str, Any]) -> dict[str, Any]:
    checks = raw.get("statusCheckRollup")
    if not isinstance(checks, list):
        checks = []
    counts = check_counts([check for check in checks if isinstance(check, dict)])
    checks_successful = counts["total"] > 0 and counts["pending"] == 0 and counts["failure"] == 0
    ready = checks_successful and not bool(raw.get("isDraft"))
    return {
        "number": raw.get("number"),
        "title": str(raw.get("title", "")),
        "url": raw.get("url"),
        "updated_at": raw.get("updatedAt"),
        "head_sha": raw.get("headRefOid"),
        "is_draft": bool(raw.get("isDraft")),
        "merge_state": raw.get("mergeStateStatus"),
        "checks_successful": checks_successful,
        "ready_to_merge": ready,
        "check_counts": counts,
    }


def fetch_dependabot_prs(config: MonitorConfig) -> tuple[list[dict[str, Any]], str | None]:
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        config.dependabot_repo,
        "--author",
        "app/dependabot",
        "--state",
        "open",
        "--json",
        "number,title,url,headRefOid,statusCheckRollup,mergeStateStatus,isDraft,updatedAt",
        "--limit",
        str(config.dependabot_pr_limit),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=config.lana_bank_dir,
            text=True,
            capture_output=True,
            timeout=config.gh_timeout_seconds,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if proc.returncode != 0:
            return [], proc.stderr.strip() or f"gh exited with {proc.returncode}"
        payload = json.loads(proc.stdout or "[]")
        if not isinstance(payload, list):
            return [], "gh returned non-list PR payload"
        return [normalize_dependabot_pr(pr) for pr in payload if isinstance(pr, dict)], None
    except Exception as exc:
        return [], str(exc)


def concourse_build_url(config: MonitorConfig, failure: dict[str, Any]) -> str | None:
    team = failure.get("team")
    pipeline = failure.get("pipeline")
    job = failure.get("job")
    build_name = failure.get("build_name") or failure.get("name")
    if not all([team, pipeline, job, build_name]):
        return None
    return (
        f"{config.concourse_url}/teams/{team}/pipelines/{pipeline}/jobs/{job}/builds/{build_name}"
    )


def normalize_concourse_failures(
    payload: Any,
    config: MonitorConfig,
) -> tuple[list[dict[str, Any]], str | None]:
    if payload is None:
        return [], None
    if not isinstance(payload, list):
        return [], "Expected concourse_failures array"
    failures: list[dict[str, Any]] = []
    for raw in payload[: config.concourse_failure_limit]:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status", "")).lower()
        if status and status not in CONCOURSE_FAILURE_STATUSES:
            continue
        failure = {
            "team": raw.get("team"),
            "pipeline": raw.get("pipeline"),
            "job": raw.get("job"),
            "status": status or raw.get("status"),
            "build_id": raw.get("build_id"),
            "build_name": raw.get("build_name") or raw.get("name"),
            "start_time": raw.get("start_time"),
            "end_time": raw.get("end_time"),
            "resources": raw.get("resources") if isinstance(raw.get("resources"), list) else [],
            "log_summary": raw.get("log_summary"),
            "failure_summary": raw.get("failure_summary"),
            "url": raw.get("url"),
        }
        if not failure["url"]:
            failure["url"] = concourse_build_url(config, failure)
        failures.append(failure)
    return failures, None


def payload_error(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def zenduty_error_from_run(payload: dict[str, Any], proc: subprocess.CompletedProcess[str]) -> str | None:
    zenduty_error = payload_error(payload, "zenduty_error")
    if zenduty_error:
        return zenduty_error

    incidents = payload.get("incidents")
    if incidents == [] and "ToolSetsError - Zenduty:" in proc.stderr:
        return "Zenduty MCP tool failed; see the poll log for details."
    return None


def poll_prompt(config: MonitorConfig) -> str:
    assignee_filter = (
        "Only include incidents whose Zenduty detail assigned_to exactly equals one of these user IDs:\n"
        f"{assignee_prompt_list(config)}"
        if config.assignee_user_id
        else "Do not filter by assignee."
    )
    schedule_check = (
        textwrap.dedent(
            f"""
            First establish whether the configured Zenduty user is currently on schedule.
            {zenduty_team_prompt(config)}
            Call zenduty_list_schedules, then select the relevant schedule.
            {zenduty_schedule_prompt(config)}
            Call zenduty_get_schedule only for the selected schedule unique_id, then determine
            whether one of these user IDs is currently on call:
            {assignee_prompt_list(config)}
            Match those IDs against on_call_now entries and nested user fields such as unique_id, id,
            user_id, user.unique_id, user.id, and member.unique_id. If on_call_now does not include
            enough timing information, inspect layers and overrides to find the active window at the
            current UTC time. Include the best window_start/window_end you can infer, preserving the
            schedule time_zone when available.

            If the schedule API fails, still attempt the incident and Concourse checks, but include
            schedule.error with the exact tool error summary.
            If schedule.on_call is false, do not list Zenduty incidents; return incidents as [] and
            do not set zenduty_error for skipped incident listing.
            """
        ).strip()
        if config.assignee_user_id
        else "Set schedule.enabled to false, schedule.on_call to true, and do not call schedule tools because no assignee filter is configured."
    )
    return textwrap.dedent(
        f"""
        Use MCP Drua Zenduty tools from this Codex session.

        {schedule_check}

        List open Zenduty incidents by calling zenduty_list_incidents twice:
        - once with statuses exactly ["triggered"]
        - once with statuses exactly ["acknowledged"]
        Do not rely on the tool's default statuses, and do not combine status names in one call.
        {zenduty_team_prompt(config)}
        Fetch detail records as needed to include assigned_to and to apply the assignee filter.
        For incidents whose title starts with "[HoneyComb]" or whose detail summary contains a Honeycomb URL:
        - include the Zenduty detail summary in "summary"
        - extract all Honeycomb URLs into "honeycomb_urls"
        - call honeycomb_get_query_results with the first Honeycomb URL and include a compact text summary in "honeycomb_query_results"
        {assignee_filter}
        If any Zenduty incident or incident-detail call fails, do not guess or return an empty
        incidents list as success. Instead return "zenduty_error" with the exact tool error summary.

        Also inspect Concourse at https://ci.galoy.io/ using MCP Drua Concourse tools:
        - call concourse_list_pipelines
        - for each active, non-paused, non-archived pipeline, call concourse_list_jobs
        - include jobs whose last_status is failed, errored, or aborted
        - for each failed job, call concourse_get_build_status
        - call concourse_get_build_resources for the build_id when available
        - call concourse_get_build_logs for the build_id and summarize the likely failure cause in one sentence
        - construct url as https://ci.galoy.io/teams/<team>/pipelines/<pipeline>/jobs/<job>/builds/<build_name>
        Limit concourse_failures to {config.concourse_failure_limit} entries.

        Return only valid JSON, with no markdown and no prose:
        {{
          "checked_at": "<UTC ISO-8601 timestamp>",
          "zenduty_error": null,
          "schedule": {{
            "enabled": true,
            "on_call": true,
            "status": "on",
            "team_id": "{config.zenduty_team_id}",
            "user_ids": {json.dumps(list(config.assignee_user_id))},
            "matched_user_id": "<matched Zenduty user id or null>",
            "matched_alias": "<alias or null>",
            "schedule_id": "<Zenduty schedule unique_id or null>",
            "schedule_name": "<schedule name or null>",
            "configured_schedule_name": "{config.zenduty_schedule_name}",
            "window_start": "<UTC/local ISO-8601 timestamp or null>",
            "window_end": "<UTC/local ISO-8601 timestamp or null>",
            "timezone": "<schedule time_zone or null>",
            "summary": "On schedule until ...",
            "error": null
          }},
          "incidents": [
            {{
              "unique_id": "<Zenduty unique_id>",
              "incident_number": 123,
              "status": "triggered",
              "title": "...",
              "creation_date": "...",
              "urgency": 1,
              "assigned_to": "...",
              "summary": "...",
              "honeycomb_urls": ["https://ui.honeycomb.io/..."],
              "honeycomb_query_results": "..."
            }}
          ],
          "concourse_failures": [
            {{
              "team": "dev",
              "pipeline": "pipeline-name",
              "job": "job-name",
              "status": "failed",
              "build_id": 123,
              "build_name": "456",
              "start_time": 1778660000,
              "end_time": 1778660300,
              "url": "https://ci.galoy.io/teams/dev/pipelines/pipeline-name/jobs/job-name/builds/456",
              "resources": [
                {{"resource": "repo", "version": {{"ref": "..."}}}}
              ],
              "log_summary": "One sentence with the relevant failing command or error.",
              "failure_summary": "One sentence identifying the failed pipeline/job and probable cause."
            }}
          ]
        }}
        """
    ).strip()


def triage_prompt(incident: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        Use the lana-alert-fixer workflow.

        Investigate this Zenduty incident:
        - unique_id: {incident["unique_id"]}
        - incident_number: {incident.get("incident_number")}
        - status: {incident.get("status")}
        - title: {incident.get("title")}
        - creation_date: {incident.get("creation_date")}
        - assigned_to: {incident.get("assigned_to")}
        - summary: {incident.get("summary")}
        - honeycomb_urls: {incident.get("honeycomb_urls")}
        - honeycomb_query_results: {incident.get("honeycomb_query_results")}

        Fetch the incident details, inspect linked Honeycomb context, identify the code path,
        classify as false positive or real problem, and add a Zenduty note with the final summary.
        If this is a HoneyComb incident and honeycomb_urls is present, call honeycomb_get_query_results
        with the first URL before forming the final diagnosis.

        Do not resolve the incident.
        Do not push.
        Do not change code unless the issue is clearly a false-positive severity fix or a real bug
        with a low-risk local fix. If you change code, commit locally with a conventional commit.

        End your response with a compact JSON object on its own line:
        {{
          "incident_number": {incident.get("incident_number") or "null"},
          "unique_id": "{incident["unique_id"]}",
          "classification": "false_positive|real_problem|needs_human",
          "changed_code": false,
          "committed": false,
          "summary": "one paragraph"
        }}
        """
    ).strip()


def schedule_default(config: MonitorConfig) -> dict[str, Any]:
    enabled = bool(config.assignee_user_id)
    return {
        "enabled": enabled,
        "on_call": True if not enabled else None,
        "status": "disabled" if not enabled else "unknown",
        "team_id": config.zenduty_team_id or None,
        "user_ids": list(config.assignee_user_id),
        "matched_user_id": None,
        "matched_alias": None,
        "schedule_id": None,
        "schedule_name": None,
        "configured_schedule_name": config.zenduty_schedule_name or None,
        "window_start": None,
        "window_end": None,
        "timezone": None,
        "summary": (
            "Schedule check disabled; no assignee filter is configured."
            if not enabled
            else "Schedule check pending."
        ),
        "error": None,
        "suppressed_incident_count": 0,
    }


def normalize_schedule(payload: Any, config: MonitorConfig) -> dict[str, Any]:
    schedule = schedule_default(config)
    if not isinstance(payload, dict):
        return schedule

    enabled = bool(payload.get("enabled", schedule["enabled"]))
    schedule["enabled"] = enabled
    schedule["team_id"] = payload.get("team_id") or schedule["team_id"]
    schedule["user_ids"] = (
        [str(user_id) for user_id in payload.get("user_ids", []) if str(user_id).strip()]
        if isinstance(payload.get("user_ids"), list)
        else schedule["user_ids"]
    )

    on_call = payload.get("on_call")
    if isinstance(on_call, bool):
        schedule["on_call"] = on_call
        schedule["status"] = "on" if on_call else "off"
    elif not enabled:
        schedule["on_call"] = True
        schedule["status"] = "disabled"
    else:
        schedule["on_call"] = None
        schedule["status"] = "unknown"

    for key in [
        "matched_user_id",
        "matched_alias",
        "schedule_id",
        "schedule_name",
        "configured_schedule_name",
        "window_start",
        "window_end",
        "timezone",
        "summary",
        "error",
    ]:
        if payload.get(key) is not None:
            schedule[key] = str(payload[key])

    summary_times = re.findall(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        str(schedule.get("summary") or ""),
    )
    if not schedule.get("window_start") and not schedule.get("window_end") and len(summary_times) >= 2:
        schedule["window_start"] = iso_time_value(summary_times[-2])
        schedule["window_end"] = iso_time_value(summary_times[-1])

    if not schedule.get("matched_alias") and schedule.get("matched_user_id"):
        schedule["matched_alias"] = (config.assignee_aliases or {}).get(
            str(schedule["matched_user_id"])
        )

    try:
        schedule["suppressed_incident_count"] = max(
            0, int(payload.get("suppressed_incident_count", 0))
        )
    except (TypeError, ValueError):
        schedule["suppressed_incident_count"] = 0

    if not schedule.get("summary"):
        if schedule["status"] == "on":
            until = f" until {schedule['window_end']}" if schedule.get("window_end") else ""
            schedule["summary"] = f"On schedule{until}."
        elif schedule["status"] == "off":
            schedule["summary"] = "Off schedule."
        elif schedule.get("error"):
            schedule["summary"] = "Schedule check failed."
        else:
            schedule["summary"] = schedule_default(config)["summary"]

    return schedule


def schedule_blocks_reporting(schedule: dict[str, Any]) -> bool:
    return bool(schedule.get("enabled")) and schedule.get("on_call") is False


def normalize_incidents(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("incidents"), list):
        raise ValueError("Expected JSON object with incidents array")
    incidents: list[dict[str, Any]] = []
    for raw in payload["incidents"]:
        if not isinstance(raw, dict) or not raw.get("unique_id"):
            continue
        incidents.append(
            {
                "unique_id": str(raw["unique_id"]),
                "incident_number": raw.get("incident_number"),
                "status": str(raw.get("status", "")),
                "title": str(raw.get("title", "")),
                "creation_date": raw.get("creation_date"),
                "urgency": raw.get("urgency"),
                "assigned_to": raw.get("assigned_to"),
                "assignee_alias": raw.get("assignee_alias"),
                "summary": raw.get("summary"),
                "honeycomb_urls": raw.get("honeycomb_urls") if isinstance(raw.get("honeycomb_urls"), list) else [],
                "honeycomb_query_results": raw.get("honeycomb_query_results"),
            }
        )
    return incidents


def write_status(
    config: MonitorConfig,
    incidents: list[dict[str, Any]],
    dependabot_prs: list[dict[str, Any]] | None = None,
    concourse_failures: list[dict[str, Any]] | None = None,
    schedule: dict[str, Any] | None = None,
    error: str | None = None,
    dependabot_error: str | None = None,
    concourse_error: str | None = None,
    zenduty_error: str | None = None,
    store: Store | None = None,
) -> None:
    dependabot_prs = dependabot_prs or []
    concourse_failures = concourse_failures or []
    schedule = normalize_schedule(schedule, config)
    if schedule_blocks_reporting(schedule) and incidents:
        schedule["suppressed_incident_count"] = len(incidents)
        incidents = []
    triggered = [i for i in incidents if i.get("status") == "triggered"]
    acknowledged = [i for i in incidents if i.get("status") == "acknowledged"]
    ready_dependabot_prs = [pr for pr in dependabot_prs if pr.get("ready_to_merge")]
    concourse_snooze = read_concourse_snooze(config)
    active_concourse_failures = concourse_failures if not concourse_snooze["active"] else []
    color = (
        "red"
        if triggered or active_concourse_failures
        else "yellow"
        if acknowledged
        else "blue"
        if ready_dependabot_prs
        else "green"
    )
    if error or zenduty_error:
        color = "gray"

    status = {
        "updated_at": utc_now(),
        "color": color,
        "error": error,
        "zenduty_error": zenduty_error,
        "dependabot_error": dependabot_error,
        "concourse_error": concourse_error,
        "counts": {
            "triggered": len(triggered),
            "acknowledged": len(acknowledged),
            "open": len(triggered) + len(acknowledged),
            "dependabot_open": len(dependabot_prs),
            "dependabot_ready": len(ready_dependabot_prs),
            "concourse_failed": len(concourse_failures),
            "concourse_unsnoozed_failed": len(active_concourse_failures),
            "schedule_suppressed": int(schedule.get("suppressed_incident_count") or 0),
        },
        "schedule": schedule,
        "dependabot": {
            "repo": config.dependabot_repo,
            "open": len(dependabot_prs),
            "ready": len(ready_dependabot_prs),
            "prs": dependabot_prs,
            "error": dependabot_error,
        },
        "concourse": {
            "url": config.concourse_url,
            "failed": len(concourse_failures),
            "failures": concourse_failures,
            "error": concourse_error,
            "snooze": concourse_snooze,
        },
        "assignee_user_id": list(config.assignee_user_id),
        "assignee_aliases": config.assignee_aliases,
        "assignee_filter": assignee_filter_display(config),
        "details_html": str(config.state_dir / "details.html"),
        "logs_dir": str(config.state_dir / "logs"),
        "incidents": incidents,
    }
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True))
    write_details_html(config, status, store=store)


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def iso_time_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value)
    parsed = parse_utc(text)
    if parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return text


def time_span(value: Any) -> str:
    iso = iso_time_value(value)
    if not iso:
        return ""
    return f'<time data-time="{e(iso)}">{e(iso)}</time>'


def status_label(color: str) -> str:
    return {
        "red": "Incidents or CI failures",
        "yellow": "Acknowledged incidents",
        "blue": "Dependabot PRs ready",
        "green": "No open incidents",
        "gray": "Monitor unavailable",
    }.get(color, "Unknown")


def write_details_html(
    config: MonitorConfig,
    status: dict[str, Any],
    store: Store | None = None,
) -> None:
    incidents = status.get("incidents", [])
    dependabot = status.get("dependabot", {})
    dependabot_prs = dependabot.get("prs", []) if isinstance(dependabot, dict) else []
    concourse = status.get("concourse", {})
    concourse_failures = concourse.get("failures", []) if isinstance(concourse, dict) else []
    concourse_snooze = concourse.get("snooze", {}) if isinstance(concourse, dict) else {}
    schedule = normalize_schedule(status.get("schedule"), config)
    runs = store.recent_runs() if store is not None else []
    rows = []
    for incident in incidents:
        assigned_to = incident.get("assigned_to")
        assignee_alias = incident.get("assignee_alias")
        assigned_to_label = (
            f"{assignee_alias} ({assigned_to})" if assignee_alias and assigned_to else assigned_to
        )
        honeycomb_urls = incident.get("honeycomb_urls") or []
        honeycomb_links = " ".join(
            f'<a href="{e(url)}">Honeycomb query</a>' for url in honeycomb_urls if url
        )
        honeycomb_details = ""
        if incident.get("summary") or honeycomb_links or incident.get("honeycomb_query_results"):
            honeycomb_details = f"""
              <section>
                <h3>Honeycomb Context</h3>
                {f'<p>{honeycomb_links}</p>' if honeycomb_links else ''}
                {f'<p>{e(incident.get("summary"))}</p>' if incident.get("summary") else ''}
                {f'<pre>{e(incident.get("honeycomb_query_results"))}</pre>' if incident.get("honeycomb_query_results") else ''}
              </section>
            """
        log_path = incident.get("triage_log_path")
        log_link = (
            f'<a href="file://{e(log_path)}">triage log</a>'
            if log_path
            else '<span class="muted">no triage log</span>'
        )
        rows.append(
            f"""
            <article class="incident {e(incident.get('status'))}">
              <header>
                <div>
                  <h2>#{e(incident.get('incident_number', '?'))}</h2>
                  <p class="title">{e(incident.get('title'))}</p>
                </div>
                <span class="pill">{e(incident.get('status'))}</span>
              </header>
              <dl>
                <div><dt>Unique ID</dt><dd><code>{e(incident.get('unique_id'))}</code></dd></div>
                <div><dt>Assigned To</dt><dd><code>{e(assigned_to_label)}</code></dd></div>
                <div><dt>Created</dt><dd>{time_span(incident.get('creation_date')) or e(incident.get('creation_date'))}</dd></div>
                <div><dt>First Seen</dt><dd>{time_span(incident.get('first_seen_at')) or e(incident.get('first_seen_at'))}</dd></div>
                <div><dt>Triage</dt><dd>{e(incident.get('triage_status', 'pending'))} · {log_link}</dd></div>
              </dl>
              <section>
                <h3>Triage Summary</h3>
                <p>{e(incident.get('triage_summary') or 'No triage summary recorded yet.')}</p>
              </section>
              {honeycomb_details}
            </article>
            """
        )

    pr_rows = []
    for pr in dependabot_prs:
        counts = pr.get("check_counts") or {}
        readiness = "ready" if pr.get("ready_to_merge") else "not ready"
        url = pr.get("url")
        pr_link = f'<a href="{e(url)}">#{e(pr.get("number", "?"))}</a>' if url else f'#{e(pr.get("number", "?"))}'
        pr_rows.append(
            f"""
            <article class="incident {'ready' if pr.get('ready_to_merge') else 'blocked'}">
              <header>
                <div>
                  <h2>{pr_link}</h2>
                  <p class="title">{e(pr.get('title'))}</p>
                </div>
                <span class="pill">{readiness}</span>
              </header>
              <dl>
                <div><dt>Checks</dt><dd>{e(counts.get('success', 0))} ok · {e(counts.get('failure', 0))} failed · {e(counts.get('pending', 0))} pending</dd></div>
                <div><dt>Merge State</dt><dd><code>{e(pr.get('merge_state'))}</code></dd></div>
                <div><dt>Updated</dt><dd>{time_span(pr.get('updated_at')) or e(pr.get('updated_at'))}</dd></div>
              </dl>
            </article>
            """
        )

    concourse_rows = []
    for failure in concourse_failures:
        resources = failure.get("resources") or []
        resource_text = ", ".join(
            f"{resource.get('resource')}: {json.dumps(resource.get('version'), sort_keys=True)}"
            for resource in resources
            if isinstance(resource, dict)
        )
        url = failure.get("url")
        title = f"{failure.get('pipeline')}/{failure.get('job')}"
        title_html = f'<a href="{e(url)}">{e(title)}</a>' if url else e(title)
        concourse_rows.append(
            f"""
            <article class="incident failed">
              <header>
                <div>
                  <h2>{title_html}</h2>
                  <p class="title">{e(failure.get('failure_summary') or failure.get('log_summary'))}</p>
                </div>
                <span class="pill">{e(failure.get('status'))}</span>
              </header>
              <dl>
                <div><dt>Team</dt><dd>{e(failure.get('team'))}</dd></div>
                <div><dt>Build</dt><dd><code>{e(failure.get('build_name') or failure.get('build_id'))}</code></dd></div>
                <div><dt>Started</dt><dd>{time_span(failure.get('start_time')) or e(failure.get('start_time'))}</dd></div>
                <div><dt>Finished</dt><dd>{time_span(failure.get('end_time')) or e(failure.get('end_time'))}</dd></div>
              </dl>
              {f'<pre>{e(resource_text)}</pre>' if resource_text else ''}
              {f'<pre>{e(failure.get("log_summary"))}</pre>' if failure.get("log_summary") else ''}
            </article>
            """
        )

    run_rows = []
    for run in runs:
        success = "ok" if run.get("success") else "failed"
        log_path = run.get("log_path")
        log_link = f'<a href="file://{e(log_path)}">log</a>' if log_path else ""
        run_rows.append(
            f"<tr><td>{e(run.get('kind'))}</td><td>{time_span(run.get('started_at')) or e(run.get('started_at'))}</td>"
            f"<td>{time_span(run.get('finished_at')) or e(run.get('finished_at'))}</td><td>{success}</td><td>{log_link}</td>"
            f"<td>{e(run.get('error'))}</td></tr>"
        )

    schedule_note = ""
    if schedule_blocks_reporting(schedule):
        suppressed = int(schedule.get("suppressed_incident_count") or 0)
        schedule_note = (
            f" Zenduty incident reporting is suppressed while off schedule"
            f"{f' ({suppressed} hidden)' if suppressed else ''}."
        )
    schedule_window = ""
    if schedule.get("window_start") and schedule.get("window_end"):
        schedule_window = (
            f' · Window {time_span(schedule.get("window_start"))} to '
            f'{time_span(schedule.get("window_end"))}'
        )
    elif schedule.get("window_end"):
        schedule_window = f' · Until {time_span(schedule.get("window_end"))}'
    elif schedule.get("window_start"):
        schedule_window = f' · From {time_span(schedule.get("window_start"))}'
    body = "\n".join(rows) or f'<p class="empty">No filtered open incidents.{e(schedule_note)}</p>'
    prs_body = "\n".join(pr_rows) or '<p class="empty">No open Dependabot PRs.</p>'
    concourse_body = "\n".join(concourse_rows) or '<p class="empty">No failed Concourse jobs.</p>'
    concourse_snooze_note = (
        f'<p class="muted">Concourse failures are snoozed until <code>{e(concourse_snooze.get("until"))}</code>. '
        f'Clear with <code>python3 -m lana_zenduty_monitor unsnooze-concourse</code>.</p>'
        if concourse_snooze.get("active")
        else '<p class="muted">Snooze with <code>python3 -m lana_zenduty_monitor snooze-concourse --minutes 60</code>.</p>'
    )
    runs_body = "\n".join(run_rows) or '<tr><td colspan="6">No monitor runs recorded.</td></tr>'
    color = status.get("color", "gray")
    counts = status.get("counts", {})
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LANA Zenduty Monitor</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #111318;
      --panel: #1b1f27;
      --text: #f4f6f8;
      --muted: #aeb7c2;
      --border: #303845;
      --red: #ff5c5c;
      --yellow: #f6d32d;
      --blue: #62a0ea;
      --green: #33d17a;
      --gray: #9a9996;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    main {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }}
    .top {{
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: flex-start;
      border-bottom: 1px solid var(--border);
      padding-bottom: 20px;
      margin-bottom: 24px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 8px; font-size: 28px; }}
    .state {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    button {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 6px 10px;
      font: inherit;
      cursor: pointer;
    }}
    button:hover {{ border-color: var(--muted); }}
    .dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--{e(color)});
    }}
    .counts {{
      display: grid;
      grid-template-columns: repeat(3, minmax(110px, 1fr));
      gap: 10px;
      min-width: 360px;
    }}
    .count, .incident, .runs {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
    }}
    .count {{ padding: 14px; }}
    .count strong {{ display: block; font-size: 24px; }}
    .muted, dt {{ color: var(--muted); }}
    .incident {{
      padding: 18px;
      margin-bottom: 16px;
    }}
    .incident header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    .incident h2 {{ margin-bottom: 4px; font-size: 20px; }}
    .title {{ margin-bottom: 0; }}
    .pill {{
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 4px 10px;
      text-transform: uppercase;
      font-size: 12px;
      letter-spacing: .04em;
    }}
    dl {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
      margin: 0 0 16px;
    }}
    dt {{ font-size: 12px; text-transform: uppercase; }}
    dd {{ margin: 2px 0 0; overflow-wrap: anywhere; }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: rgba(255, 255, 255, .04);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
    }}
    a {{ color: #8ab4f8; }}
    .empty {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-top: 1px solid var(--border);
      padding: 8px;
      text-align: left;
      vertical-align: top;
    }}
    .runs {{ padding: 12px; overflow-x: auto; }}
  </style>
</head>
<body>
  <main>
    <section class="top">
      <div>
        <h1>LANA Zenduty Monitor</h1>
        <div class="toolbar">
          <p class="state"><span class="dot"></span>{e(status_label(color))}</p>
          <button id="time-toggle" type="button">Show UTC</button>
        </div>
        <p class="muted">Updated {time_span(status.get('updated_at'))} · Assignee filter <code>{e(status.get('assignee_filter') or assignee_filter_display(config))}</code></p>
        <p class="muted">Schedule: <strong>{e(schedule.get('status'))}</strong>{schedule_window}</p>
        {f'<p class="muted">Error: {e(status.get("error"))}</p>' if status.get("error") else ""}
        {f'<p class="muted">Zenduty error: {e(status.get("zenduty_error"))}</p>' if status.get("zenduty_error") else ""}
        {f'<p class="muted">Schedule error: {e(schedule.get("error"))}</p>' if schedule.get("error") else ""}
      </div>
      <div class="counts">
        <div class="count"><strong>{e(counts.get('triggered', 0))}</strong><span>Triggered</span></div>
        <div class="count"><strong>{e(counts.get('acknowledged', 0))}</strong><span>Acknowledged</span></div>
        <div class="count"><strong>{e(counts.get('concourse_failed', 0))}</strong><span>CI Failed</span></div>
      </div>
    </section>
    <section>{body}</section>
    <section>
      <h2>Concourse Failures</h2>
      <p class="muted">Failed, errored, or aborted jobs from {e(config.concourse_url)}.</p>
      {concourse_snooze_note}
      {f'<p class="muted">Error: {e(status.get("concourse_error"))}</p>' if status.get("concourse_error") else ""}
      {concourse_body}
    </section>
    <section>
      <h2>Dependabot Pull Requests</h2>
      <p class="muted">Ready means open, not draft, and all reported checks completed successfully.</p>
      {f'<p class="muted">Error: {e(status.get("dependabot_error"))}</p>' if status.get("dependabot_error") else ""}
      {prs_body}
    </section>
    <section>
      <h2>Recent Runs</h2>
      <div class="runs">
        <table>
          <thead><tr><th>Kind</th><th>Started</th><th>Finished</th><th>Status</th><th>Log</th><th>Error</th></tr></thead>
          <tbody>{runs_body}</tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const pad = value => String(value).padStart(2, '0');
    const formatLocal = date => {{
      const offset = -date.getTimezoneOffset();
      const sign = offset >= 0 ? '+' : '-';
      const offsetHours = pad(Math.floor(Math.abs(offset) / 60));
      const offsetMinutes = pad(Math.abs(offset) % 60);
      return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ` +
        `${{pad(date.getHours())}}:${{pad(date.getMinutes())}} UTC${{sign}}${{offsetHours}}:${{offsetMinutes}}`;
    }};
    let useUtc = false;
    const renderTimes = () => {{
      document.querySelectorAll('[data-time]').forEach(node => {{
        const raw = node.getAttribute('data-time');
        const date = new Date(raw);
        node.textContent = Number.isNaN(date.getTime())
          ? raw
          : useUtc
            ? date.toISOString().replace('.000Z', 'Z')
            : formatLocal(date);
      }});
      document.getElementById('time-toggle').textContent = useUtc ? 'Show local time' : 'Show UTC';
    }};
    document.getElementById('time-toggle').addEventListener('click', () => {{
      useUtc = !useUtc;
      renderTimes();
    }});
    renderTimes();
  </script>
</body>
</html>
"""
    (config.state_dir / "details.html").write_text(html_doc)


def status_default(config: MonitorConfig) -> None:
    status_path = config.state_dir / "status.json"
    details_path = config.state_dir / "details.html"
    if not status_path.exists():
        write_status(config, [])
    elif not details_path.exists():
        store = Store(config.state_dir)
        try:
            write_details_html(config, json.loads(status_path.read_text()), store=store)
        finally:
            store.close()


def do_poll(config: MonitorConfig) -> int:
    lock_file = acquire_poll_lock(config)
    if lock_file is None:
        print("poll skipped: another poll is already running")
        return 0
    store = Store(config.state_dir)
    log_path = config.state_dir / "logs" / f"{utc_now().replace(':', '').replace('-', '')}-poll.md"
    run_id = store.begin_run("poll", log_path)
    try:
        proc = run_codex(config, poll_prompt(config), log_path)
        if proc.returncode != 0:
            raise RuntimeError(f"codex exited with {proc.returncode}")
        payload = extract_json(proc.stdout)
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object from Codex poll")
        zenduty_error = zenduty_error_from_run(payload, proc)
        schedule = normalize_schedule(payload.get("schedule"), config)
        incidents = [] if zenduty_error else add_assignee_aliases(config, normalize_incidents(payload))
        if schedule_blocks_reporting(schedule) and incidents:
            schedule["suppressed_incident_count"] = len(incidents)
            incidents = []
        concourse_failures, concourse_error = normalize_concourse_failures(
            payload.get("concourse_failures"),
            config,
        )
        dependabot_prs, dependabot_error = fetch_dependabot_prs(config)
        new_incidents = store.upsert_incidents(incidents)
        incidents = store.enrich_incidents(incidents)
        write_status(
            config,
            incidents,
            dependabot_prs=dependabot_prs,
            concourse_failures=concourse_failures,
            schedule=schedule,
            dependabot_error=dependabot_error,
            concourse_error=concourse_error,
            zenduty_error=zenduty_error,
            store=store,
        )
        store.finish_run(run_id, zenduty_error is None, zenduty_error)

        print(
            json.dumps(
                {
                    "open": len(incidents),
                    "new": len(new_incidents),
                    "dependabot_open": len(dependabot_prs),
                    "dependabot_ready": len(
                        [pr for pr in dependabot_prs if pr.get("ready_to_merge")]
                    ),
                    "dependabot_error": dependabot_error,
                    "concourse_failed": len(concourse_failures),
                    "concourse_error": concourse_error,
                    "schedule": schedule,
                    "zenduty_error": zenduty_error,
                },
                indent=2,
            )
        )
        if config.triage_new_incidents and zenduty_error is None:
            for incident in new_incidents:
                do_triage(config, incident["unique_id"], store=store)
        return 1 if zenduty_error else 0
    except Exception as exc:
        write_status(config, [], error=str(exc), store=store)
        store.finish_run(run_id, False, str(exc))
        print(f"poll failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def do_triage(config: MonitorConfig, incident_id: str, store: Store | None = None) -> int:
    owns_store = store is None
    if store is None:
        store = Store(config.state_dir)
    incident = store.incident(incident_id)
    if not incident:
        incident = {
            "unique_id": incident_id,
            "incident_number": None,
            "status": "triggered",
            "title": "",
            "creation_date": None,
            "assigned_to": config.assignee_user_id[0] if config.assignee_user_id else None,
            "assignee_alias": (
                (config.assignee_aliases or {}).get(config.assignee_user_id[0])
                if config.assignee_user_id
                else None
            ),
        }

    log_path = config.state_dir / "logs" / f"incident-{incident_id}-triage.md"
    run_id = store.begin_run("triage", log_path)
    try:
        proc = run_codex(config, triage_prompt(incident), log_path)
        if proc.returncode != 0:
            raise RuntimeError(f"codex exited with {proc.returncode}")
        summary = proc.stdout.strip()
        try:
            payload = extract_json(proc.stdout)
            if isinstance(payload, dict) and payload.get("summary"):
                summary = str(payload["summary"])
        except Exception:
            pass
        store.mark_triaged(incident_id, "done", summary, log_path)
        store.finish_run(run_id, True)
        refresh_status_from_disk(config, store)
        print(summary)
        return 0
    except Exception as exc:
        store.mark_triaged(incident_id, "failed", str(exc), log_path)
        store.finish_run(run_id, False, str(exc))
        refresh_status_from_disk(config, store)
        print(f"triage failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if owns_store:
            store.close()


def do_daemon(config: MonitorConfig) -> int:
    while True:
        do_poll(config)
        time.sleep(config.poll_interval_seconds)


def refresh_status_from_disk(config: MonitorConfig, store: Store) -> None:
    status_path = config.state_dir / "status.json"
    if not status_path.exists():
        return
    status = json.loads(status_path.read_text())
    incidents = normalize_incidents({"incidents": status.get("incidents", [])})
    incidents = add_assignee_aliases(config, incidents)
    incidents = store.enrich_incidents(incidents)
    dependabot = status.get("dependabot", {})
    dependabot_prs = dependabot.get("prs", []) if isinstance(dependabot, dict) else []
    concourse = status.get("concourse", {})
    concourse_failures = (
        concourse.get("failures", []) if isinstance(concourse, dict) else []
    )
    write_status(
        config,
        incidents,
        dependabot_prs=dependabot_prs,
        concourse_failures=concourse_failures,
        schedule=normalize_schedule(status.get("schedule"), config),
        error=status.get("error"),
        dependabot_error=status.get("dependabot_error"),
        concourse_error=status.get("concourse_error"),
        zenduty_error=status.get("zenduty_error"),
        store=store,
    )


def do_snooze_concourse(config: MonitorConfig, minutes: int) -> int:
    if minutes <= 0:
        print("minutes must be positive", file=sys.stderr)
        return 2
    snooze = set_concourse_snooze(config, minutes)
    store = Store(config.state_dir)
    try:
        refresh_status_from_disk(config, store)
    finally:
        store.close()
    print(json.dumps({"concourse_snoozed_until": snooze["until"]}, indent=2))
    return 0


def do_unsnooze_concourse(config: MonitorConfig) -> int:
    clear_concourse_snooze(config)
    store = Store(config.state_dir)
    try:
        refresh_status_from_disk(config, store)
    finally:
        store.close()
    print(json.dumps({"concourse_snoozed_until": None}, indent=2))
    return 0


def print_status(config: MonitorConfig, write_default: bool) -> int:
    if write_default:
        status_default(config)
    path = config.state_dir / "status.json"
    if not path.exists():
        print(f"No status file at {path}", file=sys.stderr)
        return 1
    print(path.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("poll")
    sub.add_parser("daemon")
    snooze = sub.add_parser("snooze-concourse")
    snooze.add_argument("--minutes", type=int, default=60)
    sub.add_parser("unsnooze-concourse")
    triage = sub.add_parser("triage")
    triage.add_argument("incident_id")
    status = sub.add_parser("status")
    status.add_argument("--write-default", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.command == "poll":
        return do_poll(config)
    if args.command == "daemon":
        return do_daemon(config)
    if args.command == "snooze-concourse":
        return do_snooze_concourse(config, args.minutes)
    if args.command == "unsnooze-concourse":
        return do_unsnooze_concourse(config)
    if args.command == "triage":
        return do_triage(config, args.incident_id)
    if args.command == "status":
        return print_status(config, args.write_default)
    parser.error("unknown command")
    return 2
