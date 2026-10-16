"""Turning Jira issues into rows and writing them to an .xlsx file."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from jira_exporter.config import ResolvedField

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
               "taskItem", "decisionItem", "blockCard", "embedCard", "mediaSingle"}


def adf_to_text(node: Any, depth: int = 0) -> str:
    """Flatten an Atlassian Document Format tree into plain text.

    Lists keep their numbering and nesting (two spaces per level), table rows
    become "a | b | c" lines, and link targets are kept next to their text.
    """
    if isinstance(node, list):
        return "".join(adf_to_text(n, depth) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        text = node.get("text", "")
        for mark in node.get("marks") or []:
            href = (mark.get("attrs") or {}).get("href") if mark.get("type") == "link" else None
            if href and href != text:
                text = f"{text} ({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind == "date":
        try:
            ts = int(attrs.get("timestamp")) / 1000
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return str(attrs.get("timestamp") or "")
    if kind in ("mention", "emoji", "status", "inlineCard", "blockCard", "embedCard"):
        text = str(attrs.get("text") or attrs.get("shortName") or attrs.get("url") or "")
        return text + "\n" if kind in _ADF_BLOCKS else text
    if kind in ("bulletList", "orderedList"):
        first = attrs.get("order", 1) if kind == "orderedList" else None
        lines = []
        for i, item in enumerate(c for c in node.get("content", []) if isinstance(c, dict)):
            marker = f"{first + i}. " if first is not None else "- "
            body = adf_to_text(item.get("content", []), depth + 1).strip("\n")
            lines.append("  " * depth + marker + body.lstrip(" ") + "\n")
        return "".join(lines)
    if kind == "taskItem":
        box = "[x] " if attrs.get("state") == "DONE" else "[ ] "
        return "  " * depth + box + adf_to_text(node.get("content", []), depth).strip("\n") + "\n"
    if kind == "tableRow":
        cells = [" ".join(adf_to_text(c.get("content", []), depth).split())
                 for c in node.get("content", []) if isinstance(c, dict)]
        return " | ".join(cells) + "\n"
    text = adf_to_text(node.get("content", []), depth)
    if kind in _ADF_BLOCKS:
        text = text.rstrip("\n") + "\n"
    return text


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


def write_xlsx(path: Path, fields: list[ResolvedField], issues: Iterable[dict]) -> int:
    """Write issues to `path`. Returns the number of issues written."""
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
        row = issue_to_row(issue, fields)
        ws.append(row)
        count += 1
        for i, v in enumerate(row):
            widths[i] = max(widths[i], min(len(str(v)), 60))

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
    wb.save(path)
    return count
