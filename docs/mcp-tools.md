---
title: MCP Tools
description: Local read and export tools and confirmation-gated online tools exposed by server.py.
---

# MCP Tools

`server.py` exposes the repository's local and online operations through a thin MCP facade. Local tools read the local snapshot or cache. Online document and table tools invoke the official CLI over the network.

## Local read tools

| Tool | Purpose |
|---|---|
| `wecom_contacts` | Search local contact fields by optional keyword |
| `wecom_conversations` | List local conversations with message counts and latest timestamps |
| `wecom_members` | List participants for a matched conversation |
| `wecom_search` | Full-text search across locally exported messages |
| `wecom_read_chat` | Read full message bodies from a uniquely selected conversation |
| `wecom_export_chat` | Export a local transcript with optional per-message media manifest |
| `wecom_stats` | Summarize local message, sender, conversation, type, and time distributions |
| `wecom_todo` | Read locally available todo records |
| `wecom_calendar` | Read locally available calendar records |
| `wecom_media` | Export readable cached media and files |
| `wecom_openfile` | Locate and parse a file referenced by local chat data |

Conversation and message queries return structured JSON on both macOS and Windows. `wecom_conversations(limit=100, offset=0)`, `wecom_search(keyword, days=None, limit=50, offset=0)` and `wecom_read_chat(conversation, days=None, limit=50, offset=0)` include totals and `next_offset`. Limits must be integers from 1 to 2000; offsets are nonnegative; optional days are positive. Message bodies are complete. Use the returned conversation ID to resolve duplicate names.

`wecom_export_chat(conversation, out, days=None, with_media=False)` infers TXT/Markdown/HTML/JSON/CSV from the output extension. It writes only local files. With media enabled, it returns the sidecar path, unique asset directory and counts by resolution status. See [local reading](/local-reading) for the resource schema and supported media profiles.

## Local document fallback

| Tool | Purpose |
|---|---|
| `wecom_local_doc_read_path` | Parse one local path and report content or a visual marker |
| `wecom_local_doc_search` | Search configured local roots by filename and parse the newest matches |

Fallback results may be stale compared with the online document.

## Normal documents and sheets

| Tool | Operation | Confirmation |
|---|---|---|
| `wecom_doc_create` | Create a normal document | Required |
| `wecom_doc_read` | Read the latest normal-document body on CLI `1.1+` | Not required |
| `wecom_doc_write_markdown` | Replace normal document content with Markdown | Required |
| `wecom_smartpage_create` | Create a smart page; current CLI requires empty `pages_json` | Required |
| `wecom_smartpage_import` | Import a local Markdown file as a smart page on CLI `1.1+` | Required |
| `wecom_sheet_create` | Create an online sheet | Required |
| `wecom_sheet_info` | Read sheet structure | Not required |
| `wecom_sheet_get_range` | Read a cell range in `default` mode on CLI `1.1+`. The wrapper omits the range in `csv` mode. | Not required |
| `wecom_sheet_add_sub` | Add a subsheet | Required |
| `wecom_sheet_delete_sub` | Delete a subsheet | Required |
| `wecom_sheet_append_row` | Append one row | Required |
| `wecom_sheet_update_range` | Update a rectangular cell range using zero-based row and column coordinates | Required |

## Smart sheets

These MCP tools route to the legacy flat commands or the current nested `smartsheet` service according to the installed CLI generation.

| Tool | Operation | Confirmation |
|---|---|---|
| `wecom_smartsheet_create` | Create a smart-sheet document | Required |
| `wecom_smartsheet_sheets` | Read child sheets | Not required |
| `wecom_smartsheet_add_sheet` | Add a child sheet | Required |
| `wecom_smartsheet_update_sheet` | Rename a child sheet | Required |
| `wecom_smartsheet_delete_sheet` | Delete a child sheet | Required |
| `wecom_smartsheet_fields` | Read fields | Not required |
| `wecom_smartsheet_records` | Read records on CLI `1.1+` with cursor or offset pagination | Not required |
| `wecom_smartsheet_add_fields` | Add fields | Required |
| `wecom_smartsheet_update_fields` | Update fields | Required |
| `wecom_smartsheet_delete_fields` | Delete fields | Required |
| `wecom_smartsheet_add_records` | Add records | Required |
| `wecom_smartsheet_update_records` | Update known records by `record_id` | Required |
| `wecom_smartsheet_delete_records` | Delete known records | Required |

## Confirmation behavior

Every online write tool accepts `confirmed`. The wrapper blocks the write before invoking the CLI unless `confirmed` is true. Verify that the user's authorization covers the exact target, content, and scope. Reuse explicit authorization for that write and verify the affected data afterward.

## Run the server

```bash
python3 server.py
```

If the Codex session already exposes `wecom` tools, call them directly. For a new client connection, configure the stdio server with the dependency environment's Python and the absolute path to `server.py`. On macOS, `bash setup.sh` prepares an isolated `.venv` and can register it with Claude Code. Direct module calls are available for testing and automation.

## JSON inputs

Complex MCP inputs such as sheet rows, pages, fields, records, and record IDs are accepted as JSON strings. Invalid JSON is rejected before the online call. Review the corresponding Python function in the [API reference](/api-reference) for the underlying payload shape.
