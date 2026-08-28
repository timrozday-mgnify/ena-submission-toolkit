"""Tests for ena_submission_toolkit.portal (the ENA Portal API behaviour)."""

from __future__ import annotations

from typing import Any

import pytest
from ena_api_handler.models import ENAPortalResultType

from ena_submission_toolkit import portal


class FakeModel:
    """Stands in for one of ena-api-handler's generated result models."""

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self._row)


class FakeENAClient:
    """Records the searches made, answers with whatever the test queued."""

    calls: list[dict[str, Any]] = []
    answers: list[list[dict[str, Any]]] = []

    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        type(self).calls.append({"auth": (username, password)})

    def __enter__(self) -> FakeENAClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def search(self, *, result: Any, query: Any, **kwargs: Any) -> list[FakeModel]:
        type(self).calls.append(
            {"result": result, "query": query.to_query_string(), **kwargs}
        )
        rows = type(self).answers.pop(0) if type(self).answers else []
        return [FakeModel(row) for row in rows]


@pytest.fixture
def fake_ena(monkeypatch):
    FakeENAClient.calls = []
    FakeENAClient.answers = []
    monkeypatch.setattr(portal, "ENAClient", FakeENAClient)
    return FakeENAClient


def searches(fake) -> list[dict[str, Any]]:
    return [call for call in fake.calls if "query" in call]


class TestFieldsForAccessions:
    def test_keys_rows_by_every_identifying_accession(self, fake_ena):
        fake_ena.answers = [
            [{"study_accession": "PRJEB1", "secondary_study_accession": "ERP1", "study_title": "Alpha"}]
        ]
        # The Reports API gave us the ERP form; the Portal answers with both,
        # so a caller holding either accession finds the row.
        found = portal.fields_for_accessions("studies", ["ERP1"])
        assert set(found) == {"PRJEB1", "ERP1"}
        assert found["ERP1"]["study_title"] == "Alpha"

    def test_asks_about_both_accession_forms(self, fake_ena):
        portal.fields_for_accessions("samples", ["ERS9000001"])
        query = searches(fake_ena)[0]["query"]
        assert 'sample_accession="ERS9000001"' in query
        assert 'secondary_sample_accession="ERS9000001"' in query

    def test_asks_for_every_field(self, fake_ena):
        portal.fields_for_accessions("runs", ["ERR1"])
        call = searches(fake_ena)[0]
        assert call["fields"] == ["all"]
        assert call["limit"] == 0
        assert call["result"] is ENAPortalResultType.READ_RUN

    def test_chunks_large_accession_lists(self, fake_ena):
        portal.fields_for_accessions("runs", [f"ERR{i}" for i in range(120)])
        assert len(searches(fake_ena)) == 3

    def test_passes_credentials_through(self, fake_ena):
        portal.fields_for_accessions("runs", ["ERR1"], username="Webin-1", password="pw")
        assert fake_ena.calls[0]["auth"] == ("Webin-1", "pw")

    def test_no_credentials_is_none_not_empty_string(self, fake_ena):
        portal.fields_for_accessions("runs", ["ERR1"])
        assert fake_ena.calls[0]["auth"] == (None, None)

    def test_renames_the_blank_booleans_back(self, fake_ena):
        # The handler types these as bool but ENA sends ""; they travel under
        # an alias to survive validation and must arrive under their own name.
        fake_ena.answers = [[{"run_accession": "ERR1", "environmental_sample_raw": "", "germline_raw": ""}]]
        row = portal.fields_for_accessions("runs", ["ERR1"])["ERR1"]
        assert row["environmental_sample"] == ""
        assert "environmental_sample_raw" not in row

    def test_no_request_for_an_entity_with_no_portal_result(self, fake_ena):
        assert portal.fields_for_accessions("files", ["ERR1"]) == {}
        assert searches(fake_ena) == []

    def test_no_request_without_accessions(self, fake_ena):
        assert portal.fields_for_accessions("runs", []) == {}
        assert searches(fake_ena) == []

    def test_drops_accessions_that_could_escape_the_query(self, fake_ena):
        assert portal.fields_for_accessions("runs", ['ERR1" OR x="y']) == {}
        assert searches(fake_ena) == []


class TestSearchPublic:
    def test_queries_the_field_the_accession_belongs_to(self, fake_ena):
        fake_ena.answers = [[{"run_accession": "ERR1", "instrument_model": "NovaSeq"}]]
        rows = portal.search_public("runs", "PRJEB1787")
        assert rows == [{"run_accession": "ERR1", "instrument_model": "NovaSeq"}]
        assert searches(fake_ena)[0]["query"] == 'study_accession="PRJEB1787"'

    def test_translates_an_accession_form_the_result_cannot_match(self, fake_ena):
        # `sample` can be filtered by study_accession but not by its secondary
        # form, so the PRJ form is looked up first and queried instead.
        fake_ena.answers = [
            [{"study_accession": "PRJEB12343", "secondary_study_accession": "ERP013810"}],
            [{"sample_accession": "SAMEA1"}],
        ]
        rows = portal.search_public("samples", "ERP013810")
        assert rows == [{"sample_accession": "SAMEA1"}]
        assert searches(fake_ena)[1]["query"] == 'study_accession="PRJEB12343"'

    def test_no_extra_lookup_when_the_form_already_matches(self, fake_ena):
        portal.search_public("runs", "PRJEB1787")
        assert len(searches(fake_ena)) == 1

    def test_rejects_an_entity_with_no_portal_result(self, fake_ena):
        with pytest.raises(ValueError, match="No public search for 'files'"):
            portal.search_public("files", "PRJEB1787")

    def test_rejects_an_implausible_accession(self, fake_ena):
        with pytest.raises(ValueError, match="Not a plausible accession"):
            portal.search_public("runs", '../../etc/passwd')

    def test_rejects_a_relationship_ena_cannot_answer(self, fake_ena):
        # A study row carries no run accession, so "which studies contain
        # ERR1" is not a question the Portal can be asked.
        with pytest.raises(ValueError, match="no field to match it on"):
            portal.search_public("studies", "ERR1160846")

    def test_works_without_credentials(self, fake_ena):
        portal.search_public("runs", "PRJEB1787")
        assert fake_ena.calls[0]["auth"] == (None, None)
