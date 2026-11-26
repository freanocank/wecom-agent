import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from online import meeting_collector
from online.wecom_cli import WecomCliError


NOW = datetime(2026, 11, 26, 4, tzinfo=timezone.utc)


def occurrence(meeting_id, subject, *, sub_id="", start="2026-11-26 10:00:00", status="end"):
    return {
        "meeting_id": meeting_id, "sub_meeting_id": sub_id, "is_repeat_meeting": bool(sub_id),
        "subject": subject, "begin_time": start, "end_time": "2026-11-26 11:00:00", "meeting_status": status,
    }


class MeetingCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = self.root / "config.json"
        settings.write_text(json.dumps({
            "subjects": ["指定的会议"], "subject_pattern": "LQA|周会", "exclude_subject_pattern": "招聘",
            "not_before": "2026-11-26 09:00:00",
        }), encoding="utf-8")
        self.config = meeting_collector.Config.load(settings)
        self.archive = self.root / "transcripts"

    def test_end_scope_pagination_and_deduplication(self):
        known = occurrence("known", "指定的会议")
        first = occurrence("recurring", "lqa", sub_id="one")
        second = occurrence("recurring", "LQA", sub_id="two", status="started")
        excluded = occurrence("excluded", "招聘周会")
        old = occurrence("old", "LQA", start="2026-11-26 08:00:00")
        renamed = occurrence("renamed", "LQA")
        calls = []

        def runner(command, payload):
            calls.append((command, dict(payload)))
            if command == ["meeting", "list"]:
                self.assertEqual(payload["begin_time"], "2026-11-26 09:00:00")
                if "cursor" not in payload:
                    return {"created_meetings": [known, first, excluded, renamed],
                            "has_more": True, "next_cursor": "another"}
                return {"attended_meetings": [first, second, old]}
            if command == ["meeting", "get"]:
                identity = payload["meeting_ids"][0]
                self.assertNotIn(identity["meeting_id"], {"excluded", "old"})
                matches = [row for row in (known, first, second, renamed)
                           if row["meeting_id"] == identity["meeting_id"]
                           and row["sub_meeting_id"] == identity.get("sub_meeting_id", "")]
                detail = matches[0]
                if detail is renamed:
                    detail = dict(detail, subject="招聘周会")
                return {"meetings": [detail]}
            self.assertEqual(command, ["meeting", "original", "get"])
            self.assertNotEqual(payload["meeting_id"], "renamed")
            self.assertNotEqual(payload.get("sub_meeting_id"), "two")
            if "cursor" not in payload:
                return {"original_data": "发言一", "has_more": True, "next_cursor": "second"}
            return {"original_data": "发言二"}

        first_run = meeting_collector.collect(self.config, now=NOW, runner=runner)
        self.assertEqual((first_run["checked"], first_run["saved"], first_run["waiting"], first_run["skipped"]),
                         (4, 2, 1, 1))
        records = sorted(self.archive.glob("*.json"))
        self.assertEqual(len(records), 2)
        for path in records:
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.stem, record["meeting"]["key"])
            self.assertEqual(record["meeting"]["end_time"], "2026-11-26 11:00:00")
            self.assertEqual(record["meeting"]["transcript_sha256"], sha256("发言一\n发言二".encode()).hexdigest())
            self.assertEqual(record["transcript"], [{"line": 1, "text": "发言一"}, {"line": 2, "text": "发言二"}])
        calls.clear()
        replay = meeting_collector.collect(self.config, now=NOW, runner=runner)
        self.assertEqual((replay["saved"], replay["skipped"], replay["waiting"]), (0, 3, 1))
        self.assertNotIn(["meeting", "original", "get"], [command for command, _ in calls])

    def test_partial_failure_is_retried_without_saving_partial_text(self):
        records = {name: occurrence(name, "LQA") for name in ("broken", "valid", "empty")}
        fail = True

        def runner(command, payload):
            if command == ["meeting", "list"]:
                return {"created_meetings": list(records.values())}
            if command == ["meeting", "get"]:
                return {"meetings": [records[payload["meeting_ids"][0]["meeting_id"]]]}
            if payload["meeting_id"] == "broken" and fail:
                if "cursor" in payload:
                    raise WecomCliError(command, 1, '{"error":{"code":"893101","message":"secret text"}}', "")
                return {"original_data": "first page", "has_more": True, "next_cursor": "missing"}
            if payload["meeting_id"] == "empty":
                return {"original_data": "  \n"}
            return {"original_data": "complete"}

        result = meeting_collector.collect(self.config, now=NOW, runner=runner)
        self.assertEqual((result["status"], result["saved"], result["failed"], result["waiting"]), ("failed", 1, 1, 1))
        self.assertEqual(result["errors"], [{"stage": "transcript", "type": "WecomCliError", "code": 893101}])
        self.assertEqual(len(list(self.archive.glob("*.json"))), 1)
        fail = False
        result = meeting_collector.collect(self.config, now=NOW, runner=runner)
        self.assertEqual((result["saved"], result["skipped"], result["waiting"]), (1, 1, 1))

    def test_busy_collector_does_not_query_api(self):
        with closing(sqlite3.connect(self.root / ".collector.lock")) as database:
            database.execute("BEGIN IMMEDIATE")
            result = meeting_collector.collect(self.config, now=NOW, runner=lambda *_: self.fail("API called"))
        self.assertEqual(result["status"], "busy")


if __name__ == "__main__":
    unittest.main()
