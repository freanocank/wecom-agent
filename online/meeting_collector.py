"""Archive ready WeCom meeting transcripts without publishing them."""

import argparse
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import NamedTemporaryFile

from . import meetings
from .wecom_cli import WecomCliError

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
COLLECTION_ERRORS = (meetings.MeetingError, WecomCliError, TimeoutExpired, OSError, ValueError, TypeError)
COLLECTOR_ERRORS = (*COLLECTION_ERRORS, sqlite3.Error)


@dataclass(frozen=True)
class Config:
    directory: Path
    subjects: frozenset[str]
    subject_pattern: re.Pattern | None
    exclude_subject_pattern: re.Pattern | None
    lookback_hours: int
    timezone: timezone
    not_before: datetime | None

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().resolve()
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("Expected a config JSON object.")
        subjects = settings.get("subjects", [])
        if not isinstance(subjects, list) or any(not isinstance(s, str) or not s.strip() for s in subjects):
            raise ValueError("Expected an array of exact meeting subjects.")
        subject_pattern = _pattern(settings.get("subject_pattern"))
        exclude_subject_pattern = _pattern(settings.get("exclude_subject_pattern"))
        if not subjects and subject_pattern is None:
            raise ValueError("Configure an exact meeting subject or a subject pattern.")
        hours = settings.get("lookback_hours", 48)
        offset = settings.get("utc_offset_minutes", 480)
        if type(hours) is not int or not 1 <= hours <= 168:
            raise ValueError("Lookback must be between 1 and 168 hours.")
        if type(offset) is not int or not -720 <= offset <= 840:
            raise ValueError("Timezone offset must be between -720 and 840 minutes.")
        tz = timezone(timedelta(minutes=offset))
        not_before = settings.get("not_before")
        if not_before is not None:
            not_before = _canonical_time(not_before).replace(tzinfo=tz)
        return cls(path.parent, frozenset(subjects), subject_pattern, exclude_subject_pattern, hours, tz, not_before)

    def matches_subject(self, subject):
        if not isinstance(subject, str):
            return False
        if subject in self.subjects:
            return True
        return bool(
            self.subject_pattern and self.subject_pattern.search(subject)
            and not (self.exclude_subject_pattern and self.exclude_subject_pattern.search(subject))
        )


def _pattern(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a non-empty subject pattern.")
    return re.compile(value, re.IGNORECASE)


def _canonical_time(value):
    parsed = datetime.strptime(value, TIME_FORMAT)
    if parsed.strftime(TIME_FORMAT) != value:
        raise ValueError("Expected a canonical local datetime.")
    return parsed


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stats():
    return {
        "checked_at": _now(), "status": "ok", "checked": 0, "saved": 0, "skipped": 0,
        "waiting": 0, "failed": 0, "errors": [],
    }


def _numeric_code(value):
    if type(value) is int and abs(value) <= 9_999_999_999:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,10}", value):
        return int(value)
    return None


def _error_code(error):
    if isinstance(error, WecomCliError):
        for raw in (error.stdout, error.stderr):
            try:
                body = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(body, dict):
                detail = body.get("error")
                code = _numeric_code(detail.get("code")) if isinstance(detail, dict) else None
                if code is None:
                    code = _numeric_code(body.get("errcode"))
                if code is not None:
                    return code
        return _numeric_code(error.returncode)
    if isinstance(error, meetings.MeetingError):
        return _numeric_code(error.code)
    if isinstance(error, OSError):
        return _numeric_code(error.errno)
    return None


def _record_error(stats, stage, error):
    entry = {"stage": stage, "type": type(error).__name__}
    code = _error_code(error)
    if code is not None:
        entry["code"] = code
    stats["errors"].append(entry)
    stats["failed"] += 1
    stats["status"] = "failed"


def _write_transcript(path, occurrence, detail, lines):
    meeting = {
        "key": occurrence.key, **occurrence.identity, "subject": detail["subject"],
        "begin_time": occurrence.begin_time,
        "transcript_sha256": sha256("\n".join(lines).encode("utf-8")).hexdigest(),
    }
    for field in ("begin_time", "end_time", "actual_begin_time", "actual_end_time"):
        value = detail.get(field)
        if (isinstance(value, str) and value) or type(value) is int:
            meeting[field] = value
    if "end_time" not in meeting and occurrence.end_time:
        meeting["end_time"] = occurrence.end_time
    record = {
        "collected_at": _now(), "meeting": meeting,
        "transcript": [{"line": number, "text": line} for number, line in enumerate(lines, 1)],
    }
    temporary_path = None
    try:
        with NamedTemporaryFile(mode="w", dir=path.parent, prefix=".transcript-", encoding="utf-8", delete=False) as file:
            temporary_path = Path(file.name)
            json.dump(record, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _run(config, stats, now, runner):
    archive = config.directory / "transcripts"
    archive.mkdir(mode=0o700, exist_ok=True)
    until = (now or datetime.now(timezone.utc)).astimezone(config.timezone)
    since = until - timedelta(hours=config.lookback_hours)
    if config.not_before is not None:
        since = max(since, config.not_before)
    if since > until:
        return
    begin_time = since.strftime(TIME_FORMAT)
    end_time = until.strftime(TIME_FORMAT)
    try:
        occurrences = meetings.list_occurrences(
            begin_time, end_time, config.subjects, runner, subject_matches=config.matches_subject,
            on_occurrence_error=lambda error: _record_error(stats, "occurrence", error),
        )
    except COLLECTION_ERRORS as error:
        _record_error(stats, "list", error)
        return
    for occurrence in occurrences:
        if not begin_time <= occurrence.begin_time <= end_time:
            continue
        stats["checked"] += 1
        target = archive / f"{occurrence.key}.json"
        stage = "detail"
        try:
            if target.is_file():
                stats["skipped"] += 1
                continue
            detail = meetings.get_occurrence(occurrence, runner)
            if not config.matches_subject(detail.get("subject")):
                stats["skipped"] += 1
                continue
            if detail.get("meeting_status") != "end":
                stats["waiting"] += 1
                continue
            stage = "transcript"
            lines = meetings.get_transcript(occurrence, runner)
            if not lines:
                stats["waiting"] += 1
                continue
            stage = "archive"
            _write_transcript(target, occurrence, detail, lines)
            stats["saved"] += 1
        except COLLECTION_ERRORS as error:
            _record_error(stats, stage, error)


def collect(config, *, now=None, runner=None):
    stats = _stats()
    try:
        lock = config.directory / ".collector.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(lock, timeout=0)) as database:
            try:
                database.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", 0) != 5 and str(error) != "database is locked":
                    raise
                stats["status"] = "busy"
                return stats
            _run(config, stats, now, runner)
    except COLLECTOR_ERRORS as error:
        _record_error(stats, "collector", error)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to the collector JSON configuration")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
    except (OSError, ValueError, TypeError, re.error) as error:
        stats = _stats()
        _record_error(stats, "config", error)
    else:
        stats = collect(config)
    print(json.dumps(stats), flush=True)
    return int(stats["status"] == "failed")


if __name__ == "__main__":
    sys.exit(main())
