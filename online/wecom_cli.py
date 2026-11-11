"""Thin wrapper around WecomTeam wecom-cli."""

import json
import re
import subprocess
from urllib.parse import urlparse

CURRENT_CLI_MIN = (1, 1, 0)
WECOM_DOC_HOSTS = {"doc.weixin.qq.com", "page.weixin.qq.com"}


class WecomCliError(RuntimeError):
    def __init__(self, argv, returncode, stdout, stderr):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = _error_message(stdout, stderr, returncode)
        super().__init__(message)


class WecomCliCompatibilityError(RuntimeError):
    """Raised when a wrapper cannot safely translate between CLI generations."""


def _error_message(stdout, stderr, returncode):
    try:
        body = json.loads(stdout.strip())
    except (json.JSONDecodeError, AttributeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = body["error"]
        detail = error.get("message") or error.get("type")
        if detail:
            code = error.get("code")
            return f"{detail} (code {code})" if code is not None else str(detail)
    return stderr.strip() or stdout.strip() or f"wecom-cli exited with {returncode}"


def run_json(args, payload=None, timeout=60, cli="wecom-cli", run=subprocess.run):
    argv = [cli] + list(args)
    if payload is not None:
        argv.extend(["--json", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))])

    completed = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise WecomCliError(argv, completed.returncode, completed.stdout, completed.stderr)

    output = completed.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def run_text(args, timeout=30, cli="wecom-cli", run=subprocess.run):
    argv = [cli] + list(args)
    completed = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise WecomCliError(argv, completed.returncode, completed.stdout, completed.stderr)
    return completed.stdout.strip()


def require_confirmation(confirmed, action):
    if not confirmed:
        raise PermissionError(f"{action} is an online write operation; pass confirmed=True after user approval.")


def target_payload(docid=None, url=None):
    if bool(docid) == bool(url):
        raise ValueError("Pass exactly one of docid or url.")
    return {"docid": docid} if docid else {"url": url}


def current_target_payload(docid=None, url=None):
    if bool(docid) == bool(url):
        raise ValueError("Pass exactly one of docid or url.")
    return {"docid": docid or docid_from_url(url)}


def docid_from_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in WECOM_DOC_HOSTS:
        raise ValueError("Expected an https://doc.weixin.qq.com or https://page.weixin.qq.com URL.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("WeCom document URL does not contain a document ID.")
    return parts[-1]


def parse_version(value):
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", str(value))
    if not match:
        raise ValueError(f"Cannot parse wecom-cli version from: {value!r}")
    return tuple(int(part) for part in match.groups())


def cli_generation(value):
    return "current" if parse_version(value) >= CURRENT_CLI_MIN else "legacy"


def resolve_generation(cli_version=None, runner=None):
    if cli_version is not None:
        return cli_generation(cli_version)
    if runner is not None:
        return "legacy"
    return cli_generation(version())


def call_json(args, payload=None, runner=None):
    call = runner or run_json
    return call(list(args), payload)


def doc_call(command, payload=None, runner=None):
    return call_json(["doc", command], payload, runner=runner)


def auth_status(runner=None, cli_version=None):
    call = runner or run_text
    generation = resolve_generation(cli_version=cli_version, runner=runner)
    flag = "--status" if generation == "current" else "--auth-status"
    return call(["auth", "show", flag])


def version(runner=None):
    call = runner or run_text
    return call(["--version"])


def doc_command_supported(command, runner=None):
    call = runner or run_text
    try:
        call(["doc", command, "--schema"])
    except (WecomCliError, OSError):
        return False
    return True


def service_available(service, runner=None):
    call = runner or run_text
    try:
        call([service, "--help"])
    except (WecomCliError, OSError):
        return False
    return True


def require_legacy(feature, cli_version=None, runner=None):
    generation = resolve_generation(cli_version=cli_version, runner=runner)
    if generation != "legacy":
        raise WecomCliCompatibilityError(
            f"{feature} still uses the legacy wecom-cli 0.1.x payload. "
            "Use the native wecom-cli 1.1+ service until this wrapper is migrated."
        )
    return generation
