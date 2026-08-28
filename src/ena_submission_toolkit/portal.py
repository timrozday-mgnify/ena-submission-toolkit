"""Public ENA records, and the full field set, via the ENA Portal API.

The Webin Reports API answers exactly one question: *what is registered under
this account?* It answers it for private and public records alike — the axis
is ownership, not release status — but it cannot say anything about a record
someone else submitted, and it returns only the four or five fields needed to
identify one.

The Portal API is the other side: ENA's public search index, keyed by
accession rather than by account, holding every field ENA indexes (around 200
for a run). So it is what this module exists for, in two directions:

:func:`fields_for_accessions`
    Fill out records the Reports API already listed — the extra columns.
:func:`search_public`
    Find records *nobody in this account submitted* — everything public under
    a study accession, say.

Transport is `ena-api-handler <https://github.com/EBI-Metagenomics/ena-api-handler>`_,
not ``ena-api-client``: the Portal API already has a maintained typed client
with generated per-result models, and re-implementing it here would be a
second copy of somebody else's job. What *is* this module's job is the
behaviour on top — which Portal result answers for which entity, how an
accession becomes a query, and how the answers merge into report rows.

Two things to know about the Portal:

* **Production only.** There is no Portal API on ``wwwdev``, so nothing
  submitted to the test environment is in it.
* **Public only, unless authenticated.** Webin Basic auth is what makes an
  account's own unreleased records visible; without it, released data only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Final

from ena_api_handler import ENAClient, ENARawQuery
from ena_api_handler.models import QUERY_MODELS, ENAPortalResultType
from ena_api_handler.types import ENAPortalDataPortal

logger = logging.getLogger(__name__)

#: Entity -> the Portal result that answers for it. ``files`` is absent
#: deliberately: a submitted file is not an indexed object in its own right,
#: its Portal-side columns (``fastq_ftp``, ``submitted_bytes``, ...) live on
#: the run that owns it.
PORTAL_RESULTS: Final[dict[str, ENAPortalResultType]] = {
    "studies": ENAPortalResultType.STUDY,
    "projects": ENAPortalResultType.STUDY,
    "samples": ENAPortalResultType.SAMPLE,
    "runs": ENAPortalResultType.READ_RUN,
    "experiments": ENAPortalResultType.READ_EXPERIMENT,
    "analyses": ENAPortalResultType.ANALYSIS,
}

#: Result -> the fields a record of that result is identified by. ENA gives
#: every object two accessions (``PRJEB…``/``ERP…``, ``SAMEA…``/``ERS…``) and
#: the Reports API is not consistent about which one it hands back, so both
#: are asked about and whichever matches wins.
_IDENTITY_FIELDS: Final[dict[ENAPortalResultType, tuple[str, ...]]] = {
    ENAPortalResultType.STUDY: ("study_accession", "secondary_study_accession"),
    ENAPortalResultType.SAMPLE: ("sample_accession", "secondary_sample_accession"),
    ENAPortalResultType.READ_RUN: ("run_accession",),
    ENAPortalResultType.READ_EXPERIMENT: ("experiment_accession",),
    ENAPortalResultType.ANALYSIS: ("analysis_accession",),
}

#: Accession prefix -> the query fields that accession could match. Used to
#: turn "everything under PRJEB1787" into a query without asking the caller
#: what kind of accession they typed. Longest prefix wins.
_QUERY_FIELDS_BY_PREFIX: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("PRJ", ("study_accession",)),
    ("ERP", ("secondary_study_accession",)),
    ("SRP", ("secondary_study_accession",)),
    ("DRP", ("secondary_study_accession",)),
    ("SAM", ("sample_accession",)),
    ("ERS", ("secondary_sample_accession",)),
    ("SRS", ("secondary_sample_accession",)),
    ("DRS", ("secondary_sample_accession",)),
    ("ERR", ("run_accession",)),
    ("SRR", ("run_accession",)),
    ("DRR", ("run_accession",)),
    ("ERX", ("experiment_accession",)),
    ("SRX", ("experiment_accession",)),
    ("DRX", ("experiment_accession",)),
    ("ERZ", ("analysis_accession",)),
)

# ena-api-handler types `environmental_sample` and `germline` as booleans, but
# ENA returns "" for them on most records, which fails model validation and
# would lose the whole response. Renaming them before validation lands their
# raw value in the model's `extra` instead; `_rows` renames them back.
#
# ponytail: drop this once ena-api-handler coerces blank booleans itself —
# its `field_coercions` hook cannot, as it runs *after* validation.
_BLANK_BOOLS: Final[dict[str, str]] = {
    "environmental_sample": "environmental_sample_raw",
    "germline": "germline_raw",
}
_BLANK_BOOLS_BACK: Final[dict[str, str]] = {v: k for k, v in _BLANK_BOOLS.items()}

#: Accession prefix -> the Portal result that record *is*, for the accessions
#: ENA issues in two forms. Used to translate one form into the other when the
#: result being searched can only match the form the caller did not give.
_OWNING_RESULT: Final[dict[str, ENAPortalResultType]] = {
    "PRJ": ENAPortalResultType.STUDY,
    "ERP": ENAPortalResultType.STUDY,
    "SRP": ENAPortalResultType.STUDY,
    "DRP": ENAPortalResultType.STUDY,
    "SAM": ENAPortalResultType.SAMPLE,
    "ERS": ENAPortalResultType.SAMPLE,
    "SRS": ENAPortalResultType.SAMPLE,
    "DRS": ENAPortalResultType.SAMPLE,
}

#: Accessions per request. The handler issues a GET, so the whole query rides
#: in the URL; 50 accessions is a comfortable margin under the usual 8 KB.
_CHUNK: Final = 50

#: ``limit=0`` means "no cap" to the Portal. Passed explicitly rather than
#: relying on the endpoint's default staying absent.
_NO_LIMIT: Final = 0


def portal_result(entity: str) -> ENAPortalResultType | None:
    """The Portal result answering for ``entity``, or ``None`` if there is none."""
    return PORTAL_RESULTS.get(entity)


def _is_accession(value: str) -> bool:
    """Cheap guard: the value goes into a query string, inside quotes."""
    return bool(value) and value.replace("_", "").replace("-", "").replace(".", "").isalnum()


def _chunks(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _rows(client: ENAClient, result: ENAPortalResultType, query: str) -> list[dict[str, Any]]:
    """One Portal search, as plain dicts with every field it returned."""
    models = client.search(
        result=result,
        query=ENARawQuery(query),
        fields=["all"],
        limit=_NO_LIMIT,
        field_aliases=_BLANK_BOOLS,
    )
    # warnings=False: the handler coerces base_count/read_count/tax_id to int
    # after validation, against a model that declares them str, and pydantic
    # says so on every dump.
    return [
        {_BLANK_BOOLS_BACK.get(k, k): v for k, v in model.model_dump(warnings=False).items()}
        for model in models
    ]


def _or_query(fields: Sequence[str], accessions: Sequence[str]) -> str:
    return " OR ".join(f'{field}="{a}"' for a in accessions for field in fields)


def fields_for_accessions(
    entity: str,
    accessions: Iterable[str],
    *,
    username: str = "",
    password: str = "",
) -> dict[str, dict[str, Any]]:
    """Everything the Portal indexes about these accessions, keyed by accession.

    Each row is indexed under *every* identifying accession it carries, so a
    caller holding either form of a study's accession finds the same row.

    Accessions the Portal cannot answer for — unreleased records these
    credentials cannot see, and everything submitted to the test environment —
    are simply absent from the result.

    Args:
        entity: One of :data:`PORTAL_RESULTS`.
        accessions: The accessions to look up; duplicates and blanks dropped.
        username: Webin username, for records that are not yet released.
        password: Webin password.

    Returns:
        ``{accession: row}``, empty when there is nothing to ask about.
    """
    result = PORTAL_RESULTS.get(entity)
    if result is None:
        return {}
    ids = [a for a in dict.fromkeys(accessions) if _is_accession(a)]
    if not ids:
        return {}

    identity = _IDENTITY_FIELDS[result]
    found: dict[str, dict[str, Any]] = {}
    with ENAClient(username=username or None, password=password or None) as client:
        for chunk in _chunks(ids, _CHUNK):
            for row in _rows(client, result, _or_query(identity, chunk)):
                for field in identity:
                    key = row.get(field)
                    if isinstance(key, str) and key:
                        found[key] = row
    return found


def search_public(
    entity: str,
    linked_to: str,
    *,
    username: str = "",
    password: str = "",
) -> list[dict[str, Any]]:
    """Public records of ``entity`` related to the accession ``linked_to``.

    This is the half of ENA a Webin account cannot see: records anybody
    submitted, found by accession rather than by ownership. "The runs in
    PRJEB1787", "the samples in ERP013810" — the accession's *kind* decides
    which field is queried, so the caller does not have to say.

    ENA resolves the relationship itself here, unlike
    ``records.list_records``'s ``linked_to``, which has to build a lineage
    index because the Reports API cannot answer a relational question.

    Credentials are optional and only widen what is visible: with them, the
    account's own unreleased records show up alongside the public ones.

    Args:
        entity: One of :data:`PORTAL_RESULTS`.
        linked_to: A study, sample, run, experiment or analysis accession.
        username: Webin username. Optional — public data needs none.
        password: Webin password.

    Returns:
        The matching rows, with every field the Portal holds. Empty when
        nothing matches.

    Raises:
        ValueError: ``entity`` has no Portal equivalent, ``linked_to`` is not
            a plausible accession, or the two cannot be related — the Portal
            cannot answer "which studies contain run ERR1", because a study
            row has no run accession to filter on.
    """
    result = PORTAL_RESULTS.get(entity)
    if result is None:
        raise ValueError(
            f"No public search for {entity!r}; expected one of {', '.join(PORTAL_RESULTS)}"
        )
    if not _is_accession(linked_to):
        raise ValueError(f"Not a plausible accession: {linked_to!r}")

    with ENAClient(username=username or None, password=password or None) as client:
        query = _clause(client, result, linked_to)
        if not query:
            raise ValueError(
                f"ENA cannot list {entity} by {linked_to!r} — that result has no field to match it on"
            )
        return _rows(client, result, query)


def _clause(client: ENAClient, result: ENAPortalResultType, accession: str) -> str:
    """The query matching ``accession`` against ``result``, or ``""``.

    ENA issues studies and samples two accessions each, and a Portal result
    can usually be filtered by only one of them — ``sample`` knows
    ``study_accession`` but not ``secondary_study_accession``. So when the
    form the caller has is not the form this result can match, the other form
    is looked up (one extra request) and used instead. That matters more than
    it sounds: the Reports API hands back the secondary form for studies, so
    it is the form this app usually has.
    """
    fields = _queryable_fields(result, accession)
    if fields:
        return _or_query(fields, [accession])
    for other in _other_forms(client, accession):
        fields = _queryable_fields(result, other)
        if fields:
            return _or_query(fields, [other])
    return ""


def _other_forms(client: ENAClient, accession: str) -> list[str]:
    """The same record's other accession(s), for a study or sample."""
    owner = next(
        (r for prefix, r in _OWNING_RESULT.items() if accession.upper().startswith(prefix)),
        None,
    )
    if owner is None:
        return []
    identity = _IDENTITY_FIELDS[owner]
    for row in _rows(client, owner, _or_query(identity, [accession])):
        return [row[f] for f in identity if isinstance(row.get(f), str) and row[f] != accession]
    return []


def _queryable_fields(result: ENAPortalResultType, accession: str) -> tuple[str, ...]:
    """The fields of ``result`` that ``accession`` could match on.

    Which fields a Portal result can be *queried* by is not the same set it
    can *return*, and it differs per result — a study cannot be filtered by
    run accession. The handler generates a query model per result, so that
    model is the authority rather than a table maintained here.
    """
    query_model = QUERY_MODELS.get((ENAPortalDataPortal.ENA, result))
    if query_model is None:
        return ()
    candidates = next(
        (fields for prefix, fields in _QUERY_FIELDS_BY_PREFIX if accession.upper().startswith(prefix)),
        (),
    )
    return tuple(f for f in candidates if f in query_model.model_fields)
