"""Meeting occurrences and original transcripts from the official WeCom CLI."""

from dataclasses import dataclass
from hashlib import sha256

from .wecom_cli import call_json


class MeetingError(RuntimeError):
    """WeCom returned an error or an unexpected meeting response."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Occurrence:
    meeting_id: str
    sub_meeting_id: str
    subject: str
    begin_time: str
    end_time: str | None = None

    @classmethod
    def from_list(cls, item):
        meeting_id = item.get("meeting_id")
        sub_id = item.get("sub_meeting_id") or ""
        begin_time = item.get("begin_time")
        if not isinstance(meeting_id, str) or not meeting_id or not isinstance(sub_id, str):
            raise MeetingError("Meeting list returned an invalid occurrence ID.")
        if item.get("is_repeat_meeting") and not sub_id:
            raise MeetingError("A recurring meeting has no occurrence ID.")
        if not isinstance(begin_time, str) or not begin_time:
            raise MeetingError("Meeting list returned no start time.")
        end_time = item.get("end_time")
        return cls(meeting_id, sub_id, item["subject"], begin_time, end_time if isinstance(end_time, str) else None)

    @property
    def key(self):
        return sha256(f"{self.meeting_id}\0{self.sub_meeting_id}".encode()).hexdigest()

    @property
    def identity(self):
        payload = {"meeting_id": self.meeting_id}
        if self.sub_meeting_id:
            payload["sub_meeting_id"] = self.sub_meeting_id
        return payload


def _request(command, payload, runner):
    response = call_json(["meeting", *command], payload, runner=runner)
    if not isinstance(response, dict):
        raise MeetingError("Expected a JSON object from wecom-cli meeting.")
    if response.get("errcode", 0) != 0 or response.get("error"):
        detail = response.get("error")
        code = detail.get("code") if isinstance(detail, dict) else None
        raise MeetingError("WeCom meeting API rejected the request.", code or response.get("errcode"))
    data = response.get("data")
    return data if isinstance(data, dict) else response


def _pages(command, payload, runner):
    query = dict(payload)
    seen = set()
    while True:
        page = _request(command, query, runner)
        yield page
        if not page.get("has_more"):
            return
        cursor = page.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise MeetingError("WeCom returned an invalid or repeated pagination cursor.")
        query["cursor"] = cursor
        seen.add(cursor)


def list_occurrences(begin_time, end_time, subjects, runner=None, *, subject_matches=None, on_occurrence_error=None):
    matches = subject_matches or subjects.__contains__
    found = {}
    for page in _pages(["list"], {"begin_time": begin_time, "end_time": end_time}, runner):
        for field in ("created_meetings", "attended_meetings"):
            for item in page.get(field) or []:
                subject = item.get("subject") if isinstance(item, dict) else None
                if not isinstance(subject, str) or not matches(subject):
                    continue
                try:
                    occurrence = Occurrence.from_list(item)
                except MeetingError as error:
                    if on_occurrence_error is None:
                        raise
                    on_occurrence_error(error)
                    continue
                found[occurrence.key] = occurrence
    return sorted(found.values(), key=lambda item: item.begin_time)


def get_occurrence(occurrence, runner=None):
    response = _request(["get"], {"meeting_ids": [occurrence.identity]}, runner)
    for item in response.get("meetings") or []:
        if (
            isinstance(item, dict)
            and item.get("meeting_id") == occurrence.meeting_id
            and (item.get("sub_meeting_id") or "") == occurrence.sub_meeting_id
        ):
            return item
    raise MeetingError("WeCom did not return the requested meeting occurrence.")


def get_transcript(occurrence, runner=None):
    lines = []
    for page in _pages(["original", "get"], {**occurrence.identity, "limit": 500}, runner):
        original = page.get("original_data")
        if original is not None and not isinstance(original, str):
            raise MeetingError("WeCom returned a non-text original transcript.")
        if original:
            lines.extend(original.splitlines())
    return lines if any(line.strip() for line in lines) else []
