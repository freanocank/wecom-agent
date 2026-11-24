---
title: wecom-cli 1.1+ Migration
description: Understand the breaking command-model change and the repository's version-aware compatibility boundary.
---

# `wecom-cli` 1.1+ Migration

The official CLI changed its command and authentication model in `1.1.0`. The original migration was checked with `1.2.0`. For current releases and command references, see the official [changelog](https://github.com/WecomTeam/wecom-cli/blob/main/CHANGELOG.md), [skills index](https://github.com/WecomTeam/wecom-cli/blob/main/docs/skills.md), and [npm package](https://www.npmjs.com/package/@wecom/cli).

## Breaking changes

| Legacy `0.1.x` | Current `1.1+` |
|---|---|
| `wecom-cli init` | `wecom-cli auth init` |
| `wecom-cli auth show --auth-status` | `wecom-cli auth show --status` |
| `doc create_doc` | `doc create` |
| `doc edit_doc_content` | `doc contents overwrite` |
| no online document-body read | `doc contents get` |
| `doc sheet_get_info` | `sheet get` |
| `doc sheet_append_data` | `sheet rows append` |
| `doc sheet_update_range_data` | `sheet contents update` |
| no standard-sheet cell read | `sheet ranges get` |
| flat `doc smartsheet_*` methods | native `smartsheet <resource> <method>` service |
| `msg` / `schedule` services | `message` / `calendar` services |

Payloads also changed. Smart-sheet operations now commonly identify child resources by readable titles rather than the legacy IDs accepted by this repository's old wrappers. Do not translate only the command name and reuse the old payload.

## Repository behavior

The Python layer detects the installed semantic version before choosing a command path.

| Area | Legacy `0.1.x` | Current `1.1+` |
|---|---|---|
| Normal document create/write | Supported | Supported |
| Normal document latest-body read | Unsupported | Supported |
| Standard sheet structure/write | Supported | Supported |
| Standard sheet cell-range read | Unsupported | Supported |
| Smart-page empty create / Markdown import | Legacy create only | Supported |
| Smart-page structured legacy `pages` payload | Supported | Fails closed; cannot map losslessly |
| Smart-sheet child sheets, fields, and records | Supported, except record reads | Supported |

Failing closed prevents an old payload from reaching a newly named write endpoint. `python3 -m online.selfcheck` reports the detected generation, authorization state, available services, and wrapper coverage.

## Check the installed CLI

```bash
wecom-cli --version
wecom-cli auth show --status
python3 -m online.selfcheck
```

Run the Python check from the skill root. Use `auth init` only when the CLI is needed and shows `unauthorized`. For `0.1.x`, use the legacy authorization flag above. Service help and schemas are determined by the installed CLI and server discovery. Authorization status alone does not establish access to a specific document.

To install or upgrade the CLI when requested, use `npm install -g @wecom/cli`, then repeat the checks. Starting with `1.3.0`, CLI file reads and writes are scoped to its working directory and the system temporary directory. Downloads default to the working directory, and `--output-dir` applies to file-producing responses. Validate the upload and download paths used by the wrapper, including smart-page Markdown import, before relying on these operations after an upgrade.

Use `--help`, `--doc`, or `--schema` and local command construction to check compatibility. Use an authorized, narrowly scoped read to check document access.
