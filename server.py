"""WeCom MCP Server —— 可选薄门面。逻辑在 decrypt/<platform>/wecom_local.py(命令行核心),这里只把工具调用转发过去。

entry 范式与 wechat 对齐:命令行 wecom_local.py 为唯一核心(通用/可测/CI/分发);MCP 是可选层,每工具内部转发。
不要 MCP 也行——直接 `$PY decrypt/macos/wecom_local.py <子命令> [--json]`。
前提:先跑 read_wecom.py 解密(macOS)/ extract+decrypt(Windows)。
"""
import contextlib
import importlib
import io
import json
import os
import platform
import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

from decrypt.local_query import validate_window
from online import docs as online_docs
from online import local_docs as online_local_docs
from online import sheets as online_sheets
from online import smartsheets as online_smartsheets
from online.wecom_cli import WecomCliCompatibilityError, WecomCliError

_HERE = os.path.dirname(os.path.abspath(__file__))
_SYSTEM = platform.system()
_PLAT = "windows" if _SYSTEM == "Windows" else ("macos" if _SYSTEM == "Darwin" else None)


def _local_module():
    if _PLAT is None:
        raise SystemExit(f"本地企微读取暂不支持 {_SYSTEM}；在线工具仍可使用。")
    module_dir = os.path.join(_HERE, "decrypt", _PLAT)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    name = "wecom_win" if _PLAT == "windows" else "wecom_local"
    return importlib.import_module(name)


def _win_key():
    p = os.path.join(_HERE, "decrypt", "windows", "key.txt")
    if not os.path.exists(p):
        raise SystemExit("缺 key.txt(Windows),先跑 extract_raw_key + decrypt")
    return bytes.fromhex(open(p).read().strip())


def _call(cmd, args):
    buf = io.StringIO()
    try:
        local = _local_module()
        with contextlib.redirect_stdout(buf):
            if _PLAT == "windows":
                result = local.CMDS[cmd](_win_key(), args, js=True)
            else:
                result = local.CMDS[cmd](args, js=True)
            if result is not None:
                return _render(result)
    except ValueError as e:
        return _render({"error": str(e)})
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return "解密 OK,但核心表缺失 → 数据未同步(可能新登录)。先打开企业微信浏览消息,等同步后重试。"
        raise
    except SystemExit as e:
        return (buf.getvalue() + f"\n{e}").strip()
    return buf.getvalue().strip() or "(无输出)"


def _json(text, default):
    if not text:
        return default
    return json.loads(text)


def _target(docid, url):
    return {"docid": docid or None, "url": url or None}


