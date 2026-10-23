"""Turning Jira issues into rows and writing them to an .xlsx file."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from jira_exporter.config import ResolvedField
from jira_exporter.timing import EXCEL, FINISH, ROWS, SAVE, ExportTimer

# Excel rejects cells longer than this.
MAX_CELL_LEN = 32767


def format_value(value: Any, schema_type: str | None = None) -> Any:
    """Flatten a Jira field value into something an Excel cell can hold."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(format_value(v)) for v in value if v is not None)
    if isinstance(value, dict):
        if value.get("type") == "doc":  # Cloud v3 rich text (Atlassian Document Format)
            # Strip only blank lines so a leading code block keeps its indentation.
            return format_value(adf_to_text(value).strip("\n"))
        for k in ("displayName", "name", "value", "key", "emailAddress"):
            if value.get(k) not in (None, ""):
                text = str(value[k])
                # Cascading select fields carry the child option in "child".
                child = value.get("child")
                if isinstance(child, dict) and child.get("value"):
                    text += f" - {child['value']}"
                return text
        return str(value)
    if isinstance(value, str) and schema_type in ("datetime", "date"):
        parsed = _parse_date(value, schema_type)
        if parsed is not None:
            return parsed
    if isinstance(value, str) and len(value) > MAX_CELL_LEN:
        return value[: MAX_CELL_LEN - 3] + "..."
    return value


_ADF_BLOCKS = {"paragraph", "heading", "blockquote", "codeBlock", "panel", "rule",
               "decisionItem", "mediaSingle"}


def adf_to_text(node: Any) -> str:
    """Flatten an Atlassian Document Format tree into plain text.

    Lists keep their numbering, and nested content is indented under its item.
    Table rows become "a | b | c" lines, and link targets are kept next to their text.
    """
    if isinstance(node, list):
        return _adf_inline_run(node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []
    if kind == "text":
        return _with_href(node.get("text", ""), _link_href(node))
    if kind == "hardBreak":
        return "\n"
    if kind == "date":
        try:
            ts = int(attrs.get("timestamp")) / 1000
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return str(attrs.get("timestamp") or "")
    if kind in ("mention", "emoji", "status", "inlineCard"):
        return str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or "")
    if kind in ("blockCard", "embedCard"):
        return str(attrs.get("url") or "") + "\n"
    if kind in ("bulletList", "orderedList"):
        first = attrs.get("order", 1) if kind == "orderedList" else None
        items = [c for c in content if isinstance(c, dict)]
        return "".join(
            _list_entry(f"{first + i}. " if first is not None else "- ", adf_to_text(item.get("content") or []))
            for i, item in enumerate(items))
    if kind == "taskList":
        out = []
        for child in content:
            if not isinstance(child, dict):
                continue
            if child.get("type") == "taskList":  # an indented (nested) action item list
                out.append(_indent(adf_to_text(child), "  "))
            else:
                box = "[x] " if (child.get("attrs") or {}).get("state") == "DONE" else "[ ] "
                out.append(_list_entry(box, adf_to_text(child.get("content") or [])))
        return "".join(out)
    if kind == "tableRow":
        cells = [" ".join(adf_to_text(c.get("content") or []).split())
                 for c in content if isinstance(c, dict)]
        return " | ".join(cells) + "\n"
    text = adf_to_text(content)
    if kind in _ADF_BLOCKS:
        text = text.rstrip("\n") + "\n"
    return text


def _link_href(node: Any) -> str | None:
    if not isinstance(node, dict) or node.get("type") != "text":
        return None
    for mark in node.get("marks") or []:
        if isinstance(mark, dict) and mark.get("type") == "link":
            return (mark.get("attrs") or {}).get("href")
    return None


def _with_href(text: str, href: str | None) -> str:
    return f"{text} ({href})" if href and href != text else text


def _adf_inline_run(nodes: list) -> str:
    """Join sibling nodes; text pieces of one link (split by bold etc.) share one URL."""
    out, i = [], 0
    while i < len(nodes):
        href = _link_href(nodes[i])
        if href:
            j = i
            while j < len(nodes) and _link_href(nodes[j]) == href:
                j += 1
            out.append(_with_href("".join(n.get("text", "") for n in nodes[i:j]), href))
            i = j
        else:
            out.append(adf_to_text(nodes[i]))
            i += 1
    return "".join(out)


def _indent(text: str, pad: str) -> str:
    return "".join(pad + line if line.strip() else line for line in text.splitlines(True))


def _list_entry(marker: str, body: str) -> str:
    """One list item: the marker on the first line, later lines indented under it."""
    first, _, rest = body.strip("\n").partition("\n")
    entry = (marker + first).rstrip(" ") + "\n" if not first.strip() else marker + first + "\n"
    if rest:
        entry += _indent(rest + "\n", " " * len(marker))
    return entry


def _parse_date(text: str, schema_type: str):
    try:
        if schema_type == "date":
            return datetime.strptime(text, "%Y-%m-%d").date()
        # Jira sends e.g. 2024-05-01T10:15:30.000+0530; Excel cannot hold tz info.
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)
    except ValueError:
        return None


def issue_to_row(issue: dict, fields: list[ResolvedField]) -> list[Any]:
    data = issue.get("fields") or {}
    row = []
    for f in fields:
        if f.id == "key":
            row.append(issue.get("key", ""))
        else:
            row.append(format_value(data.get(f.id), f.schema_type))
    return row


def write_xlsx(path: Path, fields: list[ResolvedField], issues: Iterable[dict],
               timer: ExportTimer | None = None) -> int:
    """Write issues to `path`. Returns the number of issues written.

    With a `timer`, the time spent converting, adding, formatting and saving is
    added to it, and formatting and saving are logged as steps."""
    clock = time.perf_counter
    wb = Workbook()
    ws = wb.active
    ws.title = "Issues"
    ws.append([f.header for f in fields])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    ws.freeze_panes = "A2"

    widths = [len(f.header) for f in fields]
    count = 0
    for issue in issues:
        t0 = clock()
        row = issue_to_row(issue, fields)
        t1 = clock()
        ws.append(row)
        count += 1
        for i, v in enumerate(row):
            widths[i] = max(widths[i], min(len(str(v)), 60))
        if timer:
            timer.add(ROWS, t1 - t0)
            timer.add(EXCEL, clock() - t1)

    t0 = clock()
    for i, f in enumerate(fields):
        col = get_column_letter(i + 1)
        ws.column_dimensions[col].width = widths[i] + 2
        if f.schema_type == "datetime":
            for cell in ws[col][1:]:
                cell.number_format = "yyyy-mm-dd hh:mm"
        elif f.schema_type == "date":
            for cell in ws[col][1:]:
                cell.number_format = "yyyy-mm-dd"
    if count:
        ws.auto_filter.ref = ws.dimensions
    if timer:
        timer.add(FINISH, clock() - t0)
        timer.step("Formatted the sheet (column widths, date formats, filter)")
        with timer.timed(SAVE):
            wb.save(path)
        timer.step(f"Saved {path.name}")
    else:
        wb.save(path)
    return count
