#!/usr/bin/env python3
"""Submit studies to ENA via the Webin REST API v2.

Read a JSON file containing study metadata, construct an XML submission
document, and submit studies to ENA.

Credentials are read from environment variables:

    export ENA_WEBIN=Webin-XXXXX
    export ENA_WEBIN_PASSWORD=SECRET

Usage::

    python scripts/submit_study.py --input studies.json --xsd assets/ena_schema --test

    # With hold date (max 2 years):
    python scripts/submit_study.py --input studies.json --xsd assets/ena_schema --hold-until 2028-01-01
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pendulum
import typer

from . import common
from ena_api import WebinClient

app = typer.Typer(help="Submit studies to ENA via the Webin REST API v2.", add_completion=False)
logger = logging.getLogger("ena_submit.study")


# -------------------------------------------------------------------
# XML construction
# -------------------------------------------------------------------

def build_submission_xml(
    studies: list[dict[str, Any]],
    hold_until: str | None = None,
    action: str = "ADD",
) -> ET.Element:
    """Build a WEBIN XML document for submitting studies."""
    webin = ET.Element("WEBIN")

    submission_set = ET.SubElement(webin, "SUBMISSION_SET")
    submission = ET.SubElement(submission_set, "SUBMISSION")
    submission.set("alias", "study-submission-" + pendulum.now().format("YYYYMMDD-HHmmss"))
    actions = ET.SubElement(submission, "ACTIONS")
    ET.SubElement(ET.SubElement(actions, "ACTION"), action.upper())
    if hold_until:
        hold_el = ET.SubElement(ET.SubElement(actions, "ACTION"), "HOLD")
        hold_el.set("HoldUntilDate", hold_until)

    project_set = ET.SubElement(webin, "PROJECT_SET")
    for study in studies:
        _add_project_element(project_set, study)

    return webin


def _add_project_element(project_set: ET.Element, study: dict[str, Any]) -> None:
    alias = study.get("alias", study.get("STUDY_TITLE", "").replace(" ", "_")[:50])
    project = ET.SubElement(project_set, "PROJECT")
    project.set("alias", alias)

    name_text = study.get("CENTER_PROJECT_NAME", alias)
    if name_text:
        ET.SubElement(project, "NAME").text = name_text

    ET.SubElement(project, "TITLE").text = study.get("STUDY_TITLE", "")

    desc_text = study.get("STUDY_ABSTRACT") or study.get("STUDY_DESCRIPTION", "")
    if desc_text:
        ET.SubElement(project, "DESCRIPTION").text = desc_text

    ET.SubElement(ET.SubElement(project, "SUBMISSION_PROJECT"), "SEQUENCING_PROJECT")

    study_type = study.get("existing_study_type")
    if study_type:
        attrs = ET.SubElement(project, "PROJECT_ATTRIBUTES")
        _add_project_attribute(attrs, "existing_study_type", study_type)
        new_type = study.get("new_study_type")
        if new_type and study_type == "Other":
            _add_project_attribute(attrs, "new_study_type", new_type)


def _add_project_attribute(parent: ET.Element, tag_text: str, value_text: str) -> None:
    attr = ET.SubElement(parent, "PROJECT_ATTRIBUTE")
    ET.SubElement(attr, "TAG").text = tag_text
    ET.SubElement(attr, "VALUE").text = value_text


# -------------------------------------------------------------------
# XSD fallback structural checker
# -------------------------------------------------------------------

def _validate_study_xml_structure(xml_bytes: bytes, messages: list[str]) -> tuple[bool, list[str]]:
    """Fallback structural check for study XML."""
    try:
        tree = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        messages.append(f"ERROR: XML is not well-formed: {exc}")
        return False, messages

    messages.append("XML is well-formed (basic check passed)")

    project_set = tree.find("PROJECT_SET")
    if project_set is None:
        messages.append("ERROR: Missing PROJECT_SET element")
        return False, messages

    projects = project_set.findall("PROJECT")
    if not projects:
        messages.append("ERROR: No PROJECT elements found")
        return False, messages

    for proj in projects:
        alias = proj.get("alias", "<no alias>")
        title = proj.find("TITLE")
        if title is None or not title.text:
            messages.append(f"ERROR: PROJECT '{alias}' missing TITLE")
            return False, messages
        sp = proj.find("SUBMISSION_PROJECT")
        if sp is None:
            messages.append(f"ERROR: PROJECT '{alias}' missing SUBMISSION_PROJECT")
            return False, messages
        messages.append(f"OK: PROJECT '{alias}' has required elements")

    return True, messages


# -------------------------------------------------------------------
# Public library functions
# -------------------------------------------------------------------

def build_manifest(
    studies: list[dict[str, Any]],
    *,
    hold_until: str | None = None,
    action: str = "ADD",
) -> bytes:
    """Build and serialise a WEBIN XML submission document for studies."""
    xml_root = build_submission_xml(studies, hold_until=hold_until, action=action)
    return common.xml_to_bytes(xml_root)


def validate_manifest(xml_bytes: bytes, xsd_dir: str | Path) -> tuple[bool, list[str]]:
    """Validate study XML against ENA.project.xsd."""
    return common.validate_xml_against_xsd(
        xml_bytes, xsd_dir,
        xsd_filename="ENA.project.xsd",
        fragment_tag="PROJECT_SET",
        fallback_checker=_validate_study_xml_structure,
    )


def submit_manifest(
    xml_bytes: bytes,
    client: WebinClient,
) -> tuple[bool, list[dict[str, str]], list[str]]:
    """Submit XML to ENA and parse the receipt.

    Args:
        xml_bytes: Serialised WEBIN XML document.
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
    client: WebinClient,
    env_label: str,
) -> tuple[bool, list[dict[str, Any]]]:
    """Build, validate, and submit one batch of studies. Returns (success, accessions)."""
    xml_bytes = build_manifest(batch, hold_until=hold_until, action=action)
    is_valid, xsd_msgs = validate_manifest(xml_bytes, xsd)
    for msg in xsd_msgs:
        logger.info("  %s", msg)
    if not is_valid:
        raise ValueError(f"{action} XML failed XSD validation")
    logger.info("Submitting %s to ENA (%s)...", action, env_label)
    success, accessions, receipt_msgs = submit_manifest(xml_bytes, client)
    for msg in receipt_msgs:
        logger.info("  Receipt: %s", msg)
    return success, accessions


