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


class FakeClient:
    """Just enough WebinClient to drive records.* without touching the network."""

    def __init__(self, rows: list[FakeRow] | None = None, xml: bytes | Exception = SAMPLE_XML) -> None:
        self._rows = rows or []
        self._xml = xml
        self.submitted: list[bytes] = []
        self.actions: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        outer = self

        class Reports:
            def __getattr__(self, _name: str):
                return lambda **_kwargs: outer._rows

        class Browser:
            def xml(self, _accession: str) -> bytes:
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

    def test_files_are_never_status_filtered(self, fake_client):
        fake_client._rows = [FakeRow(filename="f.fastq.gz")]
        assert len(records.list_records(CREDS, "files", test=True, status="PRIVATE")) == 1


class TestEditableColumns:
    def test_known_and_unknown_entities(self):
        assert records.editable_columns("samples") == ["alias", "title"]
        assert records.editable_columns("runs") == ["alias"]
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