def _render(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _online(fn, *args, **kwargs):
    try:
        return _render(fn(*args, **kwargs))
    except (
        PermissionError,
        ValueError,
        json.JSONDecodeError,
        WecomCliError,
        WecomCliCompatibilityError,
    ) as e:
        return str(e)


mcp = FastMCP(
    "wecom",
    instructions=(
        "读取本地企业微信快照，可查询聊天、通讯录、待办和日程，并定位或导出缓存文件。"
        "在线文档、智能主页、在线表格和智能表格通过官方 wecom-cli 联网。线上写入需要用户授权。"
    ),
)


@mcp.tool()
def wecom_contacts(keyword: str = "") -> str:
    """通讯录(姓名/部门/职位/手机/邮箱),keyword 可按词过滤。"""
    return _call("contacts", [keyword] if keyword else [])


@mcp.tool()
def wecom_conversations(limit: int = 100, offset: int = 0) -> str:
    """分页会话列表，含稳定会话 ID、total/next_offset。limit=1..2000。"""
    try:
        validate_window(limit, offset)
    except ValueError as e:
        return _render({"error": str(e)})
    return _call("conversations", ["--limit", str(limit), "--offset", str(offset)])


@mcp.tool()
def wecom_members(conversation: str) -> str:
    """某会话的参与者。优先使用会话 ID；重名会返回候选，不合并群成员。"""
    return _call("members", [conversation])


@mcp.tool()
def wecom_search(keyword: str, days: int | None = None, limit: int = 50, offset: int = 0) -> str:
    """分页全文搜索，保留完整正文和换行。days 为正整数，不传则搜索全部日期。"""
    try:
        validate_window(limit, offset, days)
        if not keyword.strip():
            raise ValueError("keyword 不能为空")
    except ValueError as e:
        return _render({"error": str(e)})
    args = ["--limit", str(limit), "--offset", str(offset)]
    if days is not None:
        args += ["--days", str(days)]
    return _call("search", args + ["--", keyword])


@mcp.tool()
def wecom_read_chat(conversation: str, days: int | None = None, limit: int = 50, offset: int = 0) -> str:
    """按会话 ID 或唯一名称读取完整消息，最新在前。重名时返回候选。"""
    try:
        validate_window(limit, offset, days)
        if not conversation.strip():
            raise ValueError("conversation 不能为空")
    except ValueError as e:
        return _render({"error": str(e)})
    args = ["--limit", str(limit), "--offset", str(offset)]
    if days is not None:
        args += ["--days", str(days)]
    return _call("chat", args + ["--", conversation])


@mcp.tool()
def wecom_export_chat(conversation: str, out: str, days: int | None = None, with_media: bool = False) -> str:
    """按会话导出完整 TXT/Markdown/HTML/JSON/CSV。with_media 生成逐消息清单和独立媒体目录。"""
    try:
        validate_window(days=days)
        if not conversation.strip() or not out.strip():
            raise ValueError("conversation 和 out 不能为空")
    except ValueError as e:
        return _render({"error": str(e)})
    args = ["--out=" + out]
    if days is not None:
        args += ["--days", str(days)]
    if with_media:
        args += ["--with-media"]
    return _call("export", args + ["--", conversation])


@mcp.tool()
def wecom_stats() -> str:
    """统计:总量/发言排行/会话排行/类型/按小时/按天。"""
    return _call("stats", [])


@mcp.tool()
def wecom_todo() -> str:
    """待办(内容/状态/创建者/提醒)。"""
    return _call("todo", [])


@mcp.tool()
def wecom_calendar() -> str:
    """日程(开始/结束/标题)。"""
    return _call("calendar", [])


@mcp.tool()
def wecom_media() -> str:
    """导出本地缓存，保留目录和原名，避免同名覆盖；按会话导出请用 wecom_export_chat。"""
    return _call("media", [])


@mcp.tool()
def wecom_openfile(keyword: str) -> str:
    """聊天里找文档→定位本体→文本解析(txt/csv/md/xlsx/docx/文本PDF)。图片型/扫描PDF 返回 🖼️VISUAL 路径,用 Read 多模态看。"""
    return _call("openfile", ["--", keyword])


@mcp.tool()
def wecom_local_doc_read_path(path: str, limit: int = 8000) -> str:
    """本地缓存/已下载文档 fallback:按路径读取文件。结果可能不是线上最新版。"""
    return _online(online_local_docs.read_path, path, limit=limit)


@mcp.tool()
def wecom_local_doc_search(keyword: str, roots_json: str = "", max_results: int = 5, limit: int = 8000) -> str:
    """本地缓存/已下载文档 fallback:按文件名关键词搜索并读取。结果可能不是线上最新版。"""
    roots = _json(roots_json, None) if roots_json else None
    return _online(online_local_docs.search, keyword, roots=roots, max_results=max_results, limit=limit)


@mcp.tool()
def wecom_doc_create(title: str, confirmed: bool = False) -> str:
    """在线普通文档:创建文档。写操作,confirmed=True 后执行。"""
    return _online(online_docs.create_document, title, confirmed=confirmed)


@mcp.tool()
def wecom_doc_write_markdown(content: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """在线普通文档:用 Markdown 覆写正文。写操作,confirmed=True 后执行。"""
    return _online(online_docs.write_document_markdown, content=content, confirmed=confirmed, **_target(docid, url))


@mcp.tool()
def wecom_doc_read(docid: str = "", url: str = "", content_type: str = "markdown") -> str:
    """在线普通文档:读取最新版正文。需要 wecom-cli 1.1+。"""
    return _online(online_docs.read_document, content_type=content_type, **_target(docid, url))


@mcp.tool()
def wecom_smartpage_create(title: str, pages_json: str = "[]", confirmed: bool = False, auto_file: bool = False) -> str:
    """在线智能文档:创建智能主页。新版 CLI 的 pages_json 必须为空。写操作需确认。"""
    return _online(
        online_docs.create_smartpage,
        title,
        _json(pages_json, []),
        confirmed=confirmed,
        auto_file=auto_file,
    )


@mcp.tool()
def wecom_smartpage_import(title: str, file_path: str, confirmed: bool = False) -> str:
    """在线智能文档:从本地 Markdown 文件导入智能主页。需要新版 CLI，写操作需确认。"""
    return _online(
        online_docs.import_smartpage_markdown,
        title,
        file_path,
        confirmed=confirmed,
    )


@mcp.tool()
def wecom_sheet_create(title: str, confirmed: bool = False) -> str:
    """在线表格:创建表格。写操作,confirmed=True 后执行。"""
    return _online(online_sheets.create_sheet, title, confirmed=confirmed)


@mcp.tool()
def wecom_sheet_info(docid: str = "", url: str = "") -> str:
    """在线表格:读取表格基本信息和子表结构。"""
    return _online(online_sheets.get_info, **_target(docid, url))


@mcp.tool()
def wecom_sheet_get_range(
    sheet_id: str,
    cell_range: str,
    docid: str = "",
    url: str = "",
    mode: str = "default",
) -> str:
    """在线表格:读取单元格范围。需要 wecom-cli 1.1+。"""
    return _online(
        online_sheets.get_range,
        sheet_id=sheet_id,
        cell_range=cell_range,
        mode=mode,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_sheet_add_sub(
    title: str,
    docid: str = "",
    url: str = "",
    row_count: int = 100,
    column_count: int = 20,
    index: int = 0,
    confirmed: bool = False,
) -> str:
    """在线表格:新增子表。写操作,confirmed=True 后执行。"""
    return _online(
        online_sheets.add_subsheet,
        title=title,
        row_count=row_count,
        column_count=column_count,
        index=index,
        confirmed=confirmed,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_sheet_delete_sub(sheet_id: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """在线表格:删除子表。写操作,confirmed=True 后执行。"""
    return _online(online_sheets.delete_subsheet, sheet_id=sheet_id, confirmed=confirmed, **_target(docid, url))


@mcp.tool()
def wecom_sheet_append_row(sheet_id: str, values_json: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """在线表格:追加一行。values_json 为单元格数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_sheets.append_row,
        sheet_id=sheet_id,
        values=_json(values_json, []),
        confirmed=confirmed,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_sheet_update_range(
    sheet_id: str,
    rows_json: str,
    docid: str = "",
    url: str = "",
    start_row: int = 0,
    start_column: int = 0,
    confirmed: bool = False,
) -> str:
    """在线表格:更新指定区域。rows_json 为二维数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_sheets.update_range,
        sheet_id=sheet_id,
        start_row=start_row,
        start_column=start_column,
        rows=_json(rows_json, []),
        confirmed=confirmed,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_create(title: str, confirmed: bool = False) -> str:
    """智能表格:创建智能表格。写操作,confirmed=True 后执行。"""
    return _online(online_smartsheets.create_smartsheet, title, confirmed=confirmed)


@mcp.tool()
def wecom_smartsheet_sheets(docid: str = "", url: str = "") -> str:
    """智能表格:读取子表列表。"""
    return _online(online_smartsheets.get_sheets, **_target(docid, url))


@mcp.tool()
def wecom_smartsheet_add_sheet(docid: str = "", url: str = "", title: str = "", confirmed: bool = False) -> str:
    """智能表格:新增子表。写操作,confirmed=True 后执行。"""
    return _online(online_smartsheets.add_sheet, title=title or None, confirmed=confirmed, **_target(docid, url))


@mcp.tool()
def wecom_smartsheet_update_sheet(sheet_id: str, title: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """智能表格:重命名子表。写操作,confirmed=True 后执行。"""
    return _online(online_smartsheets.update_sheet, sheet_id=sheet_id, title=title, confirmed=confirmed, **_target(docid, url))


@mcp.tool()
def wecom_smartsheet_delete_sheet(sheet_id: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """智能表格:删除子表。写操作,confirmed=True 后执行。"""
    return _online(online_smartsheets.delete_sheet, sheet_id=sheet_id, confirmed=confirmed, **_target(docid, url))


@mcp.tool()
def wecom_smartsheet_fields(sheet_id: str, docid: str = "", url: str = "") -> str:
    """智能表格:读取字段列表。"""
    return _online(online_smartsheets.get_fields, sheet_id=sheet_id, **_target(docid, url))


@mcp.tool()
def wecom_smartsheet_records(
    sheet_id: str,
    docid: str = "",
    url: str = "",
    limit: int = 100,
    cursor: str = "",
    start: int = 0,
    key_type: str = "",
) -> str:
    """智能表格:读取记录。需要新版 CLI；支持游标或起始偏移分页。"""
    return _online(
        online_smartsheets.get_records,
        sheet_id=sheet_id,
        limit=limit,
        cursor=cursor or None,
        start=None if cursor else start,
        key_type=key_type or None,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_add_fields(sheet_id: str, fields_json: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """智能表格:添加字段。fields_json 为字段数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_smartsheets.add_fields,
        sheet_id=sheet_id,
        fields=_json(fields_json, []),
        confirmed=confirmed,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_update_fields(sheet_id: str, fields_json: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """智能表格:更新字段名。fields_json 为字段数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_smartsheets.update_fields,
        sheet_id=sheet_id,
        fields=_json(fields_json, []),
        confirmed=confirmed,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_delete_fields(sheet_id: str, field_ids_json: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """智能表格:删除字段。field_ids_json 为字段 ID 数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_smartsheets.delete_fields,
        sheet_id=sheet_id,
        field_ids=_json(field_ids_json, []),
        confirmed=confirmed,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_add_records(
    sheet_id: str,
    records_json: str,
    docid: str = "",
    url: str = "",
    confirmed: bool = False,
    auto_file: bool = False,
) -> str:
    """智能表格:添加记录。records_json 为记录数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_smartsheets.add_records,
        sheet_id=sheet_id,
        records=_json(records_json, []),
        confirmed=confirmed,
        auto_file=auto_file,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_update_records(
    sheet_id: str,
    records_json: str,
    docid: str = "",
    url: str = "",
    key_type: str = "",
    confirmed: bool = False,
    auto_file: bool = False,
) -> str:
    """智能表格:按 record_id 更新记录。records_json 为记录数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_smartsheets.update_records,
        sheet_id=sheet_id,
        records=_json(records_json, []),
        key_type=key_type or None,
        confirmed=confirmed,
        auto_file=auto_file,
        **_target(docid, url),
    )


@mcp.tool()
def wecom_smartsheet_delete_records(sheet_id: str, record_ids_json: str, docid: str = "", url: str = "", confirmed: bool = False) -> str:
    """智能表格:删除记录。record_ids_json 为记录 ID 数组。写操作,confirmed=True 后执行。"""
    return _online(
        online_smartsheets.delete_records,
        sheet_id=sheet_id,
        record_ids=_json(record_ids_json, []),
        confirmed=confirmed,
        **_target(docid, url),
    )


if __name__ == "__main__":
    mcp.run()
