#!/usr/bin/env python3
"""Tests for submit_sample.py — ENA sample submission pipeline.

Covers:
    A. Unit tests for build_manifest / build_submission_xml / _add_sample_element
    B. Unit tests for validate_manifest
    C. Unit tests for submit_manifest
    D. CLI integration tests for main() using typer.testing.CliRunner
    E. CLI integration tests for main() using typer.testing.CliRunner

Usage:
    pytest test_submit_sample.py -v
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from ena_api import SampleReport, WebinClient, WebinConfig
from typer.testing import CliRunner


from ena_submission_toolkit.submit_sample import (  # noqa: E402
    app,
    build_manifest,
    build_submission_xml,
    submit_manifest,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def basic_sample() -> dict[str, Any]:
    return {
        "alias": "test-sample-001",
        "SAMPLE_TITLE": "A Basic Test Sample",
        "TAXON_ID": 1235509,
        "SCIENTIFIC_NAME": "synthetic metagenome",
        "collection_date": "2024-06-01",
    }


@pytest.fixture
def minimal_sample() -> dict[str, Any]:
    return {"alias": "minimal-001", "TAXON_ID": 9606, "SAMPLE_TITLE": "Minimal Sample"}




def _make_client(samples: list | None = None) -> WebinClient:
    """Return a WebinClient mock whose reports.list_samples returns *samples*."""
    client = MagicMock(spec=WebinClient)
    client.reports.list_samples.return_value = samples or []
    return client


@pytest.fixture
def account_sample_record() -> dict[str, str]:
    return {
        "title": "Existing Sample Title",
        "alias": "existing-sample-alias",
        "accession": "ERS099001",
        "secondary_accession": "SAMEA099001",
        "status": "PRIVATE",
    }


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# A. build_manifest / build_submission_xml
# ---------------------------------------------------------------------------

class TestBuildSubmissionXml:
    """Unit tests for the low-level build_submission_xml / _add_sample_element functions."""

    @staticmethod
    def _str(root: ET.Element) -> str:
        return ET.tostring(root, encoding="unicode")

    def test_sample_title_round_trips(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//TITLE").text == basic_sample["SAMPLE_TITLE"]

    def test_taxon_id_round_trips(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//TAXON_ID").text == str(basic_sample["TAXON_ID"])

    def test_alias_round_trips(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//SAMPLE").get("alias") == basic_sample["alias"]

    def test_scientific_name_present_when_given(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        el = root.find(".//SCIENTIFIC_NAME")
        assert el is not None
        assert el.text == basic_sample["SCIENTIFIC_NAME"]

    def test_common_name_omitted_when_absent(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//COMMON_NAME") is None

    def test_common_name_present_when_given(self) -> None:
        sample = {"alias": "s1", "TAXON_ID": 9606, "COMMON_NAME": "human"}
        root = build_submission_xml([sample])
        assert root.find(".//COMMON_NAME").text == "human"

    def test_non_reserved_fields_become_sample_attributes(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        tags = [el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")]
        assert "collection_date" in tags

    def test_checklist_id_is_first_sample_attribute(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], checklist_id="ERC000025")
        first_tag = root.find(".//SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE/TAG")
        assert first_tag is not None
        assert first_tag.text == "ENA-CHECKLIST"

    def test_units_added_when_slot_to_unit_provided(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], slot_to_unit={"collection_date": "ISO8601"})
        units_els = root.findall(".//SAMPLE_ATTRIBUTE/UNITS")
        assert any(el.text == "ISO8601" for el in units_els)

    def test_slot_to_title_renames_tag(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], slot_to_title={"collection_date": "Collection Date"})
        tags = [el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")]
        assert "Collection Date" in tags
        assert "collection_date" not in tags

    def test_hold_until_produces_hold_element(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], hold_until="2028-01-01")
        hold_el = root.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-01-01"

    def test_no_hold_element_when_hold_until_absent(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//HOLD") is None

    def test_modify_action_produces_modify_element(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample], action="MODIFY")
        assert "<MODIFY" in self._str(root) or "<MODIFY/>" in self._str(root)
        assert "<ADD" not in self._str(root)

    def test_add_action_produces_add_element(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        xml_str = self._str(root)
        assert "<ADD" in xml_str or "<ADD/>" in xml_str

    def test_multiple_samples_produce_multiple_sample_elements(
        self, basic_sample: dict[str, Any], minimal_sample: dict[str, Any]
    ) -> None:
        root = build_submission_xml([basic_sample, minimal_sample])
        assert len(root.findall(".//SAMPLE")) == 2

    def test_alias_derived_from_title_when_absent(self) -> None:
        sample = {"SAMPLE_TITLE": "My Derived Sample", "TAXON_ID": 9606}
        root = build_submission_xml([sample])
        alias = root.find(".//SAMPLE").get("alias", "")
        assert "My_Derived_Sample" in alias or "_" in alias

    def test_reserved_fields_not_in_sample_attributes(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        tags = {el.text for el in root.findall(".//SAMPLE_ATTRIBUTE/TAG")}
        for reserved in ("alias", "SAMPLE_TITLE", "TAXON_ID", "SCIENTIFIC_NAME"):
            assert reserved not in tags

    def test_sample_name_element_always_present(self, basic_sample: dict[str, Any]) -> None:
        root = build_submission_xml([basic_sample])
        assert root.find(".//SAMPLE_NAME") is not None


class TestBuildManifest:
    """Integration tests for build_manifest()."""

    def test_returns_bytes(self, basic_sample: dict[str, Any]) -> None:
        result = build_manifest([basic_sample])
        assert isinstance(result, bytes)
        assert b"SAMPLE_SET" in result

    def test_hold_until_passed_through(self, basic_sample: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_sample], hold_until="2028-06-15")
        root = ET.fromstring(xml_bytes)
        hold_el = root.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-06-15"

    def test_no_hold_element_when_hold_until_absent(self, basic_sample: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_sample])
        assert ET.fromstring(xml_bytes).find(".//HOLD") is None

    def test_modify_action_passed_through(self, basic_sample: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_sample], action="MODIFY")
        assert b"MODIFY" in xml_bytes

    def test_slot_to_unit_adds_sample_attribute_units(self, basic_sample: dict[str, Any]) -> None:
        sample = {**basic_sample, "sample storage temperature": "-80"}
        xml_bytes = build_manifest([sample], slot_to_unit={"sample storage temperature": "°C"})
        root = ET.fromstring(xml_bytes)
        attr = next(
            attr
            for attr in root.findall(".//SAMPLE_ATTRIBUTE")
            if attr.findtext("TAG") == "sample storage temperature"
        )
        assert attr.findtext("VALUE") == "-80"
        assert attr.findtext("UNITS") == "°C"


# ---------------------------------------------------------------------------
# B. validate_manifest
# ---------------------------------------------------------------------------

@pytest.fixture
def real_xsd_dir() -> Path:
    """Return the real ENA schema directory containing SRA.sample.xsd."""
    return Path(__file__).parent.parent / "src" / "ena_submission_toolkit" / "assets" / "ena_schema"


class TestValidateManifest:
    """Unit tests for validate_manifest().

    Tests that require schema validation use the real assets/ena_schema directory.
    The fallback structural checker is exercised when no XSD is available (tmp_path).
    """

    @staticmethod
    def _valid_xml(alias: str = "sample-1", taxon: str = "9606") -> bytes:
        return dedent(f"""\
            <?xml version='1.0' encoding='UTF-8'?>
            <WEBIN>
              <SAMPLE_SET>
                <SAMPLE alias="{alias}">
                  <TITLE>Test Sample</TITLE>
                  <SAMPLE_NAME><TAXON_ID>{taxon}</TAXON_ID></SAMPLE_NAME>
                </SAMPLE>
              </SAMPLE_SET>
            </WEBIN>
        """).encode("utf-8")

    def test_valid_xml_passes(self, real_xsd_dir: Path) -> None:
        is_valid, messages = validate_manifest(self._valid_xml(), real_xsd_dir)
        assert is_valid, f"Expected valid; messages: {messages}"

    def test_missing_sample_set_fails(self, real_xsd_dir: Path) -> None:
        xml_bytes = b"<?xml version='1.0'?><WEBIN/>"
        is_valid, messages = validate_manifest(xml_bytes, real_xsd_dir)
        assert not is_valid

    def test_missing_taxon_id_fails(self, real_xsd_dir: Path) -> None:
        xml_bytes = dedent("""\
            <WEBIN>
              <SAMPLE_SET>
                <SAMPLE alias="no-taxon">
                  <SAMPLE_NAME></SAMPLE_NAME>
                </SAMPLE>
              </SAMPLE_SET>
            </WEBIN>
        """).encode("utf-8")
        is_valid, messages = validate_manifest(xml_bytes, real_xsd_dir)
        assert not is_valid

    def test_malformed_xml_fails_with_fallback(self, tmp_path: Path) -> None:
        """Malformed XML fails even without the XSD file (fallback structural check)."""
        is_valid, messages = validate_manifest(b"<WEBIN><SAMPLE_SET><SAMPLE unclosed", tmp_path)
        assert not is_valid

    def test_returns_tuple_of_bool_and_list(self, real_xsd_dir: Path) -> None:
        result = validate_manifest(self._valid_xml(), real_xsd_dir)
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    def test_no_samples_in_set_fails_with_fallback(self, tmp_path: Path) -> None:
        """Empty SAMPLE_SET fails via the structural fallback checker."""
        xml_bytes = b"<WEBIN><SAMPLE_SET></SAMPLE_SET></WEBIN>"
        is_valid, _ = validate_manifest(xml_bytes, tmp_path)
        assert not is_valid


# ---------------------------------------------------------------------------
# C. Unit tests for submit_manifest
# ---------------------------------------------------------------------------

class TestSubmitManifest:
    """Unit tests for submit_manifest() — calls client.submit.xml and converts the receipt."""

    @staticmethod
    def _make_receipt(success: bool = True, accession: str = "ERS999", alias: str = "s1") -> MagicMock:
        acc = MagicMock()
        acc.alias = alias
        acc.accession = accession
        acc.status = "PRIVATE"
        acc.hold_until_date = ""
        acc.external_accession = ""
        acc.external_type = ""
        receipt = MagicMock()
        receipt.success = success
        receipt.accessions = [acc]
        receipt.messages = []
        receipt.errors = []
        return receipt

    def test_successful_submission_returns_accessions(self) -> None:
        client = _make_client()
        client.submit.xml.return_value = self._make_receipt(success=True)
        success, accessions, messages = submit_manifest(b"<xml/>", client)
        assert success is True
        assert accessions[0]["accession"] == "ERS999"

    def test_failed_submission_returns_false(self) -> None:
        client = _make_client()
        client.submit.xml.return_value = self._make_receipt(success=False)
        success, accessions, _ = submit_manifest(b"<xml/>", client)
        assert success is False

    def test_http_error_propagates(self) -> None:
        client = _make_client()
        err = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500))
        client.submit.xml.side_effect = err
        with pytest.raises(httpx.HTTPStatusError):
            submit_manifest(b"<xml/>", client)


# ---------------------------------------------------------------------------
# D. CLI integration tests
# ---------------------------------------------------------------------------

def _make_sample_json(sample: dict[str, Any]) -> str:
    return json.dumps({"samples": [sample]})


def _extract_json(output: str) -> dict[str, Any]:
    """Extract the last top-level JSON object from mixed CLI output."""
    depth, end, start = 0, -1, -1
    for i in range(len(output) - 1, -1, -1):
        ch = output[i]
        if ch == "}":
            if depth == 0:
                end = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                start = i
                break
    if start == -1 or end == -1:
        raise ValueError(f"No JSON found in output: {output[:200]!r}")
    return json.loads(output[start:end + 1])


_REAL_XSD_DIR = str(Path(__file__).parent.parent / "src" / "ena_submission_toolkit" / "assets" / "ena_schema")


class TestMainCli:
    """CLI integration tests via typer.testing.CliRunner."""

    _CRED = "ena_submission_toolkit.submit_sample.common.get_credentials"
    _SUBMIT = "ena_submission_toolkit.submit_sample.submit_manifest"

    def _base_args(self) -> list[str]:
        return ["--xsd", _REAL_XSD_DIR]

    def _invoke(self, runner: CliRunner, args: list[str], filename: str, content: str) -> Any:
        with runner.isolated_filesystem():
            Path(filename).write_text(content)
            return runner.invoke(
                app, ["--input", filename] + self._base_args() + args,
                catch_exceptions=False,
            )

    def test_exits_0_and_submits(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        mock_acc = [{"alias": "test-sample-001", "accession": "ERS00001", "status": "PRIVATE",
                     "holdUntilDate": "", "external_accession": "", "external_type": ""}]
        with patch(self._CRED, return_value=("Webin-12345", "pass")), \
             patch(self._SUBMIT, return_value=(True, mock_acc, [])):
            result = self._invoke(runner, [], "samples.json", _make_sample_json(basic_sample))
        assert result.exit_code == 0, result.output
        assert "submitted" in _extract_json(result.output)

    def test_duplicate_detected_without_force_skips_submission(
        self, runner: CliRunner, basic_sample: dict[str, Any]
    ) -> None:
        existing = SampleReport(
            title=basic_sample["SAMPLE_TITLE"], alias=basic_sample["alias"],
            accession="ERS55555", secondary_accession="SAMEA55555", status="PRIVATE",
        )
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch("ena_submission_toolkit.submit_sample.common.WebinClient") as MockClient,
            ):
                MockClient.return_value.reports.list_samples.return_value = [existing]
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args() + ["--check-for-duplicates"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        data = _extract_json(result.output)
        assert len(data["duplicates"]) == 1
        assert data["duplicates"][0]["existing_accession"] == "ERS55555"
        assert data["submitted"] == []

    def test_force_flag_with_duplicate_triggers_modify(
        self, runner: CliRunner, basic_sample: dict[str, Any]
    ) -> None:
        existing = SampleReport(
            title=basic_sample["SAMPLE_TITLE"], alias=basic_sample["alias"],
            accession="ERS66666", secondary_accession="", status="PRIVATE",
        )
        mock_acc = [{"alias": "test-sample-001", "accession": "ERS66666", "status": "PRIVATE",
                     "holdUntilDate": "", "external_accession": "", "external_type": ""}]
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch("ena_submission_toolkit.submit_sample.common.WebinClient") as MockClient,
                patch(self._SUBMIT, return_value=(True, mock_acc, [])),
            ):
                MockClient.return_value.reports.list_samples.return_value = [existing]
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args() + ["--force", "--check-for-duplicates"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        data = _extract_json(result.output)
        assert len(data["modified"]) == 1
        assert data["modified"][0]["accession"] == "ERS66666"

    def test_failed_submission_exits_1(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        http_err = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500))
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._SUBMIT, side_effect=http_err),
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args(),
                    catch_exceptions=False,
                )
        assert result.exit_code == 1

    def test_test_flag_creates_test_config(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        mock_acc = [{"alias": "test-sample-001", "accession": "ERS00001", "status": "PRIVATE",
                     "holdUntilDate": "", "external_accession": "", "external_type": ""}]
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._SUBMIT, return_value=(True, mock_acc, [])),
                patch("ena_submission_toolkit.submit_sample.common.WebinConfig", wraps=WebinConfig) as MockConfig,
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args() + ["--test"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        assert MockConfig.call_args.kwargs.get("test") is True

    def test_no_test_flag_creates_prod_config(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        mock_acc = [{"alias": "test-sample-001", "accession": "ERS00002", "status": "PRIVATE",
                     "holdUntilDate": "", "external_accession": "", "external_type": ""}]
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            with (
                patch(self._CRED, return_value=("Webin-12345", "pass")),
                patch(self._SUBMIT, return_value=(True, mock_acc, [])),
                patch("ena_submission_toolkit.submit_sample.common.WebinConfig", wraps=WebinConfig) as MockConfig,
            ):
                result = runner.invoke(
                    app, ["--input", "samples.json"] + self._base_args(),
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, result.output
        assert MockConfig.call_args.kwargs.get("test") is False

    def test_output_flag_writes_results_to_file(self, runner: CliRunner, basic_sample: dict[str, Any]) -> None:
        with runner.isolated_filesystem():
            Path("samples.json").write_text(_make_sample_json(basic_sample))
            with patch(self._CRED, return_value=("Webin-12345", "pass")), \
                 patch(self._SUBMIT, return_value=(True, [], [])):
                result = runner.invoke(
                    app,
                    ["--input", "samples.json"] + self._base_args() + ["--output", "results.json"],
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, result.output
            assert Path("results.json").exists()
            data = json.loads(Path("results.json").read_text())
            assert "submitted" in data


# ---------------------------------------------------------------------------
# Parametrized coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hold_until,expect_hold", [("2027-03-01", True), ("2028-12-31", True), (None, False)])
def test_hold_until_element_conditional(hold_until: str | None, expect_hold: bool) -> None:
    sample = {"alias": "hold-test", "TAXON_ID": 9606, "SAMPLE_TITLE": "Hold Date Test"}
    root = build_submission_xml([sample], hold_until=hold_until)
    hold_el = root.find(".//HOLD")
    if expect_hold:
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == hold_until
    else:
        assert hold_el is None


@pytest.mark.parametrize("action", ["ADD", "MODIFY"])
def test_submission_action_element_present(action: str) -> None:
    sample = {"alias": "action-test", "TAXON_ID": 9606, "SAMPLE_TITLE": "Action Test"}
    root = build_submission_xml([sample], action=action)
    xml_str = ET.tostring(root, encoding="unicode")
    assert f"<{action}" in xml_str or f"<{action}/>" in xml_str
    opposite = "MODIFY" if action == "ADD" else "ADD"
    assert f"<{opposite}" not in xml_str
