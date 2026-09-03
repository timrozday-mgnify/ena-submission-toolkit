"""Build and submit ENA records (studies, samples) from structured data.

A schema-driven layer above ``ena-api-client`` (Webin transport),
``ena-api-handler`` (ENA Portal transport) and ``linkml-lib`` (schema
introspection): XML manifest building, ENA-checklist unit handling,
duplicate-alias detection, and DataHarmonizer export field renaming.

Modules:
    common              -- credentials, hold-until validation, duplicate
                            detection, unit normalisation, XSD validation,
                            Container-unwrap, result I/O.
    submit_study        -- build/validate/submit ENA study (project) XML.
    submit_sample       -- build/validate/submit ENA sample XML.
    records             -- browse and change the records held under a Webin
                            account: list, MODIFY a field, run lifecycle
                            actions.
    portal              -- the public half of ENA, via the Portal API: search
                            records this account does not own, and fill out
                            the ones it does with every field ENA indexes.
    prepare_dh_output    -- rename a DataHarmonizer export's fields to their
                            LinkML annotations.id values.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

__version__ = "0.1.4"  # keep in step with pyproject's version


def xsd_dir() -> Path:
    """Directory of bundled ENA XSDs (ENA.project.xsd, SRA.*.xsd) for XSD validation.

    Ships inside the package (``assets/ena_schema/``) so callers don't need
    their own copy on disk — used as the default ``--xsd`` for the CLIs.
    """
    return Path(str(importlib.resources.files("ena_submission_toolkit") / "assets" / "ena_schema"))
