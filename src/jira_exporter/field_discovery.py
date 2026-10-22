"""Find the fields a Jira project uses, so users can build fields.json from them."""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from jira_exporter.jira_client import JiraClient, JiraError

# The issue key is not a "field" in Jira's search results, but every export wants it.
KEY_FIELD_IDS = {"issuekey", "key"}
# System fields whose values don't fit in one Excel cell (they'd export as raw data),
# so they are listed but not ticked by default.
NOT_SUGGESTED = {"comment", "worklog", "watches", "votes", "attachment", "issuelinks",
                 "progress", "aggregateprogress", "timetracking", "issuerestriction"}
# Keys that describe a value rather than being one.
_META_KEYS = {"self", "iconUrl", "avatarUrls", "showField", "nonEditableReason",
              "shouldDisplay"}


@dataclass
class FieldUsage:
    id: str
    name: str
    custom: bool = False
    type: str = ""
    # Issue types whose create screen in this project offers the field.
    issue_types: set[str] = field(default_factory=set)
    # How many of the sampled issues have a non-empty value in it.
    used_in: int = 0

    @property
    def is_key(self) -> bool:
        return self.id in KEY_FIELD_IDS

    @property
    def suggested(self) -> bool:
        """Ticked by default: the key, and fields with values that export well."""
        return self.is_key or (self.used_in > 0 and self.id not in NOT_SUGGESTED)


@dataclass
class DiscoveryResult:
    project_key: str
    project_name: str
    fields: list[FieldUsage]
    sampled: int
    warnings: list[str] = field(default_factory=list)
    source_issue: str = ""  # set when the fields were read from one issue

    @property
    def relevant(self) -> list[FieldUsage]:
        """Fields the project actually uses: on a create screen or filled in somewhere."""
        return [f for f in self.fields if f.is_key or f.issue_types or f.used_in]


