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
for idempotent re-submission, XSD validation, and renaming a DataHarmonizer export's fields from
human-readable titles to their LinkML `annotations.id` values.

## Modules

- `ena_submission_toolkit.common` — credentials (`ENA_WEBIN`/`ENA_WEBIN_PASSWORD`), hold-until-date
  validation, duplicate-alias detection (`find_duplicates_by_alias_title`/`classify_duplicates`),
  ENA-checklist unit normalisation (`normalise_sample_records`/`normalise_unit_value`), XSD
  validation, DataHarmonizer `Container`-export unwrapping, tabular/JSON record loading, result I/O.
- `ena_submission_toolkit.submit_study` — build/validate/submit ENA study (project) XML.
- `ena_submission_toolkit.submit_sample` — build/validate/submit ENA sample XML, with optional
  schema-driven unit normalisation via `submit_batch(..., unit_rules=...)`.
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
