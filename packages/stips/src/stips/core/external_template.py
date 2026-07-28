"""Generic external-survey DIA template orchestration (venv side).

Downloads and ingests an external-survey (PS1, SkyMapper, ...) template by
shelling out to the in-stack CLI
(``stips.pipeline_tools.external_template.ingest``), exactly as the historical
``core/ps1_template.py`` did for PS1 alone. This module runs in the ``stips``
venv and must stay LSST-import-free; it never imports ``lsst.*`` directly.

``core/ps1_template.py`` is now a thin back-compat shim over this module (see
its module docstring) so ``run.py``, ``cli.py`` and ``bps.py`` need no changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stips.collections import template_external
from stips.core.pipeline import template_band_map
from stips.core.stack import run_with_stack
from stips.pipeline_tools.external_template.sources import (
    TemplateSourceError,
    get_source,
)

if TYPE_CHECKING:
    from pathlib import Path

    from stips.core.config import Config


@dataclass(kw_only=True)
class ExternalTemplateResult:
    """Result of an external-template download+ingest run.

    Keyword-only on purpose. ``PS1TemplateResult`` is aliased to this dataclass
    for the benefit of unknown out-of-tree callers, and ``source`` was inserted
    between ``success`` and ``band`` — so a legacy positional
    ``PS1TemplateResult(True, "r", "templates/ps1/r", 1825)`` would rebind every
    field one place to the right without raising anything.
    """

    success: bool
    source: str
    band: str
    collection: str
    tract: int | None = None
    #: The target coordinate's own patch (the first one ingested).
    patch: int | None = None
    #: Every patch the template was ingested into. An external template now
    #: lands in each skymap patch its footprint overlaps, exactly as a
    #: self-coadd template does, so a single ``patch`` no longer describes the
    #: result. Defaults to ``[patch]`` when the ingest reported only one.
    patches: list[int] | None = None
    fits_path: str | None = None
    error: str | None = None
    #: True when an existing template was found and left untouched (overwrite=False).
    skipped: bool = False


def run(
    source: str,
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
    mjd_start: float | None = None,
    mjd_end: float | None = None,
    log_file: "Path | None" = None,
) -> ExternalTemplateResult:
    """Download and ingest an external-survey template for DIA.

    Downloads a cutout from ``source`` centered on the given coordinates,
    converts it to LSST format, and ingests it as a ``template_coadd``.

    Args:
        source: External-survey adapter name (e.g. ``ps1``, ``skymapper``),
            resolved against the ``TemplateSource`` registry in-stack.
        ra: Right ascension in degrees
        dec: Declination in degrees
        band: Local science band; must be eligible for ``source`` per the
            active profile's ``template_band_maps[source]``
        config: Pipeline configuration
        collection: Output collection (default: templates/{source}/{band})
        tract: Tract number (auto-determined if None)
        size: Cutout size in degrees (default: 0.2)
        output_dir: Directory for downloaded FITS files
        degrade_seeing: Convolve to this FWHM in arcsec (e.g., 2.0)
        unity_photocalib: Force PhotoCalib=1.0 (for flux calibration consistency)
        overwrite: Replace existing template if present
        mjd_start: Earliest frame MJD to consider (sources that expose an epoch)
        mjd_end: Latest frame MJD to consider (sources that expose an epoch)
        log_file: Optional path to write LSST pipeline logs

    Returns:
        ExternalTemplateResult with collection and status
    """
    try:
        get_source(source)
    except TemplateSourceError as e:
        return ExternalTemplateResult(
            success=False,
            source=source,
            band=band,
            collection=collection or template_external(source, band),
            error=str(e),
        )

    band_map = template_band_map(config, source)
    if band not in band_map:
        eligible = ", ".join(sorted(band_map)) or "(none configured)"
        return ExternalTemplateResult(
            success=False,
            source=source,
            band=band,
            collection=collection or template_external(source, band),
            error=(
                f"{source} templates only available for bands: {eligible}; "
                f"got {band!r}"
            ),
        )
    source_band = band_map[band]

    if collection is None:
        collection = template_external(source, band)

    # Skip-if-exists policy lives here (single source of truth) rather than in
    # each caller: unless overwrite is requested, an already-ingested template
    # is left in place and reported as a (successful) skip.
    if not overwrite and check_exists(source, band, config, collection):
        return ExternalTemplateResult(
            success=True,
            source=source,
            band=band,
            collection=collection,
            skipped=True,
        )

    if output_dir is None:
        output_dir = config.repo / "external_templates"

    # Build arguments for the ingest script
    args = [
        "python",
        "-m",
        "stips.pipeline_tools.external_template.ingest",
        "--repo",
        str(config.repo),
        "--source",
        source,
        "--ra",
        str(ra),
        "--dec",
        str(dec),
        "--band",
        band,
        "--collection",
        collection,
        "--size",
        str(size),
        "--output-dir",
        str(output_dir),
    ]

    # Only pass --source-band when the profile maps the local band to a
    # different survey band; for an identity map (e.g. Nickel r->r, i->i) the
    # ingest tool's own default (source_band = band) reproduces the same
    # argument list.
    if source_band != band:
        args.extend(["--source-band", source_band])

    if tract is not None:
        args.extend(["--tract", str(tract)])

    if degrade_seeing is not None:
        args.extend(["--degrade-seeing", str(degrade_seeing)])

    if unity_photocalib:
        args.append("--unity-photocalib")

    if overwrite:
        args.append("--overwrite")

    if mjd_start is not None:
        args.extend(["--mjd-start", str(mjd_start)])

    if mjd_end is not None:
        args.extend(["--mjd-end", str(mjd_end)])

    try:
        result = run_with_stack(args, config, capture_output=True, check=False)

        if result.returncode == 0:
            # Parse output to extract tract/patch if available. ingest.py logs
            # "Data ID: {'skymap': ..., 'tract': 1825, 'patch': 0, ...}" -- no
            # line contains the literal substrings "tract=" / "patch=", so a
            # guard requiring those (the historical bug) never matched and
            # tract/patch were always None. Apply the regex searches to every
            # line instead, keeping the first match of each.
            #
            # Both streams are scanned: ingest.py's logging handler writes to
            # STDERR, so under capture_output=True the lines we are after are
            # in result.stderr and stdout is empty -- scanning stdout alone
            # reported tract/patch as None on every real run.
            tract_val = None
            patch_val = None
            patches_val = None
            fits_path = None

            captured = f"{result.stdout or ''}\n{result.stderr or ''}"
            for line in captured.split("\n"):
                if tract_val is None:
                    tract_match = re.search(r"'tract':\s*(\d+)", line)
                    if tract_match:
                        tract_val = int(tract_match.group(1))
                if patch_val is None:
                    patch_match = re.search(r"'patch':\s*(\d+)", line)
                    if patch_match:
                        patch_val = int(patch_match.group(1))
                if patches_val is None:
                    # ingest.py logs "  Patches: [156, 142, 143, 157] (tract 444)"
                    patches_match = re.search(r"Patches:\s*\[([\d,\s]*)\]", line)
                    if patches_match:
                        patches_val = [
                            int(p)
                            for p in patches_match.group(1).split(",")
                            if p.strip()
                        ]
                if "FITS file:" in line:
                    fits_path = line.split("FITS file:")[-1].strip()

            if not patches_val and patch_val is not None:
                # Older/degenerate output with no Patches: line still gives a
                # usable list rather than None.
                patches_val = [patch_val]

            return ExternalTemplateResult(
                success=True,
                source=source,
                band=band,
                collection=collection,
                tract=tract_val,
                patch=patch_val,
                patches=patches_val,
                fits_path=fits_path,
            )
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            return ExternalTemplateResult(
                success=False,
                source=source,
                band=band,
                collection=collection,
                error=error_msg,
            )

    except Exception as e:
        return ExternalTemplateResult(
            success=False,
            source=source,
            band=band,
            collection=collection,
            error=str(e),
        )


def check_exists(
    source: str,
    band: str,
    config: "Config",
    collection: str | None = None,
) -> bool:
    """Check if an external-survey template already exists in Butler.

    Args:
        source: External-survey adapter name (e.g. ``ps1``, ``skymapper``)
        band: Local science band
        config: Pipeline configuration
        collection: Collection to check (default: templates/{source}/{band})

    Returns:
        True if template exists
    """
    if collection is None:
        collection = template_external(source, band)

    args = [
        "butler",
        "query-datasets",
        str(config.repo),
        "template_coadd",
        "--collections",
        collection,
    ]

    try:
        result = run_with_stack(args, config, capture_output=True, check=False)
        # If there's output beyond the header, templates exist
        lines = [line for line in result.stdout.strip().split("\n") if line.strip()]
        return len(lines) > 2  # Header is 2 lines
    except Exception:
        return False
