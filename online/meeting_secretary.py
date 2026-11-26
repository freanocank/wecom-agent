"""Prepare post-meeting work for an agent and deliver its structured report."""

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from getpass import getpass
from hashlib import sha256
from html import escape
from pathlib import Path
from subprocess import TimeoutExpired

from . import group_webhook, meetings
from .wecom_cli import WecomCliError, require_confirmation

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TURN_LABEL = re.compile(
    r"^\s*\d+\s+((?:\d{4}-\d{2}-\d{2}[ T])?\d{1,3}:\d{2}(?::\d{2})?)\s+(.+?)[:：]"
)


@dataclass(frozen=True)
class Project:
    subjects: tuple[str, ...]
    knowledge_sources: tuple[str, ...]
    webhook_env: str | None
    lookback_hours: int
    utc_offset_minutes: int
    database: Path
    group: str | None = None
    webhook_file: Path | None = None

    @classmethod
    def load(cls, config_path):
        path = Path(config_path).expanduser().resolve()
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise TypeError("The meeting secretary config must be a JSON object.")
        subjects = _strings(config.get("subjects"), "subjects")
        if not subjects:
            raise ValueError("Configure at least one exact meeting subject.")
        group = config.get("group")
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise ValueError("group must be a non-empty string.")
        webhook_env = config.get("webhook_env")
        if webhook_env is not None and (
            not isinstance(webhook_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", webhook_env)
        ):
            raise ValueError("webhook_env must name an environment variable.")
        if group is None and webhook_env is None:
            raise ValueError("Configure the target group or an existing webhook_env.")
        hours = _bounded_integer(config.get("lookback_hours", 24), 1, 168, "lookback_hours")
        offset = _bounded_integer(config.get("utc_offset_minutes", 480), -720, 840, "utc_offset_minutes")
        return cls(
            subjects=subjects,
            knowledge_sources=_strings(config.get("knowledge_sources", []), "knowledge_sources"),
            webhook_env=webhook_env,
            lookback_hours=hours,
            utc_offset_minutes=offset,
            database=path.with_name(path.name + ".db"),
            group=group.strip() if group else None,
            webhook_file=path.with_name(path.name + ".webhook") if webhook_env is None else None,
        )


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings.")
    return tuple(item.strip() for item in value)


def _bounded_integer(value, lower, upper, name):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer between {lower} and {upper}.")
    return value


def _write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def setup_project(config_path, group, subjects, url):
    path = Path(config_path).expanduser().resolve()
    names = _strings(subjects, "subjects")
    if not group.strip() or not names:
        raise ValueError("Provide a group name and at least one exact meeting subject.")
    group_webhook.validate_url(url)
    document = {"group": group.strip(), "subjects": list(dict.fromkeys(names))}
    credential = path.with_name(path.name + ".webhook")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or credential.exists():
        raise ValueError("The project or its webhook file already exists.")
    _write_private(credential, url + "\n")
    try:
        _write_private(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        credential.unlink()
        raise
    return {"status": "configured", **document}


def _webhook_url(project):
    if project.webhook_env:
        return os.environ.get(project.webhook_env, "")
    try:
        return project.webhook_file.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        raise ValueError("Configure the Webhook for this group before sending.") from None


def _open_state(project):
    descriptor = os.open(project.database, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    database = sqlite3.connect(project.database, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("""
        CREATE TABLE IF NOT EXISTS meeting_runs (
            key TEXT PRIMARY KEY,
            meeting_id TEXT NOT NULL,
            sub_meeting_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            begin_time TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            transcript_sha256 TEXT,
            evidence TEXT
        )
    """)
    return database


def _evidence_labels(lines):
    labels = []
    for number, line in enumerate(lines, 1):
        label = f"转写第 {number} 行"
        match = TURN_LABEL.match(line)
        if match:
            label += f"（{match[1]} {match[2]}）"
        labels.append(label)
    return labels


def _search_window(project, now, on_date):
    tz = timezone(timedelta(minutes=project.utc_offset_minutes))
    if on_date is not None:
        return (
            datetime.combine(on_date, time.min, tz).strftime(TIME_FORMAT),
            datetime.combine(on_date, time(23, 59, 59), tz).strftime(TIME_FORMAT),
        )
    until = (now or datetime.now(timezone.utc)).astimezone(tz)
    return (until - timedelta(hours=project.lookback_hours)).strftime(TIME_FORMAT), until.strftime(TIME_FORMAT)


def next_meeting(project, *, now=None, runner=None, latest=False, subject=None, on_date=None):
    if subject is not None and subject not in project.subjects:
        raise ValueError("The requested meeting is outside this project's configured subjects.")
    subjects = (subject,) if subject is not None else project.subjects
    begin_time, end_time = _search_window(project, now, on_date)
    occurrences = meetings.list_occurrences(begin_time, end_time, subjects, runner)
    waiting = []
    with closing(_open_state(project)) as database:
        with database:
            database.executemany(
                """
                INSERT INTO meeting_runs (key, meeting_id, sub_meeting_id, subject, begin_time)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET subject = excluded.subject, begin_time = excluded.begin_time
                WHERE meeting_runs.state IN ('pending', 'ready')
                  AND (meeting_runs.subject != excluded.subject OR meeting_runs.begin_time != excluded.begin_time)
                """,
                [
                    (item.key, item.meeting_id, item.sub_meeting_id, item.subject, item.begin_time)
                    for item in occurrences
                ],
            )
        attention = [
            {"key": row["key"], "subject": row["subject"], "begin_time": row["begin_time"]}
            for row in database.execute(
                "SELECT key, subject, begin_time FROM meeting_runs WHERE state = 'sending' ORDER BY begin_time"
            )
        ]
        recent = database.execute(
            """
            SELECT * FROM meeting_runs
            WHERE (state IN ('pending', 'ready') OR ?) AND begin_time BETWEEN ? AND ?
            ORDER BY begin_time, key
            """,
            (latest, begin_time, end_time),
        ).fetchall()
        for row in reversed(recent) if latest else recent:
            if row["subject"] not in subjects:
                continue
            occurrence = meetings.Occurrence(
                row["meeting_id"], row["sub_meeting_id"], row["subject"], row["begin_time"]
            )
            try:
                detail = meetings.get_occurrence(occurrence, runner)
                if detail.get("subject") not in subjects:
                    continue
                if detail.get("meeting_status") != "end":
                    waiting.append({"subject": occurrence.subject, "reason": "meeting_not_ended"})
                    continue
                if row["state"] == "sending":
                    return {"status": "send_unconfirmed", "key": occurrence.key, "attention": attention}
                if row["state"] in {"sent", "empty"}:
                    return {"status": "already_processed", "key": occurrence.key, "outcome": row["state"]}
                lines = meetings.get_transcript(occurrence, runner)
            except (meetings.MeetingError, WecomCliError, TimeoutExpired) as error:
                waiting.append({"subject": occurrence.subject, "reason": "read_failed", "detail": str(error)})
                if latest:
                    break
                continue
            if not lines:
                waiting.append({"subject": occurrence.subject, "reason": "transcript_not_ready"})
                if latest:
                    break
                continue
            digest = sha256("\n".join(lines).encode("utf-8")).hexdigest()
            with database:
                update = database.execute(
                    """
                    UPDATE meeting_runs SET state = 'ready', subject = ?, transcript_sha256 = ?, evidence = ?
                    WHERE key = ? AND state IN ('pending', 'ready')
                    """,
                    (detail["subject"], digest, json.dumps(_evidence_labels(lines)), occurrence.key),
                )
            if update.rowcount:
                return {
                    "status": "ready",
                    "meeting": {
                        "key": occurrence.key,
                        "subject": detail["subject"],
                        "begin_time": occurrence.begin_time,
                        "transcript_sha256": digest,
                    },
                    "transcript": [{"line": number, "text": line} for number, line in enumerate(lines, 1)],
                    "knowledge_sources": [
                        {"source": number, "location": location}
                        for number, location in enumerate(project.knowledge_sources, 1)
                    ],
                    "attention": attention,
                    **({"group": project.group} if project.group else {}),
                }
            if latest:
                return {"status": "state_changed", "key": occurrence.key, "attention": attention}
    return {"status": "waiting" if waiting else "idle", "meetings": waiting, "attention": attention}


def _field(item, name, *, optional=False):
    value = item.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return _plain(value)


def _plain(value):
    text = escape(" ".join(value.split()), quote=False)
    return re.sub(r"([\\\x60\[\]*_])", r"\\\1", text)


def _references(value, labels, name, *, required=True):
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"{name} must contain valid reference numbers.")
    if any(type(number) is not int or not 1 <= number <= len(labels) for number in value):
        raise ValueError(f"{name} contains an unknown reference number.")
    return "、".join(_plain(labels[number - 1]) for number in dict.fromkeys(value))


def _items(report, name, *, optional=False):
    items = report.get(name, [] if optional else None)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError(f"{name} must be an array of objects.")
    return items


def _priority(item):
    priority = _field(item, "priority", optional=True)
    return f"【{priority}】" if priority else ""


def _headline(item, name):
    headline = f"{_priority(item)}**{_field(item, name)}**"
    context = _field(item, "context", optional=True)
    return f"{headline}（{context}）" if context else headline


def _format(report, row, project, *, detail=False):
    topic = _field(report, "topic", optional=True)
    decisions = _items(report, "decisions", optional=True)
    actions = _items(report, "actions")
    issues = _items(report, "open_issues")
    if not (decisions or actions or issues):
        return ""

    evidence = json.loads(row["evidence"])
    decision_entries, action_entries, issue_entries = [], [], []
    for decision in decisions:
        source = _references(decision.get("evidence"), evidence, "evidence")
        decision_entries.append((_headline(decision, "decision"), source, ""))
    for action in actions:
        owner = _field(action, "owner", optional=True)
        task = _field(action, "task")
        due = _field(action, "due", optional=True)
        assignee = f"**{owner}** " if owner else ""
        deadline = f"（截止 {due}）" if due else ""
        source = _references(action.get("evidence"), evidence, "evidence")
        text = f"{_priority(action)}{assignee}{task}{deadline}"
        action_entries.append((text, source, ""))
    for issue in issues:
        headline = _headline(issue, "issue")
        proposal = _field(issue, "proposal")
        first_step = _field(issue, "first_step")
        source = _references(issue.get("evidence"), evidence, "evidence")
        references = _references(issue.get("sources", []), project.knowledge_sources, "sources", required=False)
        text = f"{headline}。建议 {proposal.rstrip('。')}，第一步 {first_step}"
        issue_entries.append((text, source, references))

    subject = _plain(row["subject"])
    title = f"## {topic or subject} 会后纪要"
    begin_time = row["begin_time"]
    metadata = [begin_time if detail else begin_time[:10]]
    if detail and topic:
        metadata.insert(0, f"原会议 {subject}")
    sections = ["\n".join([title, *metadata])]
    for heading, entries in (
        ("会上定下的事", decision_entries), ("接下来谁做什么", action_entries), ("待拍板", issue_entries),
    ):
        if not entries:
            continue
        lines = [f"**{heading}**"]
        for text, source, references in entries:
            lines.append(f"• {text}")
            if detail:
                lines.append(f"依据 {source}")
                if references:
                    lines.append(f"项目资料 {references}")
        sections.append(("\n" if detail else "\n\n").join(lines))
    return "\n\n".join(sections)


def _load_run(database, project, report):
    if not isinstance(report, dict):
        raise TypeError("The meeting report must be a JSON object.")
    if not isinstance(report.get("meeting_key"), str):
        raise TypeError("The report must contain a meeting_key string.")
    row = database.execute("SELECT * FROM meeting_runs WHERE key = ?", (report["meeting_key"],)).fetchone()
    if row is None or row["subject"] not in project.subjects or row["transcript_sha256"] is None:
        raise ValueError("Prepare a matching meeting before submitting its report.")
    if report.get("transcript_sha256") != row["transcript_sha256"]:
        raise ValueError("The report belongs to an older or different transcript. Prepare the meeting again.")
    return row


def preview(project, report, *, detail=False):
    with closing(_open_state(project)) as database:
        return _format(report, _load_run(database, project, report), project, detail=detail)


def _change_state(database, key, old, new, digest):
    with database:
        count = database.execute(
            """
            UPDATE meeting_runs SET state = ?
            WHERE key = ? AND state = ? AND transcript_sha256 = ?
            """,
            (new, key, old, digest),
        ).rowcount
    return count == 1


def publish(project, report, *, confirmed=False, group=None):
    require_confirmation(confirmed, "publish meeting report")
    if group != project.group:
        raise ValueError("The requested group must match the group configured for this project.")
    with closing(_open_state(project)) as database:
        row = _load_run(database, project, report)
        key, digest = row["key"], row["transcript_sha256"]
        if row["state"] in {"sent", "empty"}:
            return {"status": "already_processed", "key": key}
        if row["state"] == "sending":
            raise ValueError("A previous send is in progress or unconfirmed. Check the group before resolving it.")
        markdown = _format(report, row, project)
        if not markdown:
            changed = _change_state(database, key, "ready", "empty", digest)
            return {"status": "empty" if changed else "state_changed", "key": key}

        url = _webhook_url(project)
        group_webhook.validate_url(url)
        payload = group_webhook.prepare_message(url, markdown)
        if not _change_state(database, key, "ready", "sending", digest):
            return {"status": "state_changed", "key": key}
        try:
            group_webhook.send_message(url, payload)
        except group_webhook.WebhookRejected:
            _change_state(database, key, "sending", "ready", digest)
            raise
        _change_state(database, key, "sending", "sent", digest)
        return {"status": "sent", "key": key}


def resolve_delivery(project, key, delivered):
    with closing(_open_state(project)) as database, database:
        state = "sent" if delivered else "ready"
        count = database.execute(
            "UPDATE meeting_runs SET state = ? WHERE key = ? AND state = 'sending'", (state, key)
        ).rowcount
        if not count:
            raise ValueError("No unconfirmed delivery found for this meeting key.")
        return {"status": state, "key": key}


def _read_report(path):
    if path == "-":
        text = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        text = Path(path).read_text(encoding="utf-8-sig")
    return json.loads(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to the project JSON file")
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="Configure a meeting and its target group")
    setup_parser.add_argument("--group", required=True, help="Exact target group name")
    setup_parser.add_argument("--subject", required=True, action="append", help="Exact meeting title (repeatable)")
    setup_parser.add_argument("--webhook-stdin", action="store_true", help="Read the webhook from secure stdin")
    next_parser = commands.add_parser("next", help="Find the next ended meeting with a readable transcript")
    next_parser.add_argument("--latest", action="store_true", help="Select the latest ended meeting and keep its state")
    next_parser.add_argument("--subject", help="Select one configured meeting title")
    next_parser.add_argument("--date", type=date.fromisoformat, help="Find meetings on a specific date (YYYY-MM-DD)")
    next_parser.add_argument("--hermes-gate", action="store_true", help="Emit a Hermes wakeAgent decision")
    preview_parser = commands.add_parser("preview", help="Render a report without sending it")
    preview_parser.add_argument("report", help="JSON file or - for stdin")
    preview_parser.add_argument("--detail", action="store_true", help="Include transcript and project source references")
    publish_parser = commands.add_parser("publish", help="Send a prepared report to the configured group")
    publish_parser.add_argument("report", help="JSON file or - for stdin")
    publish_parser.add_argument("--group", help="Exact group configured for this project")
    publish_parser.add_argument("--confirmed", action="store_true", help="Authorize delivery to the configured group")
    resolve_parser = commands.add_parser("resolve", help="Record the result after checking an unconfirmed delivery")
    resolve_parser.add_argument("key", help="Meeting key reported in attention")
    resolve_parser.add_argument("--delivered", required=True, choices=["yes", "no"])
    args = parser.parse_args()

    try:
        if args.command == "setup":
            if args.webhook_stdin:
                url = sys.stdin.buffer.read().decode("utf-8-sig").strip()
            else:
                if not sys.stdin.isatty():
                    raise ValueError("Configure the Webhook in an interactive terminal or pass it through secure stdin.")
                url = getpass("WeCom group Webhook: ").strip()
            result = setup_project(args.config, args.group, args.subject, url)
        elif args.command == "next":
            project = Project.load(args.config)
            result = next_meeting(
                project, latest=args.latest, subject=args.subject, on_date=args.date,
            )
            if args.hermes_gate:
                result = {
                    "wakeAgent": result["status"] == "ready",
                    "context": {"status": result["status"], "attention": result["attention"]},
                }
        elif args.command == "resolve":
            result = resolve_delivery(Project.load(args.config), args.key, args.delivered == "yes")
        elif args.command == "preview":
            result = preview(Project.load(args.config), _read_report(args.report), detail=args.detail)
        else:
            result = publish(
                Project.load(args.config), _read_report(args.report),
                confirmed=args.confirmed, group=args.group,
            )
    except (
        OSError, TypeError, ValueError, sqlite3.Error, TimeoutExpired, WecomCliError, meetings.MeetingError,
        group_webhook.WebhookRejected, group_webhook.WebhookUncertain,
    ) as error:
        parser.exit(1, f"{error}\n")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
