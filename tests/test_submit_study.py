#!/usr/bin/env python3
"""Tests for submit_study.py — ENA study submission pipeline.

Covers:
    A. Unit tests for build_submission_xml and _add_project_element
    B. Unit tests for build_manifest
    C. Unit tests for validate_manifest
    D. CLI integration tests for main() using typer.testing.CliRunner

Usage:
    pytest test_submit_study.py -v
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
from ena_api import StudyReport, WebinClient, WebinConfig
from typer.testing import CliRunner


from ena_submission_toolkit.submit_study import (  # noqa: E402
    app,
    build_manifest,
    build_submission_xml,
    submit_manifest,
    validate_manifest,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REAL_XSD_DIR = str(Path(__file__).parent.parent / "src" / "ena_submission_toolkit" / "assets" / "ena_schema")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_study() -> dict[str, Any]:
    return {
        "alias": "test-study-001",
        "STUDY_TITLE": "A Basic Test Study",
        "STUDY_ABSTRACT": "An abstract for the test study.",
        "CENTER_PROJECT_NAME": "My Centre Project",
        "existing_study_type": "Metagenomics",
    }


@pytest.fixture
def metagenomics_assembly_study() -> dict[str, Any]:
    return {
        "alias": "metagenome-assembly-001",
        "STUDY_TITLE": "Primary Metagenome Assembly of Soil Sample",
        "STUDY_ABSTRACT": "Assembly of contigs from metagenome sequencing of soil.",
        "CENTER_PROJECT_NAME": "Soil Metagenome Project",
        "existing_study_type": "Metagenomics",
    }


@pytest.fixture
def mag_genome_study() -> dict[str, Any]:
    return {
        "alias": "mag-genome-001",
        "STUDY_TITLE": "Metagenome-Assembled Genome from Soil Microbiome",
        "STUDY_ABSTRACT": "A high-quality MAG reconstructed from binned metagenome data.",
        "existing_study_type": "Other",
        "new_study_type": "Genome Sequencing",
    }



@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def real_xsd_dir() -> Path:
    return Path(_REAL_XSD_DIR)


# ---------------------------------------------------------------------------
# A. Unit tests for build_submission_xml
# ---------------------------------------------------------------------------


class TestBuildSubmissionXml:

    @staticmethod
    def _to_str(root: ET.Element) -> str:
        return ET.tostring(root, encoding="unicode")

    def test_study_title_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        title_el = root.find(".//TITLE")
        assert title_el is not None
        assert title_el.text == basic_study["STUDY_TITLE"]

    def test_study_abstract_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        desc_el = root.find(".//DESCRIPTION")
        assert desc_el is not None
        assert desc_el.text == basic_study["STUDY_ABSTRACT"]

    def test_alias_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        project_el = root.find(".//PROJECT")
        assert project_el is not None
        assert project_el.get("alias") == basic_study["alias"]

    def test_center_project_name_round_trips(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        name_el = root.find(".//NAME")
        assert name_el is not None
        assert name_el.text == basic_study["CENTER_PROJECT_NAME"]

    def test_submission_project_present(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        sp_el = root.find(".//SUBMISSION_PROJECT")
        assert sp_el is not None
        assert sp_el.find("SEQUENCING_PROJECT") is not None

    def test_existing_study_type_emitted_as_project_attribute(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        xml_str = self._to_str(root)
        assert "existing_study_type" in xml_str
        assert basic_study["existing_study_type"] in xml_str

    def test_new_study_type_absent_when_not_other(self, basic_study: dict[str, Any]) -> None:
        study = dict(basic_study)
        study["new_study_type"] = "Genome Sequencing"
        root = build_submission_xml([study])
        assert "new_study_type" not in self._to_str(root)

    def test_new_study_type_present_when_existing_is_other(self, mag_genome_study: dict[str, Any]) -> None:
        root = build_submission_xml([mag_genome_study])
        tags = [el.text for el in root.findall(".//PROJECT_ATTRIBUTE/TAG") if el.text]
        values = [el.text for el in root.findall(".//PROJECT_ATTRIBUTE/VALUE") if el.text]
        assert "existing_study_type" in tags
        assert "new_study_type" in tags
        assert "Other" in values
        assert "Genome Sequencing" in values

    def test_no_project_attributes_when_no_study_type(self) -> None:
        study = {"alias": "no-type", "STUDY_TITLE": "No Type Study"}
        root = build_submission_xml([study])
        assert root.find(".//PROJECT_ATTRIBUTES") is None

    def test_hold_until_present_in_submission(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study], hold_until="2028-06-15")
        hold_el = root.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-06-15"

    def test_hold_until_absent_when_not_provided(self, basic_study: dict[str, Any]) -> None:
        root = build_submission_xml([basic_study])
        assert root.find(".//HOLD") is None

    def test_modify_action_produces_modify_element(self, basic_study: dict[str, Any]) -> None:
        xml_str = self._to_str(build_submission_xml([basic_study], action="MODIFY"))
        assert "<MODIFY" in xml_str or "<MODIFY/>" in xml_str

    def test_add_action_produces_add_element(self, basic_study: dict[str, Any]) -> None:
        xml_str = self._to_str(build_submission_xml([basic_study]))
        assert "<ADD" in xml_str or "<ADD/>" in xml_str

    def test_modify_action_does_not_produce_add(self, basic_study: dict[str, Any]) -> None:
        xml_str = self._to_str(build_submission_xml([basic_study], action="MODIFY"))
        assert "<ADD" not in xml_str and "<ADD/>" not in xml_str

    def test_multiple_studies_produce_multiple_project_elements(
        self, basic_study: dict[str, Any], metagenomics_assembly_study: dict[str, Any]
    ) -> None:
        root = build_submission_xml([basic_study, metagenomics_assembly_study])
        assert len(root.findall(".//PROJECT")) == 2

    def test_alias_derived_from_title_when_absent(self) -> None:
        study = {"STUDY_TITLE": "My Derived Title"}
        root = build_submission_xml([study])
        project_el = root.find(".//PROJECT")
        assert project_el is not None
        alias = project_el.get("alias", "")
        assert "_" in alias or alias == "My_Derived_Title"[:50]

    def test_mag_genome_study_has_both_project_attributes(self, mag_genome_study: dict[str, Any]) -> None:
        root = build_submission_xml([mag_genome_study])
        attr_els = root.findall(".//PROJECT_ATTRIBUTE")
        assert len(attr_els) == 2
        pairs = {
            (attr_el.find("TAG").text or ""): (attr_el.find("VALUE").text or "")
            for attr_el in attr_els
            if attr_el.find("TAG") is not None and attr_el.find("VALUE") is not None
        }
        assert pairs.get("existing_study_type") == "Other"
        assert pairs.get("new_study_type") == "Genome Sequencing"


# ---------------------------------------------------------------------------
# B. Unit tests for build_manifest
# ---------------------------------------------------------------------------


class TestBuildManifest:

    def test_returns_bytes(self, basic_study: dict[str, Any]) -> None:
        result = build_manifest([basic_study])
        assert isinstance(result, bytes)

    def test_hold_until_passed_through(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study], hold_until="2028-06-15")
        tree = ET.fromstring(xml_bytes)
        hold_el = tree.find(".//HOLD")
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == "2028-06-15"

    def test_no_hold_when_not_provided(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study])
        tree = ET.fromstring(xml_bytes)
        assert tree.find(".//HOLD") is None

    def test_modify_action_passed_through(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study], action="MODIFY")
        xml_str = xml_bytes.decode("utf-8")
        assert "<MODIFY" in xml_str or "<MODIFY/>" in xml_str

    def test_add_action_is_default(self, basic_study: dict[str, Any]) -> None:
        xml_bytes = build_manifest([basic_study])
        xml_str = xml_bytes.decode("utf-8")
        assert "<ADD" in xml_str or "<ADD/>" in xml_str


# ---------------------------------------------------------------------------
# C. Unit tests for validate_manifest
# ---------------------------------------------------------------------------


def _valid_study_xml_bytes(alias: str = "study-1", title: str = "Test Study") -> bytes:
    xml_str = dedent(f"""\
        <?xml version='1.0' encoding='UTF-8'?>
        <WEBIN>
          <PROJECT_SET>
            <PROJECT alias="{alias}">
              <TITLE>{title}</TITLE>
              <SUBMISSION_PROJECT>
                <SEQUENCING_PROJECT/>
              </SUBMISSION_PROJECT>
            </PROJECT>
          </PROJECT_SET>
        </WEBIN>
    """)
    return xml_str.encode("utf-8")


class TestValidateManifest:

    def test_valid_xml_passes(self, real_xsd_dir: Path) -> None:
        is_valid, messages = validate_manifest(_valid_study_xml_bytes(), real_xsd_dir)
        assert is_valid, f"Expected valid; messages: {messages}"

    def test_missing_project_set_fails(self, real_xsd_dir: Path) -> None:
        xml_bytes = b"<?xml version='1.0'?><WEBIN/>"
        is_valid, messages = validate_manifest(xml_bytes, real_xsd_dir)
        assert not is_valid

    def test_missing_title_fails(self, real_xsd_dir: Path) -> None:
        xml_str = dedent("""\
            <?xml version='1.0' encoding='UTF-8'?>
            <WEBIN>
              <PROJECT_SET>
                <PROJECT alias="no-title">
                  <SUBMISSION_PROJECT><SEQUENCING_PROJECT/></SUBMISSION_PROJECT>
                </PROJECT>
              </PROJECT_SET>
            </WEBIN>
        """)
        is_valid, messages = validate_manifest(xml_str.encode(), real_xsd_dir)
        assert not is_valid

    def test_malformed_xml_fails_with_fallback(self, tmp_path: Path) -> None:
        bad_xml = b"<WEBIN><PROJECT_SET><PROJECT alias='x'><TITLE>Unclosed"
        is_valid, _ = validate_manifest(bad_xml, tmp_path)
        assert not is_valid

    def test_returns_tuple_of_bool_and_list(self, real_xsd_dir: Path) -> None:
        result = validate_manifest(_valid_study_xml_bytes(), real_xsd_dir)
        assert isinstance(result, tuple) and len(result) == 2
        is_valid, messages = result
        assert isinstance(is_valid, bool)
        assert isinstance(messages, list)

    def test_missing_submission_project_fails(self, real_xsd_dir: Path) -> None:
        xml_str = dedent("""\
            <?xml version='1.0' encoding='UTF-8'?>
            <WEBIN>
              <PROJECT_SET>
                <PROJECT alias="no-sp">
                  <TITLE>Some Title</TITLE>
                </PROJECT>
              </PROJECT_SET>
            </WEBIN>
        """)
        is_valid, messages = validate_manifest(xml_str.encode(), real_xsd_dir)
        assert not is_valid
        assert any("SUBMISSION_PROJECT" in m for m in messages)

    def test_empty_project_set_fails_with_fallback(self, tmp_path: Path) -> None:
        xml_bytes = b"<?xml version='1.0'?><WEBIN><PROJECT_SET/></WEBIN>"
        is_valid, _ = validate_manifest(xml_bytes, tmp_path)
        assert not is_valid


# ---------------------------------------------------------------------------
# D. CLI integration tests for main()
# ---------------------------------------------------------------------------


def _extract_json(output: str) -> dict[str, Any]:
    """Extract the last JSON object from mixed CLI output."""
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
        raise ValueError(f"No JSON object found in output: {output[:200]!r}")
    return json.loads(output[start:end + 1])


def _make_study_json(study: dict[str, Any]) -> str:
    return json.dumps({"studies": [study]})


@pytest.fixture
def minimal_study() -> dict[str, Any]:
    return {
        "alias": "cli-metagenomics-001",
        "STUDY_TITLE": "CLI Metagenomics Test Study",
        "STUDY_ABSTRACT": "Abstract for CLI test.",
        "existing_study_type": "Metagenomics",
    }


_MOCK_ACC = [{"alias": "a", "accession": "PRJEB1", "status": "PRIVATE",
              "holdUntilDate": "", "external_accession": "", "external_type": ""}]


class TestMainCli:
    _CRED_TARGET = "ena_submission_toolkit.submit_study.common.get_credentials"
    _SUBMIT_TARGET = "ena_submission_toolkit.submit_study.submit_manifest"

    def _invoke(self, runner: CliRunner, args: list[str], input_filename: str, input_content: str) -> Any:
        with runner.isolated_filesystem():
            Path(input_filename).write_text(input_content)
            base_args = ["--xsd", _REAL_XSD_DIR]
            result = runner.invoke(
                app,
                ["--input", input_filename] + base_args + args,
                catch_exceptions=False,
            )
        return result

    def test_exits_0_and_submits(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
             patch(self._SUBMIT_TARGET, return_value=(True, _MOCK_ACC, [])):
            result = self._invoke(runner, [], "studies.json", content)
        assert result.exit_code == 0, f"output: {result.output}"
        assert "submitted" in _extract_json(result.output)

    def test_duplicate_detected_without_force_skips_submission(
        self, runner: CliRunner, minimal_study: dict[str, Any]
    ) -> None:
        existing = StudyReport(
            title=minimal_study["STUDY_TITLE"],
            alias=minimal_study["alias"],
            accession="PRJEB55555",
            secondary_accession="ERP055555",
            status="PRIVATE",
        )
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch("ena_submission_toolkit.submit_study.common.WebinClient") as MockClient:
                MockClient.return_value.reports.list_projects.return_value = [existing]
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--xsd", _REAL_XSD_DIR, "--check-for-duplicates"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, f"output: {result.output}"
        data = _extract_json(result.output)
        assert len(data["duplicates"]) == 1
        assert data["duplicates"][0]["existing_accession"] == "PRJEB55555"
        assert data["submitted"] == []

    def test_force_flag_with_duplicate_triggers_modify(
        self, runner: CliRunner, minimal_study: dict[str, Any]
    ) -> None:
        existing = StudyReport(
            title=minimal_study["STUDY_TITLE"],
            alias=minimal_study["alias"],
            accession="PRJEB66666",
            secondary_accession="ERP066666",
            status="PRIVATE",
        )
        mock_accessions = [{"alias": "cli-metagenomics-001", "accession": "PRJEB66666",
                            "status": "PRIVATE", "holdUntilDate": "",
                            "external_accession": "", "external_type": ""}]
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch("ena_submission_toolkit.submit_study.common.WebinClient") as MockClient, \
                 patch(self._SUBMIT_TARGET, return_value=(True, mock_accessions, [])):
                MockClient.return_value.reports.list_projects.return_value = [existing]
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--force", "--xsd", _REAL_XSD_DIR, "--check-for-duplicates"],
                    catch_exceptions=False,
                )
        assert result.exit_code == 0, f"output: {result.output}"
        data = _extract_json(result.output)
        assert len(data["modified"]) == 1
        assert data["modified"][0]["accession"] == "PRJEB66666"

    def test_failed_submission_exits_1(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        http_error = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500))
        with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
             patch(self._SUBMIT_TARGET, side_effect=http_error):
            result = self._invoke(runner, [], "studies.json", content)
        assert result.exit_code == 1

    def test_test_flag_creates_test_config(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        with (
            patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")),
            patch(self._SUBMIT_TARGET, return_value=(True, _MOCK_ACC, [])),
            patch("ena_submission_toolkit.submit_study.common.WebinConfig", wraps=WebinConfig) as MockConfig,
        ):
            result = self._invoke(runner, ["--test"], "studies.json", content)
        assert result.exit_code == 0, f"output: {result.output}"
        assert MockConfig.call_args.kwargs.get("test") is True

    def test_no_test_flag_creates_prod_config(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        with (
            patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")),
            patch(self._SUBMIT_TARGET, return_value=(True, _MOCK_ACC, [])),
            patch("ena_submission_toolkit.submit_study.common.WebinConfig", wraps=WebinConfig) as MockConfig,
        ):
            result = self._invoke(runner, [], "studies.json", content)
        assert result.exit_code == 0, f"output: {result.output}"
        assert MockConfig.call_args.kwargs.get("test") is False

    def test_output_flag_writes_results_to_file(self, runner: CliRunner, minimal_study: dict[str, Any]) -> None:
        content = _make_study_json(minimal_study)
        with runner.isolated_filesystem():
            Path("studies.json").write_text(content)
            with patch(self._CRED_TARGET, return_value=("Webin-12345", "pass")), \
                 patch(self._SUBMIT_TARGET, return_value=(True, _MOCK_ACC, [])):
                result = runner.invoke(
                    app,
                    ["--input", "studies.json", "--output", "results.json", "--xsd", _REAL_XSD_DIR],
                    catch_exceptions=False,
                )
            assert result.exit_code == 0, f"output: {result.output}"
            assert Path("results.json").exists()
            data = json.loads(Path("results.json").read_text())
            assert "submitted" in data


# ---------------------------------------------------------------------------
# Parametrized tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "study_type,new_type,expect_new_type",
    [
        ("Metagenomics", None, False),
        ("RNASeq", None, False),
        ("Other", "Genome Sequencing", True),
        ("Other", "Transcriptome Analysis", True),
        ("Other", None, False),
    ],
)
def test_project_attribute_new_study_type_conditional(
    study_type: str, new_type: str | None, expect_new_type: bool
) -> None:
    study: dict[str, Any] = {
        "alias": "param-test",
        "STUDY_TITLE": "Parametrized Study",
        "existing_study_type": study_type,
    }
    if new_type is not None:
        study["new_study_type"] = new_type
    root = build_submission_xml([study])
    tags = [el.text for el in root.findall(".//PROJECT_ATTRIBUTE/TAG") if el.text]
    if expect_new_type:
        assert "new_study_type" in tags
    else:
        assert "new_study_type" not in tags


@pytest.mark.parametrize(
    "hold_until,expect_hold",
    [("2027-03-01", True), ("2028-12-31", True), (None, False)],
)
def test_hold_until_element_conditional(hold_until: str | None, expect_hold: bool) -> None:
    study = {"alias": "hold-test", "STUDY_TITLE": "Hold Date Test"}
    root = build_submission_xml([study], hold_until=hold_until)
    hold_el = root.find(".//HOLD")
    if expect_hold:
        assert hold_el is not None
        assert hold_el.get("HoldUntilDate") == hold_until
    else:
        assert hold_el is None


@pytest.mark.parametrize("action", ["ADD", "MODIFY"])
def test_submission_action_element_present(action: str) -> None:
    study = {"alias": "action-test", "STUDY_TITLE": "Action Test"}
    root = build_submission_xml([study], action=action)
    xml_str = ET.tostring(root, encoding="unicode")
    assert f"<{action}" in xml_str or f"<{action}/>" in xml_str
    opposite = "MODIFY" if action == "ADD" else "ADD"
    assert f"<{opposite}" not in xml_str
