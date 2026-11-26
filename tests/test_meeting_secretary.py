import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from online import group_webhook, meeting_secretary, meetings

NOW = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake-test-key"


class MeetingSecretaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = Path(temporary.name) / "project.json"
        config.write_text(json.dumps({
            "subjects": ["LQA 周会"],
            "knowledge_sources": ["lqa-project-guide.md"],
            "webhook_env": "TEST_MEETING_WEBHOOK",
        }), encoding="utf-8")
        self.project = meeting_secretary.Project.load(config)
        self.occurrence = {
            "meeting_id": "mtLQA",
            "sub_meeting_id": "occurrence-1",
            "is_repeat_meeting": True,
            "subject": "LQA 周会",
            "begin_time": "2026-11-25 16:00:00",
            "meeting_status": "end",
        }
        self.original = [
            "1 00:00:20 张三: 我负责抽正常截图，周五提交。",
            "2 00:01:45 李四: 正常截图的误报率还不知道。",
        ]
        self.calls = []

    def runner(self, command, payload):
        self.calls.append((command, dict(payload)))
        if command == ["meeting", "list"]:
            other = dict(self.occurrence, subject="其他项目周会")
            if payload.get("cursor") != "last":
                return {"created_meetings": [other], "has_more": True, "next_cursor": "last"}
            return {"attended_meetings": [self.occurrence], "has_more": False}
        occurrence = {
            "meeting_id": self.occurrence["meeting_id"],
            "sub_meeting_id": self.occurrence["sub_meeting_id"],
        }
        if command == ["meeting", "get"]:
            self.assertEqual(payload, {"meeting_ids": [occurrence]})
            return {"meetings": [self.occurrence]}
        self.assertEqual(command, ["meeting", "original", "get"])
        for field, value in occurrence.items():
            self.assertEqual(payload[field], value)
        if self.original and not payload.get("cursor"):
            return {"original_data": self.original[0], "has_more": True, "next_cursor": "page-2"}
        return {"original_data": "\n".join(self.original[1:]), "has_more": False}

    def next(self):
        return meeting_secretary.next_meeting(self.project, now=NOW, runner=self.runner)

    def test_latest_waits_for_the_newest_ended_meeting(self):
        latest = meetings.Occurrence(
            "mtLater", "occurrence-2", self.occurrence["subject"], "2026-11-25 16:30:00",
        )
        earlier = meetings.Occurrence.from_list(self.occurrence)
        with (
            patch.object(meetings, "list_occurrences", return_value=[earlier, latest]) as listed,
            patch.object(meetings, "get_occurrence", return_value=self.occurrence),
            patch.object(meetings, "get_transcript", return_value=[]) as original,
        ):
            result = meeting_secretary.next_meeting(self.project, latest=True, on_date=date(2026, 11, 25))
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["meetings"][0]["reason"], "transcript_not_ready")
        original.assert_called_once()
        self.assertEqual(original.call_args.args[0].key, latest.key)
        self.assertEqual(listed.call_args.args[:2], ("2026-11-25 00:00:00", "2026-11-25 23:59:59"))

    def test_named_group_local_setup_and_repeat_request(self):
        path = self.project.database.with_name("named.json")
        result = meeting_secretary.setup_project(path, "LQA 项目群", ["LQA 周会"], WEBHOOK)
        self.assertEqual(result["group"], "LQA 项目群")
        self.assertNotIn(WEBHOOK, path.read_text(encoding="utf-8"))
        project = meeting_secretary.Project.load(path)
        ready = meeting_secretary.next_meeting(project, now=NOW, runner=self.runner, latest=True)
        report = self.report(ready)
        report["open_issues"][0]["sources"] = []
        self.assertEqual(ready["group"], "LQA 项目群")
        with patch.object(group_webhook, "send_message") as sender:
            with self.assertRaisesRegex(ValueError, "requested group"):
                meeting_secretary.publish(project, report, confirmed=True, group="其他群")
            self.assertEqual(
                meeting_secretary.publish(project, report, confirmed=True, group="LQA 项目群")["status"], "sent",
            )
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], WEBHOOK)
        self.assertEqual(
            meeting_secretary.next_meeting(project, now=NOW, runner=self.runner, latest=True)["status"],
            "already_processed",
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            meeting_secretary.setup_project(path, "其他群", ["LQA 周会"], WEBHOOK)

    def report(self, ready):
        return {
            "meeting_key": ready["meeting"]["key"],
            "transcript_sha256": ready["meeting"]["transcript_sha256"],
            "actions": [{"task": "抽正常截图", "owner": "张三", "due": "周五", "evidence": [1]}],
            "open_issues": [{
                "issue": "误报率未知",
                "context": "历史批次只统计过 20 张图",
                "proposal": "把正常截图加入盲测",
                "first_step": "标出人工确认的正常截图",
                "evidence": [2],
                "sources": [1],
            }],
        }

    def test_reads_all_pages_and_publishes_the_report_once(self):
        ready = self.next()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual([turn["text"] for turn in ready["transcript"]], self.original)
        self.assertEqual(self.calls[0][1], {
            "begin_time": "2026-11-24 17:00:00", "end_time": "2026-11-25 17:00:00",
        })
        report = self.report(ready)
        markdown = meeting_secretary.preview(self.project, report)
        self.assertIn("• **张三** 抽正常截图（截止 周五）", markdown)
        self.assertIn("\n2026-11-25\n", markdown)
        self.assertNotIn("16:00:00", markdown)
        self.assertIn("**接下来谁做什么**", markdown)
        self.assertIn("**待拍板**", markdown)
        self.assertIn("**误报率未知**（历史批次只统计过 20 张图）。建议", markdown)
        self.assertNotIn("转写第", markdown)
        self.assertNotIn("lqa-project-guide.md", markdown)
        self.assertFalse(any(line.startswith(" ") for line in markdown.splitlines()))
        detail = meeting_secretary.preview(self.project, report, detail=True)
        self.assertIn("转写第 2 行（00:01:45 李四）", detail)
        self.assertIn("项目资料 lqa-project-guide.md", detail)
        payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
        with (
            patch.dict("os.environ", {self.project.webhook_env: WEBHOOK}),
            patch.object(group_webhook, "send_message") as send,
        ):
            self.assertEqual(meeting_secretary.publish(self.project, report, confirmed=True)["status"], "sent")
            replay = meeting_secretary.publish(self.project, report, confirmed=True)
            self.assertEqual(replay["status"], "already_processed")
            send.assert_called_once_with(WEBHOOK, payload)
        self.assertEqual(self.next()["status"], "idle")

    def test_preview_preserves_live_transcript_datetime_and_speaker(self):
        self.original[0] = "0 2026-11-25 16:00:20 张三: 我负责抽正常截图，周五提交。"
        report = self.report(self.next())
        markdown = meeting_secretary.preview(self.project, report, detail=True)
        self.assertIn("转写第 1 行（2026-11-25 16:00:20 张三）", markdown)

    def test_publishes_confirmed_decisions_with_context_and_safe_markdown(self):
        report = dict(self.report(self.next()), topic="AI 试译与 LQE", actions=[], open_issues=[], decisions=[{
            "decision": "使用 **20 张**截图\n<@all>",
            "context": "先验证误报率，再决定是否扩大范围",
            "priority": "高优先级",
            "evidence": [1],
        }])
        markdown = meeting_secretary.preview(self.project, report)
        self.assertTrue(markdown.startswith("## AI 试译与 LQE 会后纪要\n2026-11-25\n"))
        self.assertNotIn("LQA 周会", markdown)
        self.assertIn("原会议 LQA 周会\n2026-11-25 16:00:00",
                      meeting_secretary.preview(self.project, report, detail=True))
        self.assertIn("**会上定下的事**", markdown)
        self.assertIn("【高优先级】", markdown)
        self.assertIn("（先验证误报率，再决定是否扩大范围）", markdown)
        self.assertIn(r"\*\*20 张\*\*", markdown)
        self.assertNotIn("<@all>", markdown)
        self.assertNotIn("接下来谁做什么", markdown)
        with (
            patch.dict("os.environ", {self.project.webhook_env: WEBHOOK}),
            patch.object(group_webhook, "send_message") as send,
        ):
            self.assertEqual(meeting_secretary.publish(self.project, report, confirmed=True)["status"], "sent")
            send.assert_called_once_with(WEBHOOK, {"msgtype": "markdown", "markdown": {"content": markdown}})

    def test_waits_for_end_and_then_transcript(self):
        self.occurrence["meeting_status"] = "started"
        waiting = self.next()
        self.assertEqual(waiting["meetings"][0]["reason"], "meeting_not_ended")
        self.assertNotIn(["meeting", "original", "get"], [command for command, _ in self.calls])
        self.occurrence["meeting_status"] = "end"
        self.original = []
        waiting = self.next()
        self.assertEqual(waiting["meetings"][0]["reason"], "transcript_not_ready")
        self.original = ["1 00:00:20 张三: 测试开始。"]
        self.assertEqual(self.next()["status"], "ready")

    def test_recurring_occurrences_are_tracked_separately(self):
        first = self.next()
        report = dict(self.report(first), actions=[], open_issues=[])
        self.assertEqual(meeting_secretary.publish(self.project, report, confirmed=True)["status"], "empty")
        self.occurrence["sub_meeting_id"] = "occurrence-2"
        self.occurrence["begin_time"] = "2026-11-25 16:30:00"
        second = self.next()
        self.assertEqual(second["status"], "ready")
        self.assertNotEqual(first["meeting"]["key"], second["meeting"]["key"])

    def test_validates_evidence_and_rejects_an_outdated_report(self):
        report = self.report(self.next())
        report["actions"][0]["owner"] = None
        report["actions"][0]["due"] = None
        markdown = meeting_secretary.preview(self.project, report)
        self.assertIn("抽正常截图", markdown)
        for placeholder in ("负责人", "截止", "待确认"):
            self.assertNotIn(placeholder, markdown)
        report["actions"][0]["evidence"] = [99]
        with self.assertRaisesRegex(ValueError, "unknown reference"):
            meeting_secretary.preview(self.project, report)
        self.original[0] = "1 00:00:20 张三: 截止时间改为周四。"
        self.next()
        with self.assertRaisesRegex(ValueError, "older or different transcript"):
            meeting_secretary.publish(self.project, report, confirmed=True)

    def test_explicit_rejection_can_retry_but_unconfirmed_send_is_held(self):
        report = self.report(self.next())
        key = report["meeting_key"]
        with patch.dict("os.environ", {self.project.webhook_env: WEBHOOK}):
            with (
                patch.object(group_webhook, "send_message", side_effect=group_webhook.WebhookRejected),
                self.assertRaises(group_webhook.WebhookRejected),
            ):
                meeting_secretary.publish(self.project, report, confirmed=True)
            self.assertEqual(self.next()["status"], "ready")
            with (
                patch.object(group_webhook, "send_message", side_effect=group_webhook.WebhookUncertain),
                self.assertRaises(group_webhook.WebhookUncertain),
            ):
                meeting_secretary.publish(self.project, report, confirmed=True)
            with self.assertRaisesRegex(ValueError, "previous send"):
                meeting_secretary.publish(self.project, report, confirmed=True)
            self.assertEqual(self.next()["attention"][0]["key"], key)
            meeting_secretary.resolve_delivery(self.project, key, delivered=False)
            with patch.object(group_webhook, "send_message") as send:
                self.assertEqual(meeting_secretary.publish(self.project, report, confirmed=True)["status"], "sent")
                send.assert_called_once()

    def test_parallel_workers_cannot_send_the_same_report_twice(self):
        report = self.report(self.next())
        sending = threading.Event()
        finish = threading.Event()

        def hold_send(*_):
            sending.set()
            if not finish.wait(5):
                raise RuntimeError("Timed out waiting for second worker.")

        with (
            patch.dict("os.environ", {self.project.webhook_env: WEBHOOK}),
            patch.object(group_webhook, "send_message", side_effect=hold_send) as send,
            ThreadPoolExecutor(max_workers=1) as worker,
        ):
            first = worker.submit(meeting_secretary.publish, self.project, report, confirmed=True)
            try:
                self.assertTrue(sending.wait(5))
                with self.assertRaisesRegex(ValueError, "previous send"):
                    meeting_secretary.publish(self.project, report, confirmed=True)
            finally:
                finish.set()
            self.assertEqual(first.result()["status"], "sent")
            send.assert_called_once()

    def test_send_needs_authorization_and_a_configured_group_webhook(self):
        report = self.report(self.next())
        with self.assertRaises(PermissionError):
            meeting_secretary.publish(self.project, report)
        with (
            patch.dict("os.environ", {self.project.webhook_env: "https://example.com/webhook"}),
            self.assertRaisesRegex(ValueError, "WeCom group webhook URL"),
        ):
            meeting_secretary.publish(self.project, report, confirmed=True)
        self.assertEqual(self.next()["status"], "ready")


class MeetingApiTests(unittest.TestCase):
    def test_missing_recurring_occurrence_id_is_rejected(self):
        with self.assertRaisesRegex(meetings.MeetingError, "no occurrence ID"):
            meetings.Occurrence.from_list({"meeting_id": "mtLQA", "is_repeat_meeting": True})

    def test_repeated_transcript_cursor_is_rejected(self):
        occurrence = meetings.Occurrence("mtLQA", "", "LQA 周会", "2026-11-25 16:00:00")

        def runner(command, payload):
            return {"original_data": "原文", "has_more": True, "next_cursor": "repeat"}

        with self.assertRaisesRegex(meetings.MeetingError, "repeated pagination cursor"):
            meetings.get_transcript(occurrence, runner)

    def test_long_report_is_uploaded_and_sent_as_one_file(self):
        report = "## 会后行动\n" + "较长的行动清单\n" * 500
        with patch.object(group_webhook, "urlopen", side_effect=[
            BytesIO(b'{"errcode":0,"media_id":"media-1"}'),
            BytesIO(b'{"errcode":0}'),
        ]) as request:
            message = group_webhook.prepare_message(WEBHOOK, report)
            group_webhook.send_message(WEBHOOK, message)
        upload = request.call_args_list[0].args[0]
        send = request.call_args_list[1].args[0]
        self.assertIn("/cgi-bin/webhook/upload_media?", upload.full_url)
        self.assertIn(report.encode("utf-8"), upload.data)
        self.assertEqual(send.full_url, WEBHOOK)
        self.assertEqual(json.loads(send.data), {"msgtype": "file", "file": {"media_id": "media-1"}})


if __name__ == "__main__":
    unittest.main()
