"""Browse and change the records held under a Webin account.

The generic half of a "records browser" application: list what ENA holds for
an account, change a field on one of those records, and run the lifecycle
actions (release / hold / suppress / cancel / kill). No UI, no framework, no
project-specific vocabulary — ``ena-browser-ui`` and
``mimicc-ena-submission-assistant`` both drive this module, and the
``ena-browser`` element renders what ``list_records`` returns.

Credentials are passed in explicitly on every call (both callers receive them
per-request) and turned into a short-lived ``WebinClient``. Nothing here reads
or writes the environment or the disk — for env-var credentials use
``common.create_webin_client`` instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from ena_api import WebinClient, WebinConfig, is_accession
from lxml import etree

from .common import validate_hold_until

#: Every entity the Reports API exposes, in the order a UI usually shows them.
ENTITIES: Final = ("studies", "samples", "runs", "experiments", "analyses", "files")

#: Entity -> ``ReportsProxy`` method. ENA calls a study a "project".
REPORT_METHODS: Final[dict[str, str]] = {
    "studies": "list_projects",
    "projects": "list_projects",
    "samples": "list_samples",
    "runs": "list_runs",
    "experiments": "list_experiments",
    "analyses": "list_analyses",
    "files": "list_files",
}

#: Lifecycle actions, each a ``SubmitProxy`` method of the same name.
ACTIONS: Final = ("release", "hold", "suppress", "cancel", "kill")

# Entity -> (XML set element, XML record element). Files have neither: they are
# not submittable objects in their own right.
_XML_TAGS: Final[dict[str, tuple[str, str]]] = {
    "studies": ("PROJECT_SET", "PROJECT"),
    "samples": ("SAMPLE_SET", "SAMPLE"),
    "runs": ("RUN_SET", "RUN"),
    "experiments": ("EXPERIMENT_SET", "EXPERIMENT"),
    "analyses": ("ANALYSIS_SET", "ANALYSIS"),
}

# The only fields a MODIFY may change, per entity, and how each maps onto the
# record's XML. Deliberately small: every field here is one this module knows
# how to patch without touching anything else in the document.
#
#   ("attr", name)  -> an attribute of the record element
#   ("child", name) -> the text of a direct child element
# A ``child`` name is an ElementPath, so a field nested several levels down
# (an experiment's library descriptor, its platform) needs no extra machinery.
_EDITABLE: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "studies": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "samples": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "experiments": {
        "alias": ("attr", "alias"),
        "title": ("child", "TITLE"),
        "design_description": ("child", "DESIGN/DESIGN_DESCRIPTION"),
        "library_name": ("child", "DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_NAME"),
        "library_strategy": ("child", "DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_STRATEGY"),
        "library_source": ("child", "DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_SOURCE"),
        "library_selection": ("child", "DESIGN/LIBRARY_DESCRIPTOR/LIBRARY_SELECTION"),
        # The instrument sits under whichever platform element the experiment
        # was registered with (ILLUMINA, OXFORD_NANOPORE, ...), hence the
        # wildcard: the model can be corrected, the platform cannot be swapped.
        "instrument_model": ("child", "PLATFORM/*/INSTRUMENT_MODEL"),
    },
    "analyses": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "runs": {
        "alias": ("attr", "alias"),
        "title": ("child", "TITLE"),
        "run_center": ("attr", "run_center"),
        "run_date": ("attr", "run_date"),
    },
    "files": {},
}

#: How many accessions to ask the Browser API for in one request.
_XML_BATCH: Final = 100

DEFAULT_MAX_RESULTS: Final = 5000

#: Names a MODIFY submission when the caller does not.
DEFAULT_MODIFY_ALIAS: Final = "ena-submission-toolkit-modify"


@dataclass(frozen=True)
class Credentials:
    """A Webin username/password pair, held only for the call it is passed to."""

    username: str
    password: str


@contextmanager
def webin_client(creds: Credentials, test: bool) -> Iterator[WebinClient]:
    """An authenticated ``WebinClient`` for the duration of the block."""
    client = WebinClient(config=WebinConfig(webin_id=creds.username, password=creds.password, test=test))
    try:
        yield client
    finally:
        client.close()


def validate_credentials(creds: Credentials, *, test: bool) -> None:
    """Check credentials with the cheapest authenticated call there is.

    Raises ``PermissionError`` if ENA rejects them.
    """
    with webin_client(creds, test) as client:
        client.reports.list_projects(max_results=1)


def editable_columns(entity: str) -> list[str]:
    """The fields :func:`modify_records` knows how to change on this entity.

    Example:
        >>> editable_columns("studies")
        ['alias', 'title']
        >>> editable_columns("files")
        []
    """
    return sorted(_EDITABLE.get(entity, {}))


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _filter_by_status(rows: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    if status.lower() == "all":
        return rows
    target = status.upper()
    return [row for row in rows if (row.get("status") or "").upper() == target]


def list_records(
    creds: Credentials,
    entity: str,
    *,
    test: bool,
    status: str = "all",
    search: str = "",
    linked_to: str = "",
    unlinked: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict[str, Any]]:
    """List the account's records for one entity, as plain dicts.

    ``status`` filters on the ENA release status (``PRIVATE``, ``PUBLIC``,
    ``CANCELLED``, ``SUPPRESSED``); ``"all"`` keeps everything. File reports
    carry no status and are never filtered.

    Report models allow extra fields, so a row carries whatever the Reports
    API sent — a grid can show columns this library has never heard of.

    Run rows additionally carry ``process_status``/``process_date``/
    ``process_error`` from the run-processing report: whether ENA has finished
    validating and archiving the run's read files, which the run report itself
    does not say.

    The remaining criteria are applied here, not by ENA. The Webin Reports API
    takes only ``max-results``, a release ``status``, and (on the process/file
    reports) a processing status — there is no free-text search and no way to
    ask it a relational question. So:

    ``search``
        Whitespace-separated terms, each of which must appear (case-insensitive
        substring) somewhere in the row.
    ``linked_to``
        An accession of any entity; keeps the rows that share a submission
        lineage with it — "the samples in study PRJEB123", "the reads for
        sample ERS9000001". See :func:`_link_index` for what "linked" means.
    ``unlinked``
        Keeps only the rows nothing else points at: samples with no experiment
        or read against them, studies with nothing in them.

    ``linked_to`` and ``unlinked`` cost three extra report requests (the
    experiment/run/analysis rows the lineage is built from); ``search`` and
    ``status`` cost nothing.
    """
    method = REPORT_METHODS.get(entity)
    if method is None:
        raise ValueError(f"Unknown entity {entity!r}; expected one of {', '.join(REPORT_METHODS)}")
    with webin_client(creds, test) as client:
        # list_runs() already joins against list_experiments() to fill in
        # study_accession/sample_accession when the run's own report omits them.
        rows = [record.model_dump() for record in getattr(client.reports, method)(max_results=max_results)]
        if entity == "runs" and rows:
            # Metadata being registered and read files being archived are two
            # different events; only the second answers "is my submission
            # through yet?", and it lives in a separate report.
            processing = _run_processing(client, max_results)
            for row in rows:
                row.update(processing.get(row.get("accession") or "", {}))
        if linked_to or unlinked:
            index = _link_index(client, max_results)
            related = _expand(index, linked_to) if linked_to else set()
            rows = [row for row in rows if _keep_by_link(row, index, related, unlinked=unlinked)]
    if search:
        rows = _filter_by_search(rows, search)
    return rows if entity == "files" else _filter_by_status(rows, status)


def _run_processing(client: WebinClient, max_results: int) -> dict[str, dict[str, Any]]:
    """Run accession -> the file-processing columns to merge into its row."""
    processing: dict[str, dict[str, Any]] = {}
    for report in client.reports.list_run_processes(max_results=max_results):
        if not report.run_accession:
            continue
        processing[report.run_accession] = {
            "process_status": report.process_status,
            "process_date": report.process_date,
            "process_error": report.error_message,
        }
    return processing


def _filter_by_search(rows: list[dict[str, Any]], search: str) -> list[dict[str, Any]]:
    """Rows in which every whitespace-separated term appears somewhere."""
    terms = search.lower().split()
    kept: list[dict[str, Any]] = []
    for row in rows:
        haystack = " ".join(str(value) for value in row.values() if value is not None).lower()
        if all(term in haystack for term in terms):
            kept.append(row)
    return kept


def _row_ids(row: dict[str, Any]) -> set[str]:
    """The accessions a row answers to — a study is both a PRJ and an ERP.

    ``run_accession`` is here for file rows, whose own "accession" is not what
    anything links to; on every other entity the field is absent.
    """
    return {str(row.get(key) or "") for key in ("accession", "secondary_accession", "run_accession")} - {""}


def _link_index(client: WebinClient, max_results: int) -> dict[str, set[str]]:
    """Accession -> every accession sharing a submission lineage with it.

    An experiment is the hub of an ENA submission: it names its study and its
    sample, and a run names its experiment. An analysis names its study. Each
    such record is therefore a group of accessions that belong together, and
    two accessions are "linked" when some group holds both — which makes
    "samples in study X" and "reads for sample Y" the same lookup.

    ponytail: one hop only. A ->PRJ<- B relates two samples in the same study,
    which is what the questions being asked here mean by related; a graph
    walk would be a different, longer answer.
    """
    groups: list[set[str]] = []
    for exp in client.reports.list_experiments(max_results=max_results):
        groups.append(
            {
                exp.accession,
                exp.secondary_accession,
                exp.study_accession,
                exp.sample_accession,
            }
        )
    # Raw run rows would do, but list_runs() has already filled in the
    # study/sample a run's own report omits, so use what it worked out.
    for run in client.reports.list_runs(max_results=max_results):
        groups.append(
            {
                run.accession,
                run.secondary_accession,
                run.experiment_accession,
                run.study_accession,
                run.sample_accession,
            }
        )
    for analysis in client.reports.list_analyses(max_results=max_results):
        groups.append({analysis.accession, analysis.secondary_accession, analysis.study_accession})

    index: dict[str, set[str]] = {}
    for group in groups:
        group.discard("")
        for accession in group:
            index.setdefault(accession, set()).update(group)
    return index


def _expand(index: dict[str, set[str]], accession: str) -> set[str]:
    """Everything linked to ``accession``, including itself."""
    return index.get(accession, set()) | {accession}


def _keep_by_link(
    row: dict[str, Any],
    index: dict[str, set[str]],
    related: set[str],
    *,
    unlinked: bool,
) -> bool:
    ids = _row_ids(row)
    if unlinked:
        # Linked means something *other than this record* is in its group;
        # every record is trivially in its own.
        if any(index.get(accession, set()) - ids for accession in ids):
            return False
    return bool(ids & related) if related else True


def read_editable_fields(
    creds: Credentials,
    entity: str,
    accessions: list[str],
    *,
    test: bool,
) -> dict[str, dict[str, Any]]:
    """The *current* value of every editable field, per accession.

    The Reports API returns a handful of columns per record — for a run, not
    even its title; for an experiment, nothing about its library or
    instrument. Those fields only exist in the record's XML, so a grid that
    wants to edit them has to be shown them first. This reads them, in batches,
    from the same Browser API documents :func:`modify_records` patches, so what
    the user edits is what ENA currently holds.

    Returns ``{accession: {field: value}}``, omitting records ENA does not
    hold and fields the record's XML does not carry. Fields are exactly
    :func:`editable_columns` for the entity — an entity with none (files)
    costs no request at all.
    """
    mapping = _EDITABLE.get(entity)
    if entity not in _XML_TAGS or not mapping:
        return {}
    wanted = [a for a in dict.fromkeys(accessions) if is_accession(a)]
    if not wanted:
        return {}

    _, record_tag = _XML_TAGS[entity]
    found: dict[str, dict[str, Any]] = {}
    with webin_client(creds, test) as client:
        for start in range(0, len(wanted), _XML_BATCH):
            batch = wanted[start : start + _XML_BATCH]
            try:
                payload = client.browser.xml_many(batch)
            except LookupError:
                continue  # ENA holds none of this batch; the others still matter
            for record in etree.fromstring(payload).iter(record_tag):
                accession = record.get("accession") or ""
                if not accession:
                    continue
                values: dict[str, Any] = {}
                for field, (kind, name) in mapping.items():
                    value = record.get(name) if kind == "attr" else record.findtext(name)
                    if value is not None:
                        values[field] = value
                found[accession] = values
    return found


def find_runs_by_experiment_alias(
    creds: Credentials,
    aliases: set[str],
    *,
    test: bool,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict[str, dict[str, str]]:
    """Find runs already in ENA by their experiment's alias.

    Thin wrapper over ``ReportsProxy.find_runs_by_experiment_alias``, for
    callers that hold :class:`Credentials` rather than a client — used to make
    a reads submission resumable ("is this run already submitted?").
    """
    if not aliases:
        return {}
    with webin_client(creds, test) as client:
        return client.reports.find_runs_by_experiment_alias(aliases, max_results=max_results)


# ---------------------------------------------------------------------------
# Modification
# ---------------------------------------------------------------------------


def _find_record(document: etree._Element, record_tag: str) -> etree._Element:
    """The single record element in a Browser API response, set-wrapped or not."""
    if document.tag == record_tag:
        return document
    found = document.findall(f".//{record_tag}")
    if len(found) != 1:
        raise LookupError(f"Expected exactly one <{record_tag}> in the fetched XML, found {len(found)}")
    return found[0]


def _apply_change(record: etree._Element, entity: str, field: str, value: Any) -> None:
    mapping = _EDITABLE.get(entity, {})
    if field not in mapping:
        raise ValueError(f"{field!r} is not editable on {entity}")
    kind, name = mapping[field]
    text = "" if value is None else str(value)
    if kind == "attr":
        if not text:
            raise ValueError(f"{field!r} cannot be emptied")
        record.set(name, text)
        return
    child = record.find(name)
    if child is None:
        # Element order matters to ENA's XSDs; a missing optional element is
        # rare here (report rows carry these fields) and getting the position
        # right is not something this generic patcher can know.
        raise LookupError(f"The record's XML has no <{name}> element to change")
    child.text = text


def _modify_document(record: etree._Element, entity: str, submission_alias: str) -> bytes:
    """Wrap a patched record element in a WEBIN MODIFY submission."""
    set_tag, _ = _XML_TAGS[entity]
    webin = etree.Element("WEBIN")
    submission = etree.SubElement(etree.SubElement(webin, "SUBMISSION_SET"), "SUBMISSION")
    submission.set("alias", submission_alias)
    etree.SubElement(etree.SubElement(etree.SubElement(submission, "ACTIONS"), "ACTION"), "MODIFY")
    etree.SubElement(webin, set_tag).append(record)
    return etree.tostring(webin, encoding="UTF-8", xml_declaration=True)


def _build_manifests(
    client: WebinClient,
    entity: str,
    records: list[dict[str, Any]],
    submission_alias: str,
) -> Iterator[tuple[dict[str, Any], bytes | None]]:
    """Build one MODIFY document per record, yielding ``(result, document)``.

    The single place a MODIFY manifest is constructed, so what
    :func:`preview_modify_records` shows a user is byte-for-byte what
    :func:`modify_records` submits. ``document`` is ``None`` for a record that
    could not be built — its ``result`` carries why, and nothing is sent.
    """
    _, record_tag = _XML_TAGS[entity]
    for entry in records:
        accession = str(entry.get("accession") or "")
        changes = dict(entry.get("changes") or {})
        result: dict[str, Any] = {
            "accession": accession,
            "changes": changes,
            "success": False,
            "messages": [],
            "xml": "",
        }
        if not accession or not changes:
            result["messages"] = ["Nothing to change"]
            yield result, None
            continue
        try:
            record = _find_record(etree.fromstring(client.browser.xml(accession)), record_tag)
            for field, value in changes.items():
                _apply_change(record, entity, field, value)
            document = _modify_document(record, entity, submission_alias)
        except Exception as exc:  # noqa: BLE001 - one bad record must not sink the batch
            result["messages"] = [str(exc)]
            yield result, None
            continue
        result["xml"] = document.decode("utf-8")
        result["success"] = True  # the manifest was built; whether ENA accepts it is another matter
        yield result, document


def preview_modify_records(
    creds: Credentials,
    entity: str,
    records: list[dict[str, Any]],
    *,
    test: bool,
    submission_alias: str = DEFAULT_MODIFY_ALIAS,
) -> dict[str, Any]:
    """Build the MODIFY manifests for a change set without submitting anything.

    Same arguments as :func:`modify_records`, same work up to the point of
    submission — each record's current XML is fetched and patched — but the
    documents are returned instead of sent, so a user can read what would go to
    ENA before deciding to send it.

    Returns ``{"success": every manifest built, "results": [{accession,
    changes, success, messages, xml}]}``. ``success`` here means "this manifest
    was built", never "ENA accepted it": nothing was submitted.
    """
    if entity not in _XML_TAGS:
        raise ValueError(f"{entity} records cannot be modified")
    with webin_client(creds, test) as client:
        results = [result for result, _ in _build_manifests(client, entity, records, submission_alias)]
    return {"success": all(r["success"] for r in results), "results": results}


def modify_records(
    creds: Credentials,
    entity: str,
    records: list[dict[str, Any]],
    *,
    test: bool,
    submission_alias: str = DEFAULT_MODIFY_ALIAS,
) -> dict[str, Any]:
    """Apply a change set to ENA, one MODIFY submission per record.

    ``records`` is ``[{"accession": ..., "changes": {field: value}}]`` — the
    shape a grid's change set narrows to once the host has filtered it to the
    fields it allows (see :func:`editable_columns`).

    An ENA MODIFY **replaces** the whole object, and the Reports API returns
    only a handful of fields per record (alias, accession, title, status) — so
    building the submission from a report row would silently drop everything
    ENA holds but does not report, e.g. a study's description or a sample's
    attributes. Instead each record's current XML is fetched from the Browser
    API, the edited fields are patched into it, and that document goes back. A
    record whose XML cannot be fetched is reported as failed and **not**
    submitted: a partial document is worse than no submission.

    Returns ``{"success": all succeeded, "results": [{accession, changes,
    success, messages, info, warnings, errors, xml}]}``. ``xml`` is the exact
    document submitted — the same bytes :func:`preview_modify_records` would
    have shown — so a caller can prove what it sent. ``messages`` stays the
    flat "everything ENA said" list; ``info``/``warnings``/``errors`` are that
    same receipt split up.
    """
    if entity not in _XML_TAGS:
        raise ValueError(f"{entity} records cannot be modified")

    results: list[dict[str, Any]] = []
    with webin_client(creds, test) as client:
        for result, document in _build_manifests(client, entity, records, submission_alias):
            if document is None:
                results.append(result)
                continue
            try:
                receipt = client.submit.xml(document)
                result["success"] = receipt.success
                result["info"] = list(receipt.messages)
                result["warnings"] = list(receipt.warnings)
                result["errors"] = list(receipt.errors)
                result["messages"] = result["info"] + result["warnings"] + result["errors"]
            except Exception as exc:  # noqa: BLE001 - one bad record must not sink the batch
                result["success"] = False
                result["messages"] = [str(exc)]
                result["errors"] = [str(exc)]
            results.append(result)

    return {"success": all(r["success"] for r in results), "results": results}


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------


def record_action(
    creds: Credentials,
    accession: str,
    action: str,
    *,
    test: bool,
    hold_until: str | None = None,
    alias: str | None = None,
) -> dict[str, Any]:
    """Run one lifecycle action against one accession.

    ``action`` is one of :data:`ACTIONS`. ``hold`` requires ``hold_until``
    (``YYYY-MM-DD``, validated before anything is sent). Returns
    ``{"accession", "action", "success", "messages": [str]}`` — a failure ENA
    reported is a result, not an exception.
    """
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
    if not is_accession(accession):
        raise ValueError(f"Not a plausible accession: {accession!r}")
    if action == "hold":
        if not hold_until:
            raise ValueError("hold needs a hold_until date (YYYY-MM-DD)")
        validate_hold_until(hold_until)

    result: dict[str, Any] = {"accession": accession, "action": action}
    with webin_client(creds, test) as client:
        args: tuple[Any, ...] = (accession, hold_until) if action == "hold" else (accession,)
        kwargs: dict[str, Any] = {"alias": alias} if alias else {}
        try:
            receipt = getattr(client.submit, action)(*args, **kwargs)
            result["success"] = receipt.success
            result["messages"] = receipt.messages + receipt.warnings + receipt.errors
        except Exception as exc:  # noqa: BLE001 - the caller shows this text
            result["success"] = False
            result["messages"] = [str(exc)]
    return result
