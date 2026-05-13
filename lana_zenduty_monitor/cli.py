from __future__ import annotations

import argparse
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ASSIGNEE = "a7eecf16-e8fa-4903-90ea-8"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "lana-zenduty-monitor" / "config.toml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "lana-zenduty-monitor"
DEFAULT_LANA_BANK_DIR = Path.home() / "source" / "repos" / "lana-bank"


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
    assignee_user_id: str = DEFAULT_ASSIGNEE
    codex: CodexConfig = CodexConfig()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        assignee_user_id=str(monitor.get("assignee_user_id", DEFAULT_ASSIGNEE)),
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
        return dict(row) if row else None

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


def poll_prompt(config: MonitorConfig) -> str:
    assignee_filter = (
        f"Only include incidents whose Zenduty detail assigned_to exactly equals {config.assignee_user_id!r}."
        if config.assignee_user_id
        else "Do not filter by assignee."
    )
    return textwrap.dedent(
        f"""
        Use MCP Drua Zenduty tools from this Codex session.

        List all open Zenduty incidents with statuses triggered and acknowledged.
        Fetch detail records as needed to include assigned_to and to apply the assignee filter.
        {assignee_filter}

        Return only valid JSON, with no markdown and no prose:
        {{
          "checked_at": "<UTC ISO-8601 timestamp>",
          "incidents": [
            {{
              "unique_id": "<Zenduty unique_id>",
              "incident_number": 123,
              "status": "triggered",
              "title": "...",
              "creation_date": "...",
              "urgency": 1,
              "assigned_to": "..."
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

        Fetch the incident details, inspect linked Honeycomb context, identify the code path,
        classify as false positive or real problem, and add a Zenduty note with the final summary.

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
            }
        )
    return incidents


def write_status(
    config: MonitorConfig,
    incidents: list[dict[str, Any]],
    error: str | None = None,
    store: Store | None = None,
) -> None:
    triggered = [i for i in incidents if i.get("status") == "triggered"]
    acknowledged = [i for i in incidents if i.get("status") == "acknowledged"]
    color = "red" if triggered else "yellow" if acknowledged else "green"
    if error:
        color = "gray"

    status = {
        "updated_at": utc_now(),
        "color": color,
        "error": error,
        "counts": {
            "triggered": len(triggered),
            "acknowledged": len(acknowledged),
            "open": len(triggered) + len(acknowledged),
        },
        "assignee_user_id": config.assignee_user_id,
        "details_html": str(config.state_dir / "details.html"),
        "logs_dir": str(config.state_dir / "logs"),
        "incidents": incidents,
    }
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True))
    write_details_html(config, status, store=store)


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def status_label(color: str) -> str:
    return {
        "red": "Triggered incidents",
        "yellow": "Acknowledged incidents",
        "green": "No open incidents",
        "gray": "Monitor unavailable",
    }.get(color, "Unknown")


def write_details_html(
    config: MonitorConfig,
    status: dict[str, Any],
    store: Store | None = None,
) -> None:
    incidents = status.get("incidents", [])
    runs = store.recent_runs() if store is not None else []
    rows = []
    for incident in incidents:
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
                <div><dt>Assigned To</dt><dd><code>{e(incident.get('assigned_to'))}</code></dd></div>
                <div><dt>Created</dt><dd>{e(incident.get('creation_date'))}</dd></div>
                <div><dt>First Seen</dt><dd>{e(incident.get('first_seen_at'))}</dd></div>
                <div><dt>Triage</dt><dd>{e(incident.get('triage_status', 'pending'))} · {log_link}</dd></div>
              </dl>
              <section>
                <h3>Triage Summary</h3>
                <p>{e(incident.get('triage_summary') or 'No triage summary recorded yet.')}</p>
              </section>
            </article>
            """
        )

    run_rows = []
    for run in runs:
        success = "ok" if run.get("success") else "failed"
        log_path = run.get("log_path")
        log_link = f'<a href="file://{e(log_path)}">log</a>' if log_path else ""
        run_rows.append(
            f"<tr><td>{e(run.get('kind'))}</td><td>{e(run.get('started_at'))}</td>"
            f"<td>{e(run.get('finished_at'))}</td><td>{success}</td><td>{log_link}</td>"
            f"<td>{e(run.get('error'))}</td></tr>"
        )

    body = "\n".join(rows) or '<p class="empty">No filtered open incidents.</p>'
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
        <p class="state"><span class="dot"></span>{e(status_label(color))}</p>
        <p class="muted">Updated {e(status.get('updated_at'))} · Assignee filter <code>{e(status.get('assignee_user_id') or 'none')}</code></p>
        {f'<p class="muted">Error: {e(status.get("error"))}</p>' if status.get("error") else ""}
      </div>
      <div class="counts">
        <div class="count"><strong>{e(counts.get('triggered', 0))}</strong><span>Triggered</span></div>
        <div class="count"><strong>{e(counts.get('acknowledged', 0))}</strong><span>Acknowledged</span></div>
        <div class="count"><strong>{e(counts.get('open', 0))}</strong><span>Open</span></div>
      </div>
    </section>
    <section>{body}</section>
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
    store = Store(config.state_dir)
    log_path = config.state_dir / "logs" / f"{utc_now().replace(':', '').replace('-', '')}-poll.md"
    run_id = store.begin_run("poll", log_path)
    try:
        proc = run_codex(config, poll_prompt(config), log_path)
        if proc.returncode != 0:
            raise RuntimeError(f"codex exited with {proc.returncode}")
        payload = extract_json(proc.stdout)
        incidents = normalize_incidents(payload)
        new_incidents = store.upsert_incidents(incidents)
        incidents = store.enrich_incidents(incidents)
        write_status(config, incidents, store=store)
        store.finish_run(run_id, True)

        print(json.dumps({"open": len(incidents), "new": len(new_incidents)}, indent=2))
        if config.triage_new_incidents:
            for incident in new_incidents:
                do_triage(config, incident["unique_id"], store=store)
        return 0
    except Exception as exc:
        write_status(config, [], error=str(exc), store=store)
        store.finish_run(run_id, False, str(exc))
        print(f"poll failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


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
            "assigned_to": config.assignee_user_id or None,
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
    incidents = store.enrich_incidents(incidents)
    write_status(config, incidents, error=status.get("error"), store=store)


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
    if args.command == "triage":
        return do_triage(config, args.incident_id)
    if args.command == "status":
        return print_status(config, args.write_default)
    parser.error("unknown command")
    return 2
