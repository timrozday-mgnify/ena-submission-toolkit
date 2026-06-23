"""Unified CLI: ``ena-submission-toolkit <command> ...``.

Aggregates the per-module ``typer`` commands (each also runnable standalone,
e.g. ``python -m ena_submission_toolkit.submit_study``) under one installed
entry point.
"""

from __future__ import annotations

import typer

from . import prepare_dh_output, submit_sample, submit_study

app = typer.Typer(
    help="Build, validate, and submit ENA records (studies, samples) from structured/DataHarmonizer-exported data.",
    add_completion=False,
)
app.command("submit-study")(submit_study.main)
app.command("submit-sample")(submit_sample.main)
app.command("prepare-dh-output")(prepare_dh_output.main)


if __name__ == "__main__":
    app()
