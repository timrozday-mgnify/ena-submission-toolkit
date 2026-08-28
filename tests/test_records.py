#!/usr/bin/env python3
"""Tests for ena_submission_toolkit.records — account record browsing/editing."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from lxml import etree

from ena_submission_toolkit import records

CREDS = records.Credentials(username="Webin-1", password="secret")

SAMPLE_XML = b"""<?xml version="1.0"?>
<SAMPLE_SET><SAMPLE alias="old-alias" accession="ERS9000001">
  <TITLE>Old title</TITLE>
  <SAMPLE_ATTRIBUTES><SAMPLE_ATTRIBUTE><TAG>t</TAG><VALUE>v</VALUE></SAMPLE_ATTRIBUTE></SAMPLE_ATTRIBUTES>
</SAMPLE></SAMPLE_SET>"""


class FakeReceipt:
    def __init__(self, success: bool = True) -> None:
        self.success = success
        self.messages = ["INFO: ok"]
        self.warnings: list[str] = []
        self.errors: list[str] = []


class FakeRow:
    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def model_dump(self) -> dict[str, Any]:
        return dict(self._fields)


class FakeProcess:
    """One row of the run-processing report."""

    def __init__(self, run_accession: str, status: str, date: str = "", error: str = "") -> None:
        self.run_accession = run_accession
        self.process_status = status
        self.process_date = date
        self.error_message = error


class FakeClient:
    """Just enough WebinClient to drive records.* without touching the network."""

    def __init__(self, rows: list[FakeRow] | None = None, xml: bytes | Exception = SAMPLE_XML) -> None:
        self._rows = rows or []
        #: Per-report-method overrides, for the tests that need the lineage
        #: reports to differ from the entity being listed.
        self.rows_by_method: dict[str, list[Any]] = {}
        self._xml = xml
        self._processes: list[FakeProcess] = []
        self.submitted: list[bytes] = []
        self.actions: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.batches: list[list[str]] = []
        outer = self

        class Reports:
            def list_run_processes(self, **_kwargs: Any) -> list[FakeProcess]:
                return outer._processes

            def __getattr__(self, name: str):
                return lambda **_kwargs: outer.rows_by_method.get(name, outer._rows)

        class Browser:
            def xml(self, _accession: str) -> bytes:
                if isinstance(outer._xml, Exception):
                    raise outer._xml
                return outer._xml

            def xml_many(self, accessions: list[str]) -> bytes:
                outer.batches.append(list(accessions))
                if isinstance(outer._xml, Exception):
                    raise outer._xml
                return outer._xml

        class Submit:
            def xml(self, document: bytes) -> FakeReceipt:
                outer.submitted.append(document)
                return FakeReceipt()

            def __getattr__(self, name: str):
                def action(*args: Any, **kwargs: Any) -> FakeReceipt:
                    outer.actions.append((name, args, kwargs))
                    return FakeReceipt()

                return action

        self.reports, self.browser, self.submit = Reports(), Browser(), Submit()


@pytest.fixture
def fake_client(monkeypatch):
    """Install a FakeClient; the test fills in its rows/xml before calling."""
    client = FakeClient()

    @contextmanager
    def fake_webin_client(_creds, _test):
        yield client

    monkeypatch.setattr(records, "webin_client", fake_webin_client)
    return client


class TestListRecords:
    def test_rejects_unknown_entity(self, fake_client):
        with pytest.raises(ValueError, match="Unknown entity"):
            records.list_records(CREDS, "widgets", test=True)

    def test_returns_dicts_including_extra_fields(self, fake_client):
        fake_client._rows = [FakeRow(accession="ERS1", status="PRIVATE", somethingNew="kept")]
        rows = records.list_records(CREDS, "samples", test=True)
        assert rows == [{"accession": "ERS1", "status": "PRIVATE", "somethingNew": "kept"}]

    def test_filters_by_status(self, fake_client):
        fake_client._rows = [FakeRow(accession="A", status="PRIVATE"), FakeRow(accession="B", status="CANCELLED")]
        assert [r["accession"] for r in records.list_records(CREDS, "samples", test=True, status="private")] == ["A"]
        assert len(records.list_records(CREDS, "samples", test=True, status="all")) == 2

    def test_full_fields_merges_the_portal_answer(self, fake_client, monkeypatch):
        calls: list[tuple] = []

        def fake_fields(entity, accessions, *, username="", password=""):
            calls.append((entity, list(accessions), username))
            return {"ERR1": {"run_accession": "ERR1", "instrument_model": "NovaSeq", "status": "PUBLIC"}}

        monkeypatch.setattr(records.portal, "fields_for_accessions", fake_fields)
        fake_client._rows = [FakeRow(accession="ERR1", status="PRIVATE")]
        rows = records.list_records(CREDS, "runs", test=False, full_fields=True)
        assert rows[0]["instrument_model"] == "NovaSeq"
        # The report's own value survives a collision.
        assert rows[0]["status"] == "PRIVATE"
        assert calls == [("runs", ["ERR1"], CREDS.username)]

    def test_full_fields_looks_up_both_accession_forms(self, fake_client, monkeypatch):
        seen: list[list[str]] = []
        monkeypatch.setattr(
            records.portal,
            "fields_for_accessions",
            lambda entity, accessions, **_kw: seen.append(list(accessions)) or {"PRJEB1": {"study_title": "Alpha"}},
        )
        fake_client._rows = [FakeRow(accession="ERP1", secondary_accession="PRJEB1")]
        rows = records.list_records(CREDS, "studies", test=False, full_fields=True)
        assert rows[0]["study_title"] == "Alpha"
        assert seen == [["ERP1", "PRJEB1"]]

    def test_full_fields_is_skipped_on_the_test_environment(self, fake_client, monkeypatch):
        called = []
        monkeypatch.setattr(records.portal, "fields_for_accessions", lambda *a, **k: called.append(a) or {})
        fake_client._rows = [FakeRow(accession="ERR1")]
        records.list_records(CREDS, "runs", test=True, full_fields=True)
        assert called == []

    def test_a_failing_portal_still_returns_the_listing(self, fake_client, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("portal down")

        monkeypatch.setattr(records.portal, "fields_for_accessions", boom)
        fake_client._rows = [FakeRow(accession="ERR1", status="PRIVATE")]
        rows = records.list_records(CREDS, "runs", test=False, full_fields=True)
        assert [r["accession"] for r in rows] == ["ERR1"]

    def test_files_are_never_status_filtered(self, fake_client):
        fake_client._rows = [FakeRow(filename="f.fastq.gz")]
        assert len(records.list_records(CREDS, "files", test=True, status="PRIVATE")) == 1

    def test_search_needs_every_term_somewhere_in_the_row(self, fake_client):
        fake_client._rows = [
            FakeRow(accession="ERS1", title="Gut metagenome, mouse"),
            FakeRow(accession="ERS2", title="Soil metagenome"),
        ]
        found = records.list_records(CREDS, "samples", test=True, search="META mouse")
        assert [r["accession"] for r in found] == ["ERS1"]
        assert records.list_records(CREDS, "samples", test=True, search="nothing here") == []


class FakeLink:
    """One lineage row (experiment/run/analysis) as _link_index reads it."""

    def __init__(
        self,
        accession="",
        study_accession="",
        sample_accession="",
        experiment_accession="",
    ):
        self.accession = accession
        self.secondary_accession = ""
        self.study_accession = study_accession
        self.sample_accession = sample_accession
        self.experiment_accession = experiment_accession

    def model_dump(self) -> dict[str, Any]:
        return {"accession": self.accession}


@pytest.fixture
def linked(fake_client):
    """ERS1 sits in study PRJ1 via experiment ERX1, which has run ERR1. ERS2 is loose."""
    fake_client.rows_by_method = {
        "list_experiments": [FakeLink(accession="ERX1", study_accession="PRJ1", sample_accession="ERS1")],
        "list_runs": [
            FakeLink(
                accession="ERR1",
                experiment_accession="ERX1",
                study_accession="PRJ1",
                sample_accession="ERS1",
            )
        ],
        "list_analyses": [],
        "list_run_processes": [],
    }
    fake_client._rows = [
        FakeRow(accession="ERS1", status="PRIVATE"),
        FakeRow(accession="ERS2", status="PRIVATE"),
    ]
    return fake_client


class TestLinkFilters:
    def test_linked_to_a_study_finds_its_samples(self, linked):
        found = records.list_records(CREDS, "samples", test=True, linked_to="PRJ1")
        assert [r["accession"] for r in found] == ["ERS1"]

    def test_linked_to_a_sample_finds_its_reads(self, linked):
        linked._rows = [
            FakeRow(accession="ERR1", status="PRIVATE"),
            FakeRow(accession="ERR9", status="PRIVATE"),
        ]
        found = records.list_records(CREDS, "runs", test=True, linked_to="ERS1")
        assert [r["accession"] for r in found] == ["ERR1"]

    def test_unlinked_finds_samples_with_no_experiment_or_read(self, linked):
        found = records.list_records(CREDS, "samples", test=True, unlinked=True)
        assert [r["accession"] for r in found] == ["ERS2"]

    def test_unlinked_does_not_count_a_record_as_linked_to_itself(self, linked):
        # ERX1 is in the index because of its own row; it is linked because of
        # PRJ1/ERS1/ERR1, not because it appears there.
        linked.rows_by_method["list_experiments"].append(FakeLink(accession="ERX9"))
        found = records.list_records(CREDS, "experiments", test=True, unlinked=True)
        assert [r["accession"] for r in found] == ["ERX9"]

    def test_no_link_criteria_costs_no_lineage_lookup(self, linked):
        assert len(records.list_records(CREDS, "samples", test=True)) == 2


EXPERIMENT_XML = b"""<?xml version="1.0"?>
<EXPERIMENT_SET>
  <EXPERIMENT alias="exp-a" accession="ERX1">
    <TITLE>Old title</TITLE>
    <DESIGN>
      <DESIGN_DESCRIPTION>Old design</DESIGN_DESCRIPTION>
      <SAMPLE_DESCRIPTOR accession="ERS1"/>
      <LIBRARY_DESCRIPTOR>
        <LIBRARY_NAME>lib-a</LIBRARY_NAME>
        <LIBRARY_STRATEGY>WGS</LIBRARY_STRATEGY>
        <LIBRARY_SOURCE>METAGENOMIC</LIBRARY_SOURCE>
        <LIBRARY_SELECTION>RANDOM</LIBRARY_SELECTION>
        <LIBRARY_LAYOUT><PAIRED/></LIBRARY_LAYOUT>
      </LIBRARY_DESCRIPTOR>
    </DESIGN>
    <PLATFORM><ILLUMINA><INSTRUMENT_MODEL>Illumina MiSeq</INSTRUMENT_MODEL></ILLUMINA></PLATFORM>
  </EXPERIMENT>