def has_value(value) -> bool:
    """True when a field value carries information (not None/empty/an empty rich-text doc)."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() not in ("", "{}")  # Cloud's Development field sends "{}"
    if isinstance(value, (list, tuple, set)):
        return len(value) > 0
    if isinstance(value, dict):
        if value.get("type") == "doc":  # Cloud rich text: empty unless it has content
            return bool(value.get("content"))
        # Fields Jira always returns as a counter: comment/worklog pages, watches, votes.
        for counter in ("total", "watchCount", "votes"):
            if isinstance(value.get(counter), int) and not isinstance(value.get(counter), bool):
                return value[counter] > 0
        # Otherwise a dict counts if anything in it, other than metadata, has a value
        # (so placeholders like {"issuerestrictions": {}, "shouldDisplay": false} don't).
        return any(has_value(v) for k, v in value.items()
                   if k not in _META_KEYS and not isinstance(v, bool))
    return True


ISSUE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")


def _sorted(fields) -> list[FieldUsage]:
    return sorted(fields, key=lambda f: (not f.is_key, -f.used_in, not f.issue_types,
                                         f.name.lower()))


def _counts(fid: str, value) -> bool:
    if fid == "workratio" and isinstance(value, (int, float)) and value < 0:
        return False  # -1 means "no estimate"
    return has_value(value)


def discover_from_issue(
    client: JiraClient,
    key: str,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DiscoveryResult:
    """Quick scan: read one issue's JSON (with field names) instead of the create screens
    and a sample of issues. `key` is an issue key (ABC-123), or a project key to use its
    most recently updated issue. Needs only one or two requests."""
    say = progress or (lambda _msg: None)
    stop = cancelled or (lambda: False)
    key = key.strip()
    if ISSUE_KEY.match(key):
        issue_key = key.upper()
    else:
        project = client.project(key)
        say(f"Finding the latest issue in {project.get('key', key)}...")
        latest = list(itertools.islice(
            client.search(jql_for_project(project.get("key", key)), ["summary"], page_size=1,
                          cancelled=stop), 1))
        if not latest:
            if stop():
                return DiscoveryResult(project.get("key", key), project.get("name", key), [], 0)
            raise JiraError(f"Project {project.get('key', key)} has no issues you can see, "
                            "so there is no sample issue to read.")
        issue_key = latest[0]["key"]
    if stop():
        return DiscoveryResult(key, key, [], 0)
    say(f"Reading {issue_key}...")
    issue = client.issue(issue_key)
    values = issue.get("fields") or {}
    names = issue.get("names") or {}
    schemas = issue.get("schema") or {}
    project = values.get("project") or {}

    fields: list[FieldUsage] = [FieldUsage("issuekey", "Key", used_in=1)]
    for fid in dict.fromkeys([*names, *values]):  # names first, keeping Jira's order
        if fid in KEY_FIELD_IDS:
            continue
        schema = schemas.get(fid) or {}
        kind = schema.get("type", "")
        if kind == "array" and schema.get("items"):
            kind = f"array of {schema['items']}"
        fields.append(FieldUsage(fid, names.get(fid, fid),
                                 custom=fid.startswith("customfield_") or "custom" in schema,
                                 type=kind, used_in=int(_counts(fid, values.get(fid)))))
    warning = (f"Fields read from one issue, {issue_key}. A field that is empty on this "
               "issue shows as unused even if other issues fill it in, and fields that "
               "aren't on this issue type are not listed.")
    return DiscoveryResult(project.get("key", issue_key.rsplit("-", 1)[0]),
                           project.get("name", project.get("key", "")) or issue_key,
                           _sorted(fields), 1, [warning], source_issue=issue_key)


def jql_for_project(project_key: str) -> str:
    escaped = project_key.replace("\\", "\\\\").replace('"', '\\"')
    return f'project = "{escaped}" ORDER BY updated DESC'


def discover_fields(
    client: JiraClient,
    project_key: str,
    sample_size: int = 200,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DiscoveryResult:
    """Combine three sources: all fields Jira knows, the project's create screens,
    and which fields are filled in on the project's most recently updated issues."""
    say = progress or (lambda _msg: None)
    stop = cancelled or (lambda: False)
    project_key = project_key.strip()

    project = client.project(project_key)
    project_key = project.get("key", project_key)
    project_name = project.get("name", project_key)
    if stop():
        return DiscoveryResult(project_key, project_name, [], 0, [])
    say(f"Reading the field list for {project_key}...")

    by_id: dict[str, FieldUsage] = {}
    for f in client.fields():
        schema = f.get("schema") or {}
        kind = schema.get("type", "")
        if kind == "array" and schema.get("items"):
            kind = f"array of {schema['items']}"
        by_id[f["id"]] = FieldUsage(f["id"], f.get("name", f["id"]), bool(f.get("custom")), kind)
    if not any(i in by_id for i in KEY_FIELD_IDS):
        by_id["issuekey"] = FieldUsage("issuekey", "Key")

    warnings: list[str] = []
    try:
        issue_types = [] if stop() else client.create_issue_types(project_key, cancelled=stop)
        for it in issue_types:
            if stop():
                break
            say(f"Reading create screen fields for {it.get('name', it.get('id'))}...")
            for cf in client.create_fields(project_key, str(it["id"]), cancelled=stop):
                fid = cf.get("fieldId") or cf.get("key") or cf.get("id")
                if not fid:
                    continue
                usage = by_id.setdefault(fid, FieldUsage(fid, cf.get("name", fid)))
                usage.issue_types.add(it.get("name", str(it["id"])))
    except JiraError as exc:
        # The sample below still shows which fields are filled in.
        if exc.status == 404 and client.is_cloud:
            reason = "Jira returned 404, which can mean you can't create issues in this project"
        elif exc.status == 404:
            reason = ("Jira returned 404: this lookup needs Data Center 8.4 or later, or you "
                      "can't create issues in this project")
        else:
            reason = str(exc).splitlines()[0]
        warnings.append(f"Could not read the project's create screens ({reason}).")

    sampled = 0
    if sample_size > 0 and not stop():
        say("Checking which fields recent issues use...")
        for issue in client.search(jql_for_project(project_key), ["*all"],
                                   page_size=min(100, sample_size), cancelled=stop):
            sampled += 1
            for fid, value in (issue.get("fields") or {}).items():
                if _counts(fid, value):
                    usage = by_id.setdefault(fid, FieldUsage(fid, fid))
                    usage.used_in += 1
            if sampled % 50 == 0:
                say(f"Checked {sampled} issues...")
            if sampled >= sample_size:
                break
        for key_id in KEY_FIELD_IDS:
            if key_id in by_id:
                by_id[key_id].used_in = sampled
    if sampled == 0 and not stop():
        warnings.append("No issues were checked, so only create screen fields are listed. "
                        "Raise 'Issues to check' to see which fields are filled in.")

    return DiscoveryResult(project_key, project_name, _sorted(by_id.values()), sampled, warnings)


def config_entries(selected: list[FieldUsage]) -> list[dict]:
    """fields.json entries: by id (unambiguous), with the Jira name as the column header."""
    entries = []
    for f in selected:
        if f.is_key:
            entries.append({"id": "key", "header": "Issue Key"})
        else:
            entries.append({"id": f.id, "header": f.name})
    return entries


def write_config(path: Path, selected: list[FieldUsage], template: Path | None = None) -> None:
    """Write a fields.json for `selected`, keeping other settings from `template` if given."""
    data: dict = {}
    if template and Path(template).is_file():
        try:
            data = json.loads(Path(template).read_text(encoding="utf-8"))
        except (OSError, ValueError):  # unreadable, not UTF-8, or not JSON
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("_comment", "Fields exported to Excel, in column order. Generated by "
                                "'Discover fields'; edit freely.")
    data.setdefault("max_results_per_page", 100)
    data["fields"] = config_entries(selected)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_field_list_xlsx(path: Path, result: DiscoveryResult) -> None:
    """Save the full field list (used and unused) for reference."""
    wb = Workbook()
    ws = wb.active
    ws.title = f"{result.project_key} fields"[:31]
    headers = ["Field name", "Field id", "Custom", "Type",
               f"Issues with a value (of {result.sampled} checked)", "On create screen for"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for f in result.fields:
        ws.append([f.name, "key" if f.is_key else f.id, "Yes" if f.custom else "No", f.type,
                   f.used_in, ", ".join(sorted(f.issue_types))])
    for i, width in enumerate([32, 22, 8, 20, 18, 40], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
