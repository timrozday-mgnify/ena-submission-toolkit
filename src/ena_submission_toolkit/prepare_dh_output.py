#!/usr/bin/env python3
"""Rename DataHarmonizer export field names from slot titles to annotations.id values.

DataHarmonizer exports use human-readable slot title values as JSON field names
(e.g. "Sample alias (ENA sample alias)").  This script renames those keys to
the canonical annotations.id values defined in the LinkML schema (e.g. "alias"),
preserving the Container structure and all other top-level metadata.

Usage::

    python scripts/prepare_dh_output.py input.json schema.yaml -o output.json
    python scripts/prepare_dh_output.py input.json schema.yaml          # → stdout
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from linkml_lib import io as linkml_io

app = typer.Typer(help="Rename DataHarmonizer export fields to annotations.id values.")


def _build_title_to_id_map(schema: dict[str, Any]) -> dict[str, str]:
    """Return a mapping of slot title → annotations.id for all titled slots."""
    result: dict[str, str] = {}
    for slot_name, defn in schema.get("slots", {}).items():
        if not defn:
            continue
        title = defn.get("title", "")
        if not title:
            continue
        ann_id = (defn.get("annotations") or {}).get("id", slot_name)
        result[title] = ann_id
    return result


def _rename_records(
    records: list[dict[str, Any]],
    title_to_id: dict[str, str],
) -> list[dict[str, Any]]:
    return [{title_to_id.get(k, k): v for k, v in record.items()} for record in records]


def prepare_data(data: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Rename a DataHarmonizer JSON export's fields to annotations.id, return result.

    Pure in-memory variant of :func:`prepare` — callers that already hold the
    export and schema as dicts (e.g. a server process) can call this directly
    instead of round-tripping through files.
    """
    try:
        container = data["Container"]
        container_key = next(k for k, v in container.items() if isinstance(v, list))
    except (KeyError, TypeError, StopIteration) as exc:
        raise ValueError(
            f"Expected a DataHarmonizer JSON export with a 'Container' key containing "
            f"a list; got top-level keys: "
            f"{list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        ) from exc

    title_to_id = _build_title_to_id_map(schema)
    renamed = _rename_records(container[container_key], title_to_id)
    return {**data, "Container": {container_key: renamed}}


def prepare(input_path: Path, schema_path: Path) -> dict[str, Any]:
    """Load a DataHarmonizer JSON export + schema from disk, then prepare_data()."""
    data = json.loads(input_path.read_text())
    schema = linkml_io.load_any(schema_path)
    if schema is None:
        raise ValueError(f"Could not load schema from {schema_path}")
    return prepare_data(data, schema)


@app.command()
def main(
    input_file: Path = typer.Argument(..., help="DataHarmonizer JSON export"),
    schema_file: Path = typer.Argument(..., help="LinkML YAML schema"),
    output: Path | None = typer.Option(None, "-o", "--output", help="Output path (default: stdout)"),
) -> None:
    """Rename DataHarmonizer export field names to annotations.id values."""
    result = prepare(input_file, schema_file)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if output:
        output.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    app()
