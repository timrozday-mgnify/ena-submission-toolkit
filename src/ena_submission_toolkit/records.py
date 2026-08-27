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
_EDITABLE: Final[dict[str, dict[str, tuple[str, str]]]] = {
    "studies": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "samples": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "experiments": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "analyses": {"alias": ("attr", "alias"), "title": ("child", "TITLE")},
    "runs": {"alias": ("attr", "alias")},
    "files": {},
}

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
    max_results: int = DEFAULT_MAX_RESULTS,
) -> list[dict[str, Any]]:
    """List the account's records for one entity, as plain dicts.

    ``status`` filters on the ENA release status (``PRIVATE``, ``PUBLIC``,
    ``CANCELLED``, ``SUPPRESSED``); ``"all"`` keeps everything. File reports
    carry no status and are never filtered.

    Report models allow extra fields, so a row carries whatever the Reports
    API sent — a grid can show columns this library has never heard of.
    """
    method = REPORT_METHODS.get(entity)
    if method is None:
        raise ValueError(f"Unknown entity {entity!r}; expected one of {', '.join(REPORT_METHODS)}")
    with webin_client(creds, test) as client:
        # list_runs() already joins against list_experiments() to fill in
        # study_accession/sample_accession when the run's own report omits them.
        rows = [record.model_dump() for record in getattr(client.reports, method)(max_results=max_results)]
    return rows if entity == "files" else _filter_by_status(rows, status)


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
