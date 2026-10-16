"""Loading the export field list from fields.json and matching it to Jira fields."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

# The issue key is returned at the top level of each issue, not under "fields".
KEY_ALIASES = {"key", "issuekey"}


class ConfigError(Exception):
    pass


@dataclass
class FieldSpec:
    id: str | None
    name: str | None
    header: str | None = None


@dataclass
class ExportConfig:
    fields: list[FieldSpec]
    page_size: int = 100
    path: Path | None = None


@dataclass
class ResolvedField:
    id: str
    header: str
    schema_type: str | None = None


def default_config_path() -> Path:
    """Find fields.json: next to the executable/script first, then the bundled copy."""
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable).parent / "config" / "fields.json"]
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(bundle / "config" / "fields.json")
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = [Path.cwd() / "config" / "fields.json", root / "config" / "fields.json"]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def load_config(path: Path) -> ExportConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from exc

    entries = raw.get("fields")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: 'fields' must be a non-empty list.")

    specs = []
    for i, e in enumerate(entries):
        if isinstance(e, str):
            e = {"name": e}
        if not isinstance(e, dict) or not (e.get("id") or e.get("name")):
            raise ConfigError(f"{path}: field #{i + 1} needs an 'id' or a 'name'.")
        specs.append(FieldSpec(id=e.get("id"), name=e.get("name"), header=e.get("header")))

    page_size = int(raw.get("max_results_per_page", 100))
    return ExportConfig(fields=specs, page_size=max(1, min(page_size, 1000)), path=Path(path))


def resolve_fields(
    specs: list[FieldSpec], jira_fields: list[dict]
) -> tuple[list[ResolvedField], list[str]]:
    """Match config entries to Jira field ids. Returns (resolved, problems)."""
    by_id = {f["id"]: f for f in jira_fields}
    by_name: dict[str, list[dict]] = {}
    for f in jira_fields:
        by_name.setdefault(f.get("name", "").strip().lower(), []).append(f)

    resolved, problems = [], []
    for s in specs:
        if s.id and s.id.lower() in KEY_ALIASES:
            resolved.append(ResolvedField("key", s.header or "Key"))
            continue
        if s.id:
            f = by_id.get(s.id)
            if not f:
                problems.append(f"Field id '{s.id}' was not found in Jira.")
                continue
        else:
            matches = by_name.get(s.name.strip().lower(), [])
            if not matches:
                problems.append(f"Field name '{s.name}' was not found in Jira.")
                continue
            if len(matches) > 1:
                ids = ", ".join(m["id"] for m in matches)
                problems.append(
                    f"Field name '{s.name}' matches several fields ({ids}); "
                    "use 'id' in the config instead. Using the first one."
                )
            f = matches[0]
        if f["id"] in KEY_ALIASES:
            resolved.append(ResolvedField("key", s.header or f.get("name", "Key")))
            continue
        schema = (f.get("schema") or {}).get("type")
        resolved.append(ResolvedField(f["id"], s.header or f.get("name", f["id"]), schema))
    return resolved, problems
