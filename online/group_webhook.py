"""Delivery to a fixed WeCom group webhook."""

import json
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4


class WebhookRejected(RuntimeError):
    """The WeCom API explicitly rejected delivery."""


class WebhookUncertain(RuntimeError):
    """A delivery request did not receive a usable acknowledgement."""


def validate_url(url):
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    if (
        parts.scheme != "https"
        or parts.netloc != "qyapi.weixin.qq.com"
        or parts.path != "/cgi-bin/webhook/send"
        or parts.fragment
        or set(params) != {"key"}
        or len(params["key"]) != 1
    ):
        raise ValueError("Set the configured variable to the WeCom group webhook URL.")


def _post(url, body, content_type):
    request = Request(url, body, {"Content-Type": content_type}, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except (OSError, ValueError):
        # urllib errors may include the webhook URL and its credential.
        raise WebhookUncertain("WeCom did not acknowledge the webhook request.") from None
    if not isinstance(result, dict) or type(result.get("errcode")) is not int:
        raise WebhookUncertain("WeCom returned an unrecognized webhook acknowledgement.")
    if result["errcode"] != 0:
        raise WebhookRejected(f"WeCom rejected the webhook request (code {result['errcode']}).")
    return result


def prepare_message(url, markdown):
    content = markdown.encode("utf-8")
    if len(content) <= 4096:
        return {"msgtype": "markdown", "markdown": {"content": markdown}}

    # The webhook can deliver the complete action list as a single file.
    parts = urlsplit(url)
    key = parse_qs(parts.query)["key"][0]
    upload_url = urlunsplit((
        parts.scheme,
        parts.netloc,
        "/cgi-bin/webhook/upload_media",
        urlencode({"key": key, "type": "file"}),
        "",
    ))
    boundary = uuid4().hex
    header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="media"; filename="meeting-actions.md"\r\n'
        "Content-Type: text/markdown; charset=utf-8\r\n\r\n"
    ).encode()
    body = header + content + f"\r\n--{boundary}--\r\n".encode()
    result = _post(upload_url, body, f"multipart/form-data; boundary={boundary}")
    media_id = result.get("media_id")
    if not isinstance(media_id, str) or not media_id:
        raise WebhookUncertain("WeCom did not return a file ID for the meeting report.")
    return {"msgtype": "file", "file": {"media_id": media_id}}


def send_message(url, payload):
    _post(
        url,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
    )