# -------------------------------------------------------------------
# JSON loader
# -------------------------------------------------------------------

def _load_studies_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    try:
        container = data["Container"]
        records = next(v for v in container.values() if isinstance(v, list))
    except (KeyError, TypeError, StopIteration):
        raise ValueError(
            f"Expected a DataHarmonizer JSON export with a 'Container' key; "
            f"got top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
    return records


# -------------------------------------------------------------------
# Full submission pipeline
# -------------------------------------------------------------------

def submit_studies(
    input_file: Path,
    xsd: Path,
    *,
    test: bool = False,
    hold_until: str | None = None,
    resubmit_with_modify: bool = False,
) -> dict[str, list]:
    """Full study submission pipeline. Returns a results dict."""
    env_label = "TEST" if test else "PRODUCTION"
    client = common.create_webin_client(test=test)

    if hold_until:
        common.validate_hold_until(hold_until)

    logger.info("Loading input: %s", input_file)
    studies = _load_studies_json(input_file)
    logger.info("Loaded %d study/studies from input", len(studies))

    results: dict[str, list] = {"submitted": [], "modified": [], "failed": []}

    success, accessions = submit_batch(
        studies, "ADD", xsd=xsd, hold_until=hold_until, client=client, env_label=env_label,
    )
    if success:
        logger.info("ADD successful: %d study/studies", len(accessions))
        results["submitted"] = accessions
    elif resubmit_with_modify:
        logger.info("ADD failed; retrying as MODIFY...")
        success, accessions = submit_batch(
            studies, "MODIFY", xsd=xsd, hold_until=hold_until, client=client, env_label=env_label,
        )
        if success:
            logger.info("MODIFY successful: %d study/studies", len(accessions))
            results["modified"] = accessions
        else:
            logger.error("MODIFY failed")
            results["failed"] = accessions
    else:
        logger.error("ADD failed")
        results["failed"] = accessions

    return results


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

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


@app.command()
def main(
    input_file: Path = typer.Option(..., "--input", exists=True, help="Path to study metadata JSON file"),
    xsd: Path = typer.Option(..., exists=True, file_okay=False, resolve_path=True, help="Directory containing ENA.project.xsd and SRA.common.xsd"),
    test: bool = typer.Option(False, "--test", help="Use the ENA test service (submissions are discarded daily)"),
    hold_until: str | None = typer.Option(None, "--hold-until", help="Hold studies private until this date (YYYY-MM-DD, max 2 years from now)"),
    log: Path | None = typer.Option(None, help="Path to log file"),
    output: Path | None = typer.Option(None, help="Path to write JSON accession results (default: stdout)"),
    resubmit_with_modify: bool = typer.Option(False, "--resubmit-with-modify", help="If ADD fails, resubmit all records as MODIFY"),
) -> None:
    """Submit studies to ENA via the Webin REST API v2."""
    common.setup_logging(log)
    env_label = "TEST" if test else "PRODUCTION"
    logger.info("ENA Study Submission — environment: %s", env_label)
    try:
        results = submit_studies(
            input_file, xsd,
            test=test, hold_until=hold_until,
            resubmit_with_modify=resubmit_with_modify,
        )
    except (ValueError, httpx.HTTPStatusError) as exc:
        logger.error("%s", exc)
        raise typer.Exit(1)
    common.write_results(results, output)
    _log_summary(results)


if __name__ == "__main__":
    app()
