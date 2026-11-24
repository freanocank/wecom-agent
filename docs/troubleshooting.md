---
title: Troubleshooting
description: Diagnose missing CLI commands, enterprise permission boundaries, local file fallbacks, and confirmation failures.
---

# Troubleshooting

Start with the narrowest failing layer: repository tests, local WeCom data, WecomTeam authentication, command availability, then enterprise permission.

## Repository tests fail

Run the full standard-library test suite from the repository root:

```bash
python3 -m unittest discover -s tests
```

Install only the optional parser or platform package required by the failing workflow. Core online-wrapper tests inject fake runners and do not need a live WeCom session.

## `wecom-cli` is not found

```bash
npm install -g @wecom/cli
wecom-cli --version
```

Confirm that the npm global binary directory is on `PATH` before running `python3 -m online.selfcheck`.

## Authentication is missing

```bash
wecom-cli auth init
wecom-cli auth show --status
```

Complete the official authentication flow in the same user environment that runs the Python process.

## A WeCom method or document denies access

CLI authorization status reports the credential state. Check both the business method and the specific document. For `851008` (for example, `partial no authorization`), use the response details to identify the missing business authorization, such as document search and content access. If the response says `forbidden for current apikey`, the credential lacks access to the method that returned the error. For document requests returning `851003 no authority`, check the calling account or bot's access to the document and the enterprise's visibility settings. Enterprise-mode bots have their own document permissions. See the [WeCom permission discussion](https://github.com/WecomTeam/wecom-cli/issues/64).

After an explicit permission denial, stop retries under the same authorization. If the user chooses the desktop client, use the signed-in account's existing access. Verify that the available Computer Use tools can interact with the document before writing, then reopen the changed target to verify saved data.

## CLI licensing and service charges

The official CLI software is [MIT-licensed](https://github.com/WecomTeam/wecom-cli/blob/main/LICENSE). The public [CLI documentation](https://github.com/WecomTeam/wecom-cli) and [SmartSheet references](https://github.com/WecomTeam/wecom-cli/tree/main/skills/wecomcli-smartsheet) do not list per-call pricing for ordinary sheet, field, and record operations. Verify applicable charges against the organization's current WeCom plan or agreement. [Optional advanced document features](https://open.work.weixin.qq.com/help2/pc/15422) and [service-provider API access licenses](https://hiflow.tencent.com/document/applications/qiweishoufeishuoming/) have separate terms. A permission error indicates an access issue and does not establish a charge.

## A legacy or current command is rejected

CLI `1.1+` renamed services, command paths, flags, payloads, and error output. Run:

```bash
wecom-cli --version
python3 -m online.selfcheck
```

Do not reuse a legacy flat command or smart-sheet ID payload on a current CLI. See the [migration note](/wecom-cli-migration).

## An online document body cannot be read

Current CLI releases use `online.docs.read_document()`. If the installed CLI is legacy, the enterprise denies the read, or the workflow is offline, search a locally cached or downloaded copy instead:

```python
from online.local_docs import search

result = search("document title")
```

A fallback result may be stale. No local match means only that the file is absent from the searched roots.

## Smart-sheet record reads fail

CLI `1.1+` reads smart-sheet records through `online.smartsheets.get_records()`. CLI `0.1.x` has no corresponding read command and raises a compatibility error. On current releases, check authorization, the document target, `sheet_id`, and pagination values.

Do not perform an update unless the record IDs were obtained from a successful read or another authorized source.

## `PermissionError` requests confirmation

The call is an online write and `confirmed` is false. Review the exact target, content, and impact, then repeat the same intended operation with `confirmed=True`.

Do not set the flag globally or reuse confirmation for a different operation.

## `ValueError: Pass exactly one of docid or url`

Supply one document target:

```python
get_info(docid="DOC_ID")
# or
get_info(url="https://doc.weixin.qq.com/...")
```

Passing both is ambiguous; passing neither leaves no target.

## A local document returns `visual`

The file is an image or a scanned PDF without extractable text. Use an authorized visual inspection or OCR workflow on the returned local path. The reader deliberately does not invent text.

## A local document parser is unavailable

Install the optional package needed by the format:

```bash
python3 -m pip install -r requirements-documents.txt
```

Keep optional dependencies limited to the formats required by the current task.

## No local chat results appear

Confirm that WeCom is running and signed in, then rebuild the authorized local export before querying it. On macOS, use `decrypt/macos/read_wecom.py`; on Windows, use the provided PowerShell coordinator and validation script.

## Share a safe diagnostic

Include the command name, exit code, sanitized error message, platform, Python version, and `wecom-cli` version. Remove keys, tokens, document URLs, IDs, contact data, message content, decrypted database paths, and enterprise payloads.
