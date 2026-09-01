# ena-submission-toolkit

Build and submit ENA records (studies, samples) from structured or
DataHarmonizer-exported data.

A schema-driven layer above two smaller libraries:

- [`ena-api-client`](https://github.com/timrozday-mgnify/ena-api-client) — transport: a typed
  client for the Webin Submission v2 and Reports APIs.
- [`linkml-lib`](https://github.com/timrozday-mgnify/linkml-lib) — schema introspection: generic
  LinkML schema helpers (slot metadata, unit rules, etc.), no ENA awareness beyond the `ena_*`
  annotation conventions it documents.

`ena-submission-toolkit` sits between them: XML manifest building, ENA-checklist unit handling
(parsing/converting/validating values against a schema's allowed units), duplicate-alias detection
for idempotent re-submission, XSD validation, renaming a DataHarmonizer export's fields from
human-readable titles to their LinkML `annotations.id` values, and browsing/editing the records
already held under a Webin account.

Every application in this ecosystem talks to ENA **through here** (or through `ena-api-client`
directly) and nowhere else — [`ena-browser-ui`](https://github.com/timrozday-mgnify/ena-browser-ui)
and [`mimicc-ena-submission-assistant`](https://github.com/timrozday-mgnify/mimicc-ena-submission-assistant)
are HTTP/UI shells over `ena_submission_toolkit.records`, and the
[`ena-browser`](https://github.com/timrozday-mgnify/ena-browser) element is a pure view that never
makes an ENA request at all.

## Modules

- `ena_submission_toolkit.common` — credentials (`ENA_WEBIN`/`ENA_WEBIN_PASSWORD`), hold-until-date
  validation, duplicate-alias detection (`find_duplicates_by_alias_title`/`classify_duplicates`),
  ENA-checklist unit normalisation (`normalise_sample_records`/`normalise_unit_value`), XSD
  validation, DataHarmonizer `Container`-export unwrapping, tabular/JSON record loading, result I/O.
- `ena_submission_toolkit.submit_study` — build/validate/submit ENA study (project) XML.
- `ena_submission_toolkit.submit_sample` — build/validate/submit ENA sample XML, with optional
  schema-driven unit normalisation via `submit_batch(..., unit_rules=...)`.
- `ena_submission_toolkit.records` — browse and change what a Webin account already holds:
  `list_records` (Reports API rows as plain dicts, optionally filtered by release status, by
  free-text `search`, or by submission lineage — `linked_to="PRJEB123"` for the samples in a
  study, `unlinked=True` for the samples no experiment or read points at. The Reports API
  itself takes only `max-results` and a status, so everything beyond that is joined and
  filtered here, from the experiment/run/analysis rows; run rows also carry
  `process_status`/`process_date`/`process_error` from the run-processing report, which is how a
  submitter sees whether ENA has finished archiving the read files as opposed to merely registering
  the run), `editable_columns`, `read_xml_fields` (everything only the record XML holds, read in batches: the
  editable fields — a run's title, an experiment's library and instrument — and the record's whole
  TAG/VALUE attribute list as `attr:`-prefixed columns, which is where a checklist's fields live.
  Neither listing API has them: the Reports API returns five columns per record, and the Portal API
  indexes a fixed set — 102 fields for a sample — so a checklist tag outside that set exists nowhere
  else. Attribute columns are read-only; `read_editable_fields` is the editable subset alone),
  `modify_records` (fetch the record's current XML, patch the edited fields, resubmit as a MODIFY —
  never rebuilt from a report row, which would drop everything ENA holds but does not report),
  `preview_modify_records` (the same manifests, returned instead of sent, so a caller can show
  exactly what a MODIFY would do before committing to it — `modify_records` echoes the document it
  submitted, so the two can be compared), `undo_changes` (every result also carries `previous`, the
  edited fields' values as ENA held them just before the change, and `undo_xml`, a complete MODIFY
  manifest restoring the pre-edit document — so a caller can keep a stack of applied changes and
  walk back down it), `record_action` (release/hold/suppress/cancel/kill) and
  `find_runs_by_experiment_alias`.
  Credentials are passed per call as `records.Credentials`, so a multi-user server never needs
  process-wide state.
- `ena_submission_toolkit.portal` — the half of ENA a Webin account cannot see. The Reports API
  is scoped by *ownership*, not by release status: it lists this account's records, private and
  public alike, and nothing anybody else submitted. The ENA Portal API is keyed by accession
  instead, so this module reaches any record — `search_public("runs", "PRJEB1787")` for a study's
  public runs, whoever submitted them — and also fills out the account's own rows with every field
  ENA indexes (`fields_for_accessions`, ~200 fields for a run against the Reports API's five;
  `list_records(..., full_fields=True)` merges it into a listing, alongside the record's own
  submitted XML — every checklist attribute, from the Browser API, which is the only one of the
  two that answers in the test environment). Transport is
  [`ena-api-handler`](https://github.com/EBI-Metagenomics/ena-api-handler) rather than
  `ena-api-client`, which owns the Webin account APIs; what lives here is the behaviour on top —
  which Portal result answers for which entity, turning an accession into a query (including
  translating `ERP…` into `PRJEB…` when the result being searched can only match the latter), and
  merging Portal rows into report rows. Production only: there is no Portal API on `wwwdev`.
  Credentials are optional and only widen what is visible.
- `ena_submission_toolkit.prepare_dh_output` — rename a DataHarmonizer export's fields to their
  LinkML `annotations.id` values (`prepare_data` for in-memory data, `prepare` for files).

## Install

```bash
pip install "ena-submission-toolkit @ git+https://github.com/timrozday-mgnify/ena-submission-toolkit.git"
```

or, for local development:

```bash
git clone https://github.com/timrozday-mgnify/ena-submission-toolkit.git
cd ena-submission-toolkit
pip install -e ".[dev]"
pytest
```

## Credentials

Read from environment variables (never written to disk by this package):

```bash
export ENA_WEBIN=Webin-XXXXX
export ENA_WEBIN_PASSWORD=SECRET
```
