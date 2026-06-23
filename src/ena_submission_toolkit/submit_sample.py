#!/usr/bin/env python3
"""Submit samples to ENA via the Webin REST API v2.

Read a JSON file containing sample metadata, validate against an XSD schema,
and submit to ENA.

Credentials are read from environment variables::

    export ENA_WEBIN=Webin-XXXXX
    export ENA_WEBIN_PASSWORD=SECRET

Usage::

    python scripts/submit_sample.py --input samples.json --xsd assets/ena_schema --test
    python scripts/submit_sample.py --input samples.json --xsd assets/ena_schema --dry-run

Library usage::

    from scripts.submit_sample import build_manifest, validate_manifest, submit_manifest, submit_samples

    xml_bytes = build_manifest(samples)
    is_valid, messages = validate_manifest(xml_bytes, xsd_dir)
    success, accessions, messages = submit_manifest(xml_bytes, client)

    # Or all-in-one:
    results = submit_samples(Path("samples.json"), Path("assets/ena_schema"))
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

import httpx
import pendulum
import typer

from . import common
from ena_api import WebinClient
from linkml_lib.schema import UnitRule


app = typer.Typer(help="Submit samples to ENA via the Webin REST API v2.", add_completion=False)
logger = logging.getLogger("ena_submit.sample")

# Fields consumed as dedicated XML elements, not SAMPLE_ATTRIBUTE tag-value pairs.
_RESERVED_FIELDS: Final = frozenset({
    "alias", "SAMPLE_TITLE", "TAXON_ID", "SCIENTIFIC_NAME",
    "COMMON_NAME", "SAMPLE_DESCRIPTION", "SAMPLE_ABSTRACT",
})

# ---------------------------------------------------------------------------
# XML construction
# ---------------------------------------------------------------------------

def build_submission_xml(
    samples: list[dict[str, Any]],
    hold_until: str | None = None,
    checklist_id: str | None = None,
    slot_to_title: dict[str, str] | None = None,
    slot_to_unit: dict[str, str] | None = None,
    action: str = "ADD",
) -> ET.Element:
    """Build a WEBIN XML document for the given samples.

    Fields not in _RESERVED_FIELDS become SAMPLE_ATTRIBUTE tag-value pairs.
    slot_to_title maps slot names to human-readable tag names; slot_to_unit adds UNITS elements.
    """
    webin = ET.Element("WEBIN")

    submission = ET.SubElement(ET.SubElement(webin, "SUBMISSION_SET"), "SUBMISSION")
    submission.set("alias", "sample-submission-" + pendulum.now().format("YYYYMMDD-HHmmss"))
    actions = ET.SubElement(submission, "ACTIONS")
    ET.SubElement(ET.SubElement(actions, "ACTION"), action.upper())
    if hold_until:
        ET.SubElement(ET.SubElement(actions, "ACTION"), "HOLD").set("HoldUntilDate", hold_until)

    sample_set = ET.SubElement(webin, "SAMPLE_SET")
    for sample in samples:
        _add_sample_element(sample_set, sample, checklist_id, slot_to_title, slot_to_unit)

    return webin


def _add_sample_element(
    sample_set: ET.Element,
    sample: dict[str, Any],
    checklist_id: str | None = None,
    slot_to_title: dict[str, str] | None = None,
    slot_to_unit: dict[str, str] | None = None,
) -> None:
    alias = sample.get("alias") or (sample.get("SAMPLE_TITLE", "") or "").replace(" ", "_")[:50]
    sample_el = ET.SubElement(sample_set, "SAMPLE")
    sample_el.set("alias", alias)

    if title := sample.get("SAMPLE_TITLE", ""):
        ET.SubElement(sample_el, "TITLE").text = title

    sample_name = ET.SubElement(sample_el, "SAMPLE_NAME")
    ET.SubElement(sample_name, "TAXON_ID").text = str(sample.get("TAXON_ID", ""))
    if sci_name := sample.get("SCIENTIFIC_NAME", ""):
        ET.SubElement(sample_name, "SCIENTIFIC_NAME").text = sci_name
    if common_name := sample.get("COMMON_NAME", ""):
        ET.SubElement(sample_name, "COMMON_NAME").text = common_name

    if desc := sample.get("SAMPLE_DESCRIPTION") or sample.get("SAMPLE_ABSTRACT", ""):
        ET.SubElement(sample_el, "DESCRIPTION").text = desc

    attrs = {k: v for k, v in sample.items() if k not in _RESERVED_FIELDS and v is not None and str(v).strip()}
    if attrs or checklist_id:
        attrs_el = ET.SubElement(sample_el, "SAMPLE_ATTRIBUTES")
        if checklist_id:
            _add_sample_attribute(attrs_el, "ENA-CHECKLIST", checklist_id)
        for tag, value in attrs.items():
            tag_name = (slot_to_title or {}).get(tag, tag)
            _add_sample_attribute(attrs_el, tag_name, str(value), unit=(slot_to_unit or {}).get(tag))


def _add_sample_attribute(
    parent: ET.Element, tag_text: str, value_text: str, unit: str | None = None,
) -> None:
    attr = ET.SubElement(parent, "SAMPLE_ATTRIBUTE")
    ET.SubElement(attr, "TAG").text = tag_text
    ET.SubElement(attr, "VALUE").text = value_text
    if unit:
        ET.SubElement(attr, "UNITS").text = unit


# ---------------------------------------------------------------------------
# XSD validation
# ---------------------------------------------------------------------------

def _validate_sample_xml_structure(xml_bytes: bytes, messages: list[str]) -> tuple[bool, list[str]]:
    """Fallback structural check when lxml XSD validation is unavailable."""
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(f"ERROR: XML is not well-formed: {exc}")
        return False, messages

    messages.append("XML is well-formed (basic check passed)")
    sample_set = tree.find("SAMPLE_SET")
    if sample_set is None:
        messages.append("ERROR: Missing SAMPLE_SET element")
        return False, messages
    samples = sample_set.findall("SAMPLE")
    if not samples:
        messages.append("ERROR: No SAMPLE elements found")
        return False, messages

    for sample in samples:
        alias = sample.get("alias", "<no alias>")
        sample_name = sample.find("SAMPLE_NAME")
        if sample_name is None:
            messages.append(f"ERROR: SAMPLE '{alias}' missing SAMPLE_NAME")
            return False, messages
        taxon = sample_name.find("TAXON_ID")
        if taxon is None or not taxon.text:
            messages.append(f"ERROR: SAMPLE '{alias}' missing TAXON_ID")
            return False, messages
        messages.append(f"OK: SAMPLE '{alias}' has required elements")

    return True, messages


def validate_against_xsd(xml_bytes: bytes, xsd_dir: str | Path) -> tuple[bool, list[str]]:
    """Validate sample XML against SRA.sample.xsd (with structural fallback)."""
    return common.validate_xml_against_xsd(
        xml_bytes, xsd_dir,
        xsd_filename="SRA.sample.xsd", fragment_tag="SAMPLE_SET",
        fallback_checker=_validate_sample_xml_structure,
    )


# ---------------------------------------------------------------------------
# Public library API
# ---------------------------------------------------------------------------

def build_manifest(
    samples: list[dict[str, Any]],
    *,
    hold_until: str | None = None,
    checklist_id: str | None = None,
    slot_to_unit: dict[str, str] | None = None,
    action: str = "ADD",
) -> bytes:
    """Build an ENA sample XML submission document.

    Args:
        samples: List of sample metadata dicts (keys are field names).
        hold_until: Optional hold-until date (YYYY-MM-DD).
        checklist_id: Optional ENA checklist accession (e.g. "ERC000025").
        slot_to_unit: Optional field-name to unit mapping for SAMPLE_ATTRIBUTE
            ``UNITS`` elements.
        action: "ADD" for new samples or "MODIFY" to update existing ones.

    Returns:
        Serialised XML bytes ready for validate_manifest() or submit_manifest().
    """
    xml_root = build_submission_xml(
        samples,
        hold_until=hold_until,
        checklist_id=checklist_id,
        slot_to_unit=slot_to_unit,
        action=action,
    )
    return common.xml_to_bytes(xml_root)


def validate_manifest(xml_bytes: bytes, xsd_dir: Path) -> tuple[bool, list[str]]:
    """Validate an ENA sample XML manifest against SRA.sample.xsd.

    Returns (is_valid, messages).
    """
    return validate_against_xsd(xml_bytes, xsd_dir)


def submit_manifest(
    xml_bytes: bytes,
    client: WebinClient,
) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Submit an ENA sample XML manifest and parse the receipt.

    Args:
        xml_bytes: Serialised XML submission document.
        client: Authenticated WebinClient instance.

    Returns:
        Tuple of (success, accessions, messages).

    Raises:
        httpx.HTTPStatusError: On HTTP failure.
    """
    receipt = client.submit.xml(xml_bytes)
    accessions = [
        {
            "alias": r.alias,
            "accession": r.accession,
            "status": r.status,
            "holdUntilDate": r.hold_until_date,
            "external_accession": r.external_accession,
            "external_type": r.external_type,
        }
        for r in receipt.accessions
        if r.entity_type != "SUBMISSION"
    ]
    return receipt.success, accessions, receipt.messages + receipt.errors


