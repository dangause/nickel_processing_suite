"""DEPRECATED shim -- PS1 templates via the generic external-template framework.

Kept so ``run.py``, ``cli.py`` and ``bps.py`` need no changes: ``ps1_template``
predates the generic multi-survey ``external_template`` framework (PS1,
SkyMapper, ...) and is now just ``source="ps1"`` fixed in. All the actual
band-validation, skip-if-exists, and stack-dispatch logic lives in
``stips.core.external_template``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stips.core import external_template

if TYPE_CHECKING:
    from pathlib import Path

    from stips.core.config import Config

#: Historical name; PS1 results are ordinary external-template results.
PS1TemplateResult = external_template.ExternalTemplateResult


def run(
    ra: float,
    dec: float,
    band: str,
    config: "Config",
    *,
    collection: str | None = None,
    tract: int | None = None,
    size: float = 0.2,
    output_dir: "Path | None" = None,
    degrade_seeing: float | None = None,
    unity_photocalib: bool = False,
    overwrite: bool = False,
    log_file: "Path | None" = None,
) -> external_template.ExternalTemplateResult:
    """Download and ingest a PS1 template (see external_template.run)."""
    return external_template.run(
        "ps1",
        ra,
        dec,
        band,
        config,
        collection=collection,
        tract=tract,
        size=size,
        output_dir=output_dir,
        degrade_seeing=degrade_seeing,
        unity_photocalib=unity_photocalib,
        overwrite=overwrite,
        log_file=log_file,
    )


def check_exists(
    band: str,
    config: "Config",
    collection: str | None = None,
) -> bool:
    """Check whether a PS1 template already exists in Butler."""
    return external_template.check_exists("ps1", band, config, collection)
