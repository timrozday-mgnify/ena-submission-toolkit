"""Shared utilities for ENA submission scripts.

Provide logging, credential management, file loading,
LinkML and XSD validation, duplicate detection,
XML serialisation, and result output used by
``submit_study.py`` and ``submit_sample.py``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, Final

import lxml.etree
import pendulum
from ena_api import WebinClient, WebinConfig
from linkml_lib.schema import UnitRule

_LOGGER_NAME: Final = "ena_submit"
logger = logging.getLogger(_LOGGER_NAME)


# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------

_MAX_HOLD_YEARS: Final = 2


# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------

def setup_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    """Configure stderr and optional file logging for the ena_submit logger tree.

    Args:
        log_file: Optional path to a debug-level log file.
        level: Logging level for the stderr handler (the file handler, if any,
            always logs at DEBUG).
    """
    root = logging.getLogger(_LOGGER_NAME)
    if root.handlers:
        return

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(level)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


# -------------------------------------------------------------------
# Credentials
# -------------------------------------------------------------------

def get_credentials() -> tuple[str, str]:
    """Read ENA credentials from ENA_WEBIN and ENA_WEBIN_PASSWORD env vars.

    Returns:
        Tuple of (username, password).

    Raises:
        ValueError: If either variable is unset or empty.
    """
    username = os.environ.get("ENA_WEBIN", "").strip()
    password = os.environ.get("ENA_WEBIN_PASSWORD", "").strip()
    if not username or not password:
        raise ValueError("ENA_WEBIN and ENA_WEBIN_PASSWORD environment variables must be set")
    return username, password


def create_webin_client(test: bool = False) -> WebinClient:
    """Create an authenticated WebinClient using environment credentials."""
    username, password = get_credentials()
    return WebinClient(config=WebinConfig(webin_id=username, password=password, test=test))


# -------------------------------------------------------------------
# XML utilities
# -------------------------------------------------------------------

def xml_to_bytes(root: ET.Element) -> bytes:
    """Serialise an ElementTree element to UTF-8 bytes with XML declaration."""
    buf = BytesIO()
    ET.ElementTree(root).write(buf, encoding="UTF-8", xml_declaration=True)
    return buf.getvalue()


# -------------------------------------------------------------------
# Hold-until date validation
# -------------------------------------------------------------------

def validate_hold_until(hold_until: str) -> pendulum.Date:
    """Parse and validate a hold-until date string (YYYY-MM-DD).

    Raises:
        ValueError: If the date is invalid, in the past, or more than 2 years ahead.
    """
    try:
        hold_date = pendulum.parse(hold_until, exact=True)
    except (ValueError, pendulum.parsing.ParserError):
        raise ValueError(f"Invalid date format: {hold_until!r}. Expected YYYY-MM-DD.") from None

    today = pendulum.today().date()
    max_date = today.add(years=_MAX_HOLD_YEARS)

    if hold_date > max_date:
        raise ValueError(
            f"Hold date {hold_until} is more than {_MAX_HOLD_YEARS} years from today "
            f"({today}). Maximum allowed: {max_date}."
        )
    if hold_date <= today:
        raise ValueError(f"Hold date {hold_until} is not in the future (today is {today}).")

    return hold_date


# -------------------------------------------------------------------
# ENA checklist XML parsing
# -------------------------------------------------------------------

def parse_checklist_units(xml_path: str | Path) -> dict[str, str]:
    """Parse an ENA checklist XML and return a mapping of field name to unit string."""
    units: dict[str, str] = {}
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError as exc:
        logger.warning("Could not parse checklist XML %s: %s", xml_path, exc)
        return units

    for field in tree.iter("FIELD"):
        name_el = field.find("NAME")
        if name_el is None or not name_el.text:
            continue
        units_el = field.find("UNITS")
        if units_el is None:
            continue
        unit_el = units_el.find("UNIT")
        if unit_el is None or not unit_el.text:
            continue
        units[name_el.text.strip()] = unit_el.text.strip()

    return units


# -------------------------------------------------------------------
# XSD validation
# -------------------------------------------------------------------

def validate_xml_against_xsd(
    xml_bytes: bytes,
    xsd_dir: str | Path,
    xsd_filename: str,
    fragment_tag: str | None = None,
    fallback_checker: Callable[[bytes, list[str]], tuple[bool, list[str]]] | None = None,
) -> tuple[bool, list[str]]:
    """Validate XML bytes against an XSD schema file in xsd_dir.

    Args:
        fragment_tag: If set, extract this child element from the document before validating.
        fallback_checker: Called when the XSD schema cannot be built (e.g. missing imports).
    """
    messages: list[str] = []
    xsd_root = Path(xsd_dir).resolve()
    xsd_file = xsd_root / xsd_filename

    if not xsd_file.is_file():
        messages.append(f"ERROR: {xsd_filename} not found in {xsd_root}")
        return False, messages

    common_file = xsd_root / "SRA.common.xsd"
    if not common_file.is_file():
        messages.append(f"WARNING: SRA.common.xsd not found in {xsd_root} — full XSD validation may fail")

    with open(xsd_file, "rb") as fh:
        xsd_doc = lxml.etree.parse(fh, base_url=f"file://{xsd_root}/")

    try:
        xsd_schema = lxml.etree.XMLSchema(xsd_doc)
        full_doc = lxml.etree.fromstring(xml_bytes)

        doc_to_validate = full_doc
        if fragment_tag is not None:
            fragment = full_doc.find(fragment_tag)
            if fragment is None:
                messages.append(f"ERROR: No {fragment_tag} element found in XML")
                return False, messages
            doc_to_validate = fragment

        if xsd_schema.validate(doc_to_validate):
            messages.append("XSD validation passed (lxml)")
            return True, messages

        for error in xsd_schema.error_log:
            messages.append(f"XSD ERROR: {error}")
        return False, messages

    except lxml.etree.XMLSchemaParseError as exc:
        messages.append(
            f"WARNING: Could not build XSD schema (missing imports?): {exc}. "
            "Falling back to basic XML well-formedness check."
        )

    if fallback_checker is not None:
        return fallback_checker(xml_bytes, messages)

    try:
        ET.fromstring(xml_bytes)
    except ET.ParseError as parse_exc:
        messages.append(f"ERROR: XML is not well-formed: {parse_exc}")
        return False, messages

    messages.append("XML is well-formed (basic check passed)")
    return True, messages


# -------------------------------------------------------------------
# File loading (CSV, TSV, XLS, XLSX, JSON)
# -------------------------------------------------------------------

def _is_metadata_row(row: Sequence[object]) -> bool:
    """Return True if row looks like a DataHarmonizer label row (at most one non-empty cell)."""
    return sum(1 for c in row if c is not None and str(c).strip()) <= 1


def extract_records_from_tabular(filepath: str | Path, delimiter: str = ",") -> list[dict[str, str]]:
    """Extract record dicts from a CSV or TSV file, skipping any DataHarmonizer metadata row."""
    with open(filepath, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=delimiter))

    if not rows:
        return []

    idx = 1 if _is_metadata_row(rows[0]) else 0
    if idx >= len(rows):
        return []

    headers = rows[idx]
    return [
        {col.strip(): val.strip() for col, val in zip(headers, row) if col.strip() and val.strip()}
        for row in rows[idx + 1:]
        if any(val.strip() for val in row)
    ]


def extract_records_from_json(
    input_data: object,
    record_keys: Sequence[str] = ("data",),
) -> list[dict[str, Any]] | None:
    """Extract record dicts from a JSON object in DataHarmonizer or plain list/dict formats."""
    if isinstance(input_data, list):
        return input_data

    if isinstance(input_data, dict):
        container = input_data.get("Container")
        if isinstance(container, dict):
            for key, val in container.items():
                if isinstance(val, list):
                    logger.info("Extracted records from Container.%s", key)
                    return val

        for key in record_keys:
            if key in input_data:
                return input_data[key]

        return [input_data]

    return None


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Load record dicts from a JSON, CSV, or TSV file.

    Dispatches on file suffix: ``.json`` is parsed via
    extract_records_from_json (DataHarmonizer ``Container`` exports or plain
    lists); ``.csv``/``.tsv`` via extract_records_from_tabular.

    Raises:
        ValueError: If the file extension is unsupported, or a JSON file does
            not contain a recognisable list of records.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".json":
        data = json.loads(path.read_text())
        records = extract_records_from_json(data)
        if records is None:
            raise ValueError(f"Could not extract records from JSON file: {path}")
        return records

    if suffix == ".csv":
        return extract_records_from_tabular(path, delimiter=",")

    if suffix == ".tsv":
        return extract_records_from_tabular(path, delimiter="\t")

    raise ValueError(f"Unsupported input file extension '{suffix}' for {path}; expected .json, .csv, or .tsv")



# -------------------------------------------------------------------
# Duplicate detection
# -------------------------------------------------------------------

def find_duplicates_by_alias_title(
    new_records: Sequence[dict[str, Any]],
    account_records: Sequence[dict[str, str]],
    title_field: str,
    entity_label: str,
) -> dict[int, dict[str, str]]:
    """Check new records against existing account records, matching by alias then title."""
    if not account_records:
        return {}

    by_title = {(rec.get("title") or "").strip(): rec for rec in account_records if (rec.get("title") or "").strip()}
    by_alias = {(rec.get("alias") or "").strip(): rec for rec in account_records if (rec.get("alias") or "").strip()}

    total = len(new_records)
    logger.info("Checking %d new %s against %d existing account %s...", total, entity_label, len(account_records), entity_label)

    duplicates: dict[int, dict[str, str]] = {}
    for i, record in enumerate(new_records):
        new_title = (record.get(title_field) or "").strip()
        new_alias = (record.get("alias") or "").strip()

        if not new_title and not new_alias:
            continue

        match = _match_by_alias_title(new_alias, new_title, by_alias, by_title)
        if match is not None:
            duplicates[i] = match
            logger.info("  Duplicate: '%s' matches %s -> %s (%s)",
                        new_title or new_alias, match["match_reason"], match["accession"], match["status"])
            if len(duplicates) == total:
                logger.info("All %s are duplicates — skipping further checks", entity_label)
                return duplicates

    return duplicates


def classify_duplicates(
    records: list[dict[str, Any]],
    duplicates: dict[int, dict[str, Any]],
    *,
    title_field: str,
    force: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split records into (to_submit, to_modify, duplicate_entries) based on detected duplicates.

    Args:
        records: Full list of input records.
        duplicates: Output of find_duplicates_by_alias_title — maps input index to match info.
        title_field: Key used to extract the human-readable title from each record.
        force: If True, duplicates are added to to_modify with their existing alias.

    Returns:
        to_submit: Records with no duplicate match.
        to_modify: Duplicate records with alias overridden to existing alias (only when force=True).
        duplicate_entries: Summary dicts suitable for results["duplicates"].
    """
    to_submit = [r for i, r in enumerate(records) if i not in duplicates]
    to_modify: list[dict[str, Any]] = []
    duplicate_entries: list[dict[str, Any]] = []

    for idx, dup_info in duplicates.items():
        title = (records[idx].get(title_field) or f"record[{idx}]").strip()
        action_label = "will be re-submitted with MODIFY" if force else "will NOT be submitted"
        logger.warning("DUPLICATE: '%s' matches existing %s (accession: %s) — %s",
                       title, dup_info["match_reason"], dup_info["accession"], action_label)
        duplicate_entries.append({
            "input_index": idx,
            "title": title,
            "alias": records[idx].get("alias", ""),
            "existing_accession": dup_info["accession"],
            "existing_secondary_accession": dup_info.get("secondary_accession", ""),
            "match_reason": dup_info["match_reason"],
        })
        if force:
            record_copy = dict(records[idx])
            if existing_alias := dup_info.get("alias"):
                record_copy["alias"] = existing_alias
            to_modify.append(record_copy)

    return to_submit, to_modify, duplicate_entries