def submit_batch(
    batch: list[dict[str, Any]],
    action: str,
    *,
    xsd: Path,
    hold_until: str | None,
    checklist_id: str | None = None,
    slot_to_unit: dict[str, str] | None = None,
    unit_rules: dict[str, UnitRule] | None = None,
    client: WebinClient,
    env_label: str,
) -> tuple[bool, list[dict[str, Any]]]:
    """Build, validate, and submit one batch of samples. Returns (success, accessions).

    If ``unit_rules`` is given (a schema-derived field-name -> UnitRule map,
    see ``linkml_lib.schema.unit_rules``), each record's unit-bearing values
    are normalised against those rules first via
    ``ena_common.normalise_sample_records`` — the resulting unit map is
    merged under ``slot_to_unit`` (explicit ``slot_to_unit`` entries take
    precedence, so callers can still override). Without ``unit_rules``,
    behaviour is unchanged from before — callers must pre-normalise and pass
    a complete ``slot_to_unit`` themselves.
    """
    if unit_rules:
        batch, computed_unit_map = common.normalise_sample_records(batch, unit_rules)
        slot_to_unit = {**computed_unit_map, **(slot_to_unit or {})}
    xml_bytes = build_manifest(
        batch,
        hold_until=hold_until,
        checklist_id=checklist_id,
        slot_to_unit=slot_to_unit,
        action=action,
    )
    is_valid, xsd_messages = validate_manifest(xml_bytes, xsd)
    for msg in xsd_messages:
        logger.info("  %s", msg)
    if not is_valid:
        raise ValueError(f"{action} XML failed XSD validation")
    logger.info("Submitting %s to ENA (%s)...", action, env_label)
    success, accessions, receipt_messages = submit_manifest(xml_bytes, client)
    for msg in receipt_messages:
        logger.info("  Receipt: %s", msg)
    return success, accessions


