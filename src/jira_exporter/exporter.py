"""Turning Jira issues into rows and writing them to an .xlsx file."""

from __future__ import annotations

from datetime import datetime
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