def _match_by_alias_title(
    new_alias: str,
    new_title: str,
    by_alias: dict[str, dict[str, str]],
    by_title: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    if new_alias and new_alias in by_alias:
        rec, reason = by_alias[new_alias], f"alias '{new_alias}'"
    elif new_title and new_title in by_title:
        rec, reason = by_title[new_title], f"title '{new_title}'"
    else:
        return None

    return {
        "accession": rec.get("accession", ""),
        "secondary_accession": rec.get("secondary_accession", ""),
        "alias": rec.get("alias", ""),
        "title": rec.get("title", ""),
        "status": rec.get("status", "UNKNOWN"),
        "match_reason": reason,
    }


# -------------------------------------------------------------------
# Result output
# -------------------------------------------------------------------

def write_results(results: dict[str, list[dict[str, Any]]], output_path: Path | None) -> None:
    """Write JSON results to a file (if output_path given) or stdout."""
    json_str = json.dumps(results, indent=2)
    if output_path:
        with open(output_path, "w") as fh:
            fh.write(json_str + "\n")
        logger.info("Results written to %s", output_path)
    else:
        print(json_str)


def write_records_csv(records: Sequence[dict[str, Any]], output_path: Path | None) -> None:
    """Write a list of flat dicts as CSV to a file (if output_path given) or stdout.

    The header is the union of keys across all records, in first-seen order.
    """
    fieldnames: list[str] = []
    for record in records:
        for key in record:
            if key not in fieldnames:
                fieldnames.append(key)

    if output_path:
        with open(output_path, "w", newline="") as fh:
            _write_csv(fh, fieldnames, records)
        logger.info("Wrote %d record(s) to %s", len(records), output_path)
    else:
        _write_csv(sys.stdout, fieldnames, records)


def _write_csv(fh: Any, fieldnames: list[str], records: Sequence[dict[str, Any]]) -> None:
    writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(record)


# -------------------------------------------------------------------
# Sample attribute unit normalisation
#
# ENA's SAMPLE_ATTRIBUTE/UNITS is a single value per field for the whole
# submission batch (see build_submission_xml's slot_to_unit parameter), but a
# user/DataHarmonizer-entered value may carry its own unit text (e.g.
# "200 mg") that doesn't match what the checklist accepts. These functions
# parse, validate, and convert such values against a schema's UnitRules
# (linkml_lib.schema.unit_rules) so submit_sample.submit_batch can accept raw
# records directly instead of requiring callers to pre-normalise them.
# -------------------------------------------------------------------

_INTEGER_RE: Final = re.compile(r"[+-]?[0-9]+")
_NUMBER_WITH_UNIT_RE: Final = re.compile(
    r"^(?P<number>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?)"
    r"(?:\s+(?P<unit>\S.+))?$"
)
_UNIT_CONVERSIONS: Final = {
    ("mL", "L"): Decimal("0.001"),
    ("ml", "L"): Decimal("0.001"),
}


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _converted_unit_value(number: str, unit: str, allowed_units: tuple[str, ...]) -> tuple[str, str] | None:
    for target_unit in allowed_units:
        factor = _UNIT_CONVERSIONS.get((unit, target_unit))
        if factor is None:
            continue
        try:
            converted = Decimal(number) * factor
        except InvalidOperation as exc:
            raise ValueError(f"Could not parse numeric value {number!r}") from exc
        return _format_decimal(converted), target_unit
    return None


def normalise_unit_value(field: str, value: str, rule: UnitRule) -> tuple[str, str | None]:
    """Parse a ``"<number> [unit]"`` value against a slot's allowed/default units.

    Returns ``(normalised_value, unit)`` — ``unit`` is ``None`` if ``value``
    doesn't look like a bare number with an optional trailing unit (left
    untouched in that case). Raises ``ValueError`` if an explicit unit isn't
    accepted (and can't be converted to one that is), or if no unit can be
    determined at all.
    """
    match = _NUMBER_WITH_UNIT_RE.match(value)
    if match is None:
        return value, None

    number = match.group("number")
    unit = match.group("unit")
    if unit:
        if unit in rule.allowed_units:
            return number, unit
        converted = _converted_unit_value(number, unit, rule.allowed_units)
        if converted is not None:
            return converted
        allowed = ", ".join(rule.allowed_units) or "<none configured>"
        raise ValueError(f"{field!r} uses unsupported unit {unit!r}; allowed units: {allowed}")

    default_unit = rule.default_unit or (rule.allowed_units[0] if len(rule.allowed_units) == 1 else None)
    if default_unit is None:
        allowed = ", ".join(rule.allowed_units)
        raise ValueError(f"{field!r} value {value!r} needs an explicit unit; allowed units: {allowed}")
    if rule.allowed_units and default_unit not in rule.allowed_units:
        allowed = ", ".join(rule.allowed_units)
        raise ValueError(f"{field!r} default unit {default_unit!r} is not in allowed units: {allowed}")
    return value, default_unit


def normalise_sample_records(
    records: list[dict[str, Any]],
    unit_rules: dict[str, UnitRule],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Normalise sample records' unit-bearing values against schema unit rules.

    Returns ``(normalised_records, slot_to_unit)`` ready for
    ``build_submission_xml``/``submit_batch``'s ``slot_to_unit`` parameter.
    Also drops the optional ``"library size"`` field when its value isn't an
    integer (an ENA SRA.sample.xsd constraint, not specific to any one
    checklist) and enforces that each field resolves to one consistent unit
    across the whole batch (required since ``UNITS`` is set once per
    submission, not per record).
    """
    normalised: list[dict[str, Any]] = []
    slot_to_unit: dict[str, str] = {}

    for record in records:
        out: dict[str, Any] = {}
        for field, raw_value in record.items():
            value = str(raw_value).strip() if raw_value is not None else ""
            if field == "library size" and value and _INTEGER_RE.fullmatch(value) is None:
                logger.info("Dropping optional field 'library size' with non-integer value: %s", value)
                continue

            rule = unit_rules.get(field)
            if rule is None or not value:
                out[field] = raw_value
                continue

            normalised_value, unit = normalise_unit_value(field, value, rule)
            out[field] = normalised_value
            if unit is None:
                continue
            existing = slot_to_unit.get(field)
            if existing is not None and existing != unit:
                raise ValueError(f"{field!r} resolves to conflicting units: {existing!r}, {unit!r}")
            slot_to_unit[field] = unit
        normalised.append(out)

    if slot_to_unit:
        fields = ", ".join(f"{field}={unit}" for field, unit in sorted(slot_to_unit.items()))
        logger.info("Applied schema units to sample attributes: %s", fields)
    return normalised, slot_to_unit