def _load_samples_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    # extract_records_from_json is intentionally lenient (it'll wrap a bare
    # dict as a single record as a last resort) — this CLI specifically
    # expects a DataHarmonizer export, so require the 'Container' key itself.
    if not isinstance(data, dict) or "Container" not in data:
        raise ValueError(
            f"Expected a DataHarmonizer JSON export with a 'Container' key; "
            f"got top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
    return common.extract_records_from_json(data) or []


def submit_samples(
    input_file: Path,
    xsd: Path,
    *,
    test: bool = False,
    hold_until: str | None = None,
    resubmit_with_modify: bool = False,
    check_for_duplicates: bool = False,
    force: bool = False,
) -> dict[str, list]:
    """Load, validate, and submit samples to ENA.

    Args:
        input_file: Path to a JSON file containing sample metadata.
        xsd: Directory containing SRA.sample.xsd and SRA.common.xsd.
        test: Use the ENA test service.
        hold_until: Hold samples private until this date (YYYY-MM-DD, max 2 years).
        resubmit_with_modify: If ADD fails, resubmit all records as MODIFY.
        check_for_duplicates: Check records against existing samples on the
            account by alias/title before submitting.
        force: With check_for_duplicates, resubmit matched duplicates as
            MODIFY instead of skipping them.

    Returns:
        Results dict with keys: submitted, modified, failed, duplicates.

    Raises:
        ValueError: On invalid input or failed validation.
        httpx.HTTPStatusError: On HTTP submission failure.
    """
    client = common.create_webin_client(test=test)
    env_label = "TEST" if test else "PRODUCTION"

    if hold_until:
        common.validate_hold_until(hold_until)

    logger.info("Loading input: %s", input_file)
    samples = _load_samples_json(input_file)
    logger.info("Loaded %d sample(s)", len(samples))

    results: dict[str, list] = {"submitted": [], "modified": [], "failed": [], "duplicates": []}

    if check_for_duplicates:
        account = [r.model_dump() for r in client.reports.list_samples()]
        dups = common.find_duplicates_by_alias_title(samples, account, title_field="SAMPLE_TITLE", entity_label="samples")
        to_submit, to_modify, duplicate_entries = common.classify_duplicates(
            samples, dups, title_field="SAMPLE_TITLE", force=force
        )
        results["duplicates"] = duplicate_entries
        if to_modify:
            success, accessions = submit_batch(
                to_modify, "MODIFY", xsd=xsd, hold_until=hold_until, client=client, env_label=env_label,
            )
            results["modified"] = accessions if success else []
            if not success:
                results["failed"].extend(accessions)
        samples = to_submit

    if not samples:
        return results

    success, accessions = submit_batch(
        samples, "ADD", xsd=xsd, hold_until=hold_until, client=client, env_label=env_label,
    )
    if success:
        logger.info("ADD successful: %d sample(s)", len(accessions))
        results["submitted"] = accessions
    elif resubmit_with_modify:
        logger.info("ADD failed; retrying as MODIFY...")
        success, accessions = submit_batch(
            samples, "MODIFY", xsd=xsd, hold_until=hold_until, client=client, env_label=env_label,
        )
        if success:
            logger.info("MODIFY successful: %d sample(s)", len(accessions))
            results["modified"] = accessions
        else:
            logger.error("MODIFY failed")
            results["failed"] = accessions
    else:
        logger.error("ADD failed")
        results["failed"] = accessions

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def main(
    input_file: Path = typer.Option(..., "--input", exists=True, help="Path to sample metadata JSON file"),
    xsd: Path = typer.Option(..., exists=True, file_okay=False, resolve_path=True, help="Directory containing SRA.sample.xsd and SRA.common.xsd"),
    test: bool = typer.Option(False, "--test", help="Use the ENA test service (submissions discarded daily)"),
    hold_until: str | None = typer.Option(None, "--hold-until", help="Hold samples private until this date (YYYY-MM-DD, max 2 years)"),
    log: Path | None = typer.Option(None, help="Path to log file"),
    output: Path | None = typer.Option(None, help="Path to write JSON results (default: stdout)"),
    resubmit_with_modify: bool = typer.Option(False, "--resubmit-with-modify", help="If ADD fails, resubmit all records as MODIFY"),
    check_for_duplicates: bool = typer.Option(False, "--check-for-duplicates", help="Check records against existing samples on the account by alias/title before submitting"),
    force: bool = typer.Option(False, "--force", help="With --check-for-duplicates, resubmit matched duplicates as MODIFY instead of skipping them"),
) -> None:
    """Submit samples to ENA via the Webin REST API v2."""
    common.setup_logging(log)
    logger.info("ENA Sample Submission — environment: %s", "TEST" if test else "PRODUCTION")
    try:
        results = submit_samples(
            input_file, xsd,
            test=test, hold_until=hold_until,
            resubmit_with_modify=resubmit_with_modify,
            check_for_duplicates=check_for_duplicates,
            force=force,
        )
    except (ValueError, httpx.HTTPStatusError) as exc:
        logger.error("%s", exc)
        raise typer.Exit(1)

    common.write_results(results, output)
    _log_summary(results)


def _log_summary(results: dict[str, list]) -> None:
    logger.info("=" * 60)
    logger.info("SUBMISSION SUMMARY")
    logger.info("  Submitted (ADD):   %d", len(results["submitted"]))
    for s in results["submitted"]:
        ext = s.get("external_accession", "")
        logger.info("    %s -> %s%s", s["alias"], s["accession"], f" ({ext})" if ext else "")
    logger.info("  Modified (MODIFY): %d", len(results["modified"]))
    for m in results["modified"]:
        ext = m.get("external_accession", "")
        logger.info("    %s -> %s%s", m["alias"], m["accession"], f" ({ext})" if ext else "")
    logger.info("  Failed:            %d", len(results["failed"]))
    logger.info("=" * 60)


if __name__ == "__main__":
    app()