</EXPERIMENT_SET>"""

RUN_XML = b"""<?xml version="1.0"?>
<RUN_SET>
  <RUN alias="run-a" accession="ERR1" run_center="EBI">
    <TITLE>Old run title</TITLE>
    <EXPERIMENT_REF accession="ERX1"/>
  </RUN>
</RUN_SET>"""


class TestRunProcessingStatus:
    def test_run_rows_carry_the_processing_report(self, fake_client):
        fake_client._rows = [
            FakeRow(accession="ERR1", status="PRIVATE"),
            FakeRow(accession="ERR2", status="PRIVATE"),
        ]
        fake_client._processes = [
            FakeProcess("ERR1", "COMPLETED", "2026-01-02"),
            FakeProcess("", "IGNORED"),
        ]
        rows = records.list_records(CREDS, "runs", test=True)
        assert rows[0]["process_status"] == "COMPLETED"
        assert rows[0]["process_date"] == "2026-01-02"
        # a run the processing report says nothing about keeps no stale value
        assert "process_status" not in rows[1]

    def test_other_entities_do_not_pay_for_it(self, fake_client):
        fake_client._rows = [FakeRow(accession="ERS1", status="PRIVATE")]
        fake_client._processes = [FakeProcess("ERS1", "COMPLETED")]
        assert "process_status" not in records.list_records(CREDS, "samples", test=True)[0]


class TestReadEditableFields:
    def test_reads_nested_experiment_fields(self, fake_client):
        fake_client._xml = EXPERIMENT_XML
        fields = records.read_editable_fields(CREDS, "experiments", ["ERX1"], test=True)
        assert fields["ERX1"] == {
            "alias": "exp-a",
            "title": "Old title",
            "design_description": "Old design",
            "library_name": "lib-a",
            "library_strategy": "WGS",
            "library_source": "METAGENOMIC",
            "library_selection": "RANDOM",
            "instrument_model": "Illumina MiSeq",
        }

    def test_reads_run_fields_and_omits_absent_ones(self, fake_client):
        fake_client._xml = RUN_XML
        assert records.read_editable_fields(CREDS, "runs", ["ERR1"], test=True)["ERR1"] == {
            "alias": "run-a",
            "title": "Old run title",
            "run_center": "EBI",
        }

    def test_batches_the_browser_requests(self, fake_client):
        fake_client._xml = RUN_XML
        accessions = [f"ERR{n}" for n in range(1, records._XML_BATCH + 3)]
        records.read_editable_fields(CREDS, "runs", accessions, test=True)
        assert [len(batch) for batch in fake_client.batches] == [records._XML_BATCH, 2]

    def test_skips_entities_and_input_with_nothing_to_read(self, fake_client):
        assert records.read_editable_fields(CREDS, "files", ["ERR1"], test=True) == {}
        assert records.read_editable_fields(CREDS, "runs", ["../../etc/passwd"], test=True) == {}
        assert fake_client.batches == []

    def test_a_batch_ena_does_not_hold_does_not_sink_the_rest(self, fake_client):
        fake_client._xml = LookupError("ENA holds no XML")
        assert records.read_editable_fields(CREDS, "runs", ["ERR1"], test=True) == {}


class TestEditableColumns:
    def test_known_and_unknown_entities(self):
        assert records.editable_columns("samples") == ["alias", "title"]
        assert records.editable_columns("runs") == [
            "alias",
            "run_center",
            "run_date",
            "title",
        ]
        assert "library_strategy" in records.editable_columns("experiments")
        assert records.editable_columns("widgets") == []


class TestModifyRecords:
    def test_patches_fetched_xml_and_keeps_the_rest(self, fake_client):
        result = records.modify_records(
            CREDS, "samples", [{"accession": "ERS9000001", "changes": {"title": "New title"}}], test=True
        )
        assert result["success"] is True
        document = etree.fromstring(fake_client.submitted[0])
        assert document.findtext(".//SAMPLE/TITLE") == "New title"
        assert document.find(".//SAMPLE").get("alias") == "old-alias"
        # the fields ENA holds but does not report must survive the round trip
        assert document.findtext(".//SAMPLE_ATTRIBUTE/TAG") == "t"
        assert document.find(".//SUBMISSION/ACTIONS/ACTION/MODIFY") is not None

    def test_unfetchable_record_fails_without_submitting(self, monkeypatch):
        client = FakeClient(xml=LookupError("ENA holds no XML for ERS9000001"))

        @contextmanager
        def fake_webin_client(_creds, _test):
            yield client

        monkeypatch.setattr(records, "webin_client", fake_webin_client)
        result = records.modify_records(
            CREDS, "samples", [{"accession": "ERS9000001", "changes": {"title": "x"}}], test=True
        )
        assert result["success"] is False
        assert client.submitted == []

    def test_rejects_non_editable_field(self, fake_client):
        result = records.modify_records(
            CREDS, "samples", [{"accession": "ERS9000001", "changes": {"status": "PUBLIC"}}], test=True
        )
        assert result["success"] is False
        assert "not editable" in result["results"][0]["messages"][0]
        assert fake_client.submitted == []

    def test_rejects_unmodifiable_entity(self, fake_client):
        with pytest.raises(ValueError, match="cannot be modified"):
            records.modify_records(CREDS, "files", [{"accession": "x", "changes": {}}], test=True)

    def test_result_carries_the_document_it_submitted(self, fake_client):
        result = records.modify_records(
            CREDS, "samples", [{"accession": "ERS9000001", "changes": {"title": "New title"}}], test=True
        )
        entry = result["results"][0]
        assert entry["xml"].encode() == fake_client.submitted[0]
        assert entry["changes"] == {"title": "New title"}
        assert entry["info"] == ["INFO: ok"]


class TestPreviewModifyRecords:
    def test_builds_the_same_document_it_would_submit(self, fake_client):
        change = [{"accession": "ERS9000001", "changes": {"title": "New title"}}]
        preview = records.preview_modify_records(CREDS, "samples", change, test=True)
        assert preview["success"] is True
        assert fake_client.submitted == []  # nothing left the process

        records.modify_records(CREDS, "samples", change, test=True)
        assert preview["results"][0]["xml"].encode() == fake_client.submitted[0]

    def test_a_bad_change_fails_the_manifest_not_the_batch(self, fake_client):
        preview = records.preview_modify_records(
            CREDS,
            "samples",
            [
                {"accession": "ERS9000001", "changes": {"status": "PUBLIC"}},
                {"accession": "ERS9000001", "changes": {"title": "ok"}},
            ],
            test=True,
        )
        assert preview["success"] is False
        assert "not editable" in preview["results"][0]["messages"][0]
        assert preview["results"][0]["xml"] == ""
        assert preview["results"][1]["success"] is True

    def test_rejects_unmodifiable_entity(self, fake_client):
        with pytest.raises(ValueError, match="cannot be modified"):
            records.preview_modify_records(CREDS, "files", [{"accession": "x", "changes": {}}], test=True)


class TestRecordAction:
    def test_runs_the_action(self, fake_client):
        result = records.record_action(CREDS, "ERS9000001", "release", test=True)
        assert result["success"] is True
        assert fake_client.actions == [("release", ("ERS9000001",), {})]

    def test_hold_needs_a_date(self, fake_client):
        with pytest.raises(ValueError, match="hold_until"):
            records.record_action(CREDS, "ERS9000001", "hold", test=True)

    def test_rejects_unknown_action(self, fake_client):
        with pytest.raises(ValueError, match="Unknown action"):
            records.record_action(CREDS, "ERS9000001", "destroy", test=True)

    def test_rejects_implausible_accession(self, fake_client):
        with pytest.raises(ValueError, match="Not a plausible accession"):
            records.record_action(CREDS, "../../etc/passwd", "release", test=True)

    def test_ena_failure_is_a_result_not_an_exception(self, monkeypatch, fake_client):
        def boom(*_a, **_k):
            raise RuntimeError("ENA said no")

        monkeypatch.setattr(fake_client.submit, "release", boom, raising=False)
        result = records.record_action(CREDS, "ERS9000001", "release", test=True)
        assert result == {
            "accession": "ERS9000001",
            "action": "release",
            "success": False,
            "messages": ["ENA said no"],
        }


class TestModifyNestedFields:
    def test_patches_an_experiment_library_field_in_place(self, fake_client):
        fake_client._xml = EXPERIMENT_XML
        result = records.modify_records(
            CREDS,
            "experiments",
            [
                {
                    "accession": "ERX1",
                    "changes": {
                        "library_strategy": "AMPLICON",
                        "instrument_model": "Illumina NovaSeq 6000",
                    },
                }
            ],
            test=True,
        )
        assert result["success"] is True
        document = etree.fromstring(fake_client.submitted[0])
        assert document.findtext(".//LIBRARY_DESCRIPTOR/LIBRARY_STRATEGY") == "AMPLICON"
        assert document.findtext(".//PLATFORM/ILLUMINA/INSTRUMENT_MODEL") == "Illumina NovaSeq 6000"
        # everything the Reports API never returns must survive untouched
        assert document.findtext(".//LIBRARY_DESCRIPTOR/LIBRARY_SOURCE") == "METAGENOMIC"
        assert document.find(".//LIBRARY_LAYOUT/PAIRED") is not None

    def test_patches_a_run_title(self, fake_client):
        fake_client._xml = RUN_XML
        records.modify_records(
            CREDS,
            "runs",
            [{"accession": "ERR1", "changes": {"title": "New run title"}}],
            test=True,
        )
        document = etree.fromstring(fake_client.submitted[0])
        assert document.findtext(".//RUN/TITLE") == "New run title"
        assert document.find(".//RUN/EXPERIMENT_REF").get("accession") == "ERX1"

    def test_a_field_the_xml_has_no_element_for_is_reported_not_invented(self, fake_client):
        fake_client._xml = RUN_XML
        result = records.modify_records(
            CREDS,
            "runs",
            [{"accession": "ERR1", "changes": {"nonsense": "x"}}],
            test=True,
        )
        assert result["success"] is False
        assert "not editable" in result["results"][0]["messages"][0]
