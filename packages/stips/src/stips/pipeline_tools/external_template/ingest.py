#!/usr/bin/env python3
"""In-stack CLI entry point for external-template ingestion.

Downloads a cutout from an external sky survey (``--source``), converts it to
an LSST ``ExposureF`` in nJy, and ingests it into a Butler repository as a
``template_coadd``.

This module imports :mod:`~stips.pipeline_tools.external_template.core`, which
requires the LSST stack — run it inside an activated stack, e.g.::

    python -m stips.pipeline_tools.external_template.ingest \\
        --repo $REPO \\
        --source skymapper \\
        --ra 102.247 --dec -36.0 \\
        --band i \\
        --collection templates/skymapper/i

``--source`` defaults to ``ps1``, so the historical PS1 invocation still works.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import lsst.daf.butler as dafButler

from .core import fits_to_lsst_exposure, ingest_exposure_to_butler
from .sources import TemplateSourceError, get_source

log = logging.getLogger(__name__)


def _resolve_source_band(source_name, local_band):
    """Resolve the survey band to download for a LOCAL science band.

    The band->template policy lives in the active instrument profile's
    ``template_band_maps[source]`` (with the legacy ``ps1_band_map`` as the
    fallback for ``ps1``), so a fork expresses its own filter policy in the
    profile instead of editing the framework.

    Returns the survey band name, or None when ``local_band`` is not eligible
    for this source. If the profile cannot be loaded at all (e.g.
    ``INSTRUMENT_DIR`` unset) this falls back to an identity mapping, preserving
    the tool's historical standalone behavior.
    """
    try:
        from stips.core.config import load_active_profile
        from stips.core.pipeline import template_band_map

        prof = load_active_profile()

        class _ConfigLike:
            """Minimal duck-type so ``template_band_map`` stays the one source
            of truth for the map lookup (it reads only ``config.profile``)."""

            def __init__(self, profile):
                self.profile = profile

        band_map = template_band_map(_ConfigLike(prof), source_name)
    except Exception as e:
        log.warning(
            "Could not load instrument profile (%s); assuming %s band == "
            "local band %r",
            e,
            source_name,
            local_band,
        )
        return local_band

    if local_band in band_map:
        return band_map[local_band]
    log.error(
        "Band %r is not %s-eligible for the active instrument; eligible: %s",
        local_band,
        source_name,
        ", ".join(sorted(band_map)) or "(none configured)",
    )
    return None


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Ingest external survey images as DIA templates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download and ingest a PS1 r-band template (the default source)
  python -m stips.pipeline_tools.external_template.ingest \\
      --repo /path/to/butler/repo \\
      --ra 150.123 --dec 2.456 \\
      --band r \\
      --collection templates/ps1/r

  # SkyMapper DR4, restricted to an MJD window
  python -m stips.pipeline_tools.external_template.ingest \\
      --repo /path/to/butler/repo \\
      --source skymapper \\
      --ra 102.247 --dec -36.0 \\
      --band i \\
      --collection templates/skymapper/i \\
      --mjd-start 58000 --mjd-end 59000

  # Use an existing FITS cutout
  python -m stips.pipeline_tools.external_template.ingest \\
      --repo /path/to/butler/repo \\
      --fits ./ps1_r_myfield.fits \\
      --ra 150.123 --dec 2.456 \\
      --band r \\
      --collection templates/ps1/r \\
      --tract 1099
        """,
    )

    parser.add_argument("--repo", required=True, help="Butler repository path")
    parser.add_argument(
        "--source",
        default="ps1",
        help="External survey to fetch from (default: ps1)",
    )
    parser.add_argument(
        "--ra", type=float, required=True, help="Right ascension (degrees)"
    )
    parser.add_argument(
        "--dec", type=float, required=True, help="Declination (degrees)"
    )
    parser.add_argument(
        "--band",
        required=True,
        # No static choices: eligibility is instrument-specific and lives in the
        # active profile's template_band_maps. Validated at runtime (see
        # _resolve_source_band), which also names the eligible bands on error.
        help="Local science band (must be eligible for --source on this instrument)",
    )
    parser.add_argument(
        "--source-band",
        "--ps1-band",
        dest="source_band",
        # --ps1-band is a deprecated alias kept so the historical PS1 command
        # line (and core/ps1_template.py, rewired in a later task) still works.
        help="Survey band to download (default: auto-map from --band via the profile)",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.2,
        help="Cutout width in degrees (default: 0.2)",
    )
    parser.add_argument(
        "--collection",
        required=True,
        help="Output collection (e.g., templates/skymapper/i)",
    )
    parser.add_argument(
        "--tract", type=int, help="Sky tract (auto-determined if not provided)"
    )
    parser.add_argument(
        "--output-dir",
        default="./external_templates",
        help="Directory for downloaded FITS files",
    )
    parser.add_argument(
        "--fits",
        "--ps1-fits",
        dest="fits",
        help="Use an existing FITS cutout instead of downloading",
    )
    parser.add_argument(
        "--mjd-start",
        type=float,
        help="Earliest frame MJD to consider (sources that expose an epoch)",
    )
    parser.add_argument(
        "--mjd-end",
        type=float,
        help="Latest frame MJD to consider (sources that expose an epoch)",
    )
    parser.add_argument(
        "--degrade-seeing",
        type=float,
        help="Gaussian-convolve the template to this FWHM (arcsec) before ingest",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip download (use with --fits)",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Download only, do not ingest to Butler",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing template in collection if it already exists",
    )
    parser.add_argument(
        "--unity-photocalib",
        action="store_true",
        help="Force PhotoCalib=1.0 instead of using the survey zeropoint",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = _build_parser().parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        source = get_source(args.source)
    except TemplateSourceError as e:
        log.error("%s", e)
        return 1

    # Map local band to the survey band (via the active profile) if not given.
    if args.source_band is None:
        args.source_band = _resolve_source_band(source.name, args.band)
        if args.source_band is None:
            return 1
        log.info(
            "Mapped local band %s -> %s %s", args.band, source.name, args.source_band
        )

    # Step 1: Download, or use an existing FITS cutout.
    if args.fits:
        fits_path = args.fits
        if not os.path.exists(fits_path):
            log.error("FITS file not found: %s", fits_path)
            return 1
    elif not args.skip_download:
        try:
            fits_path = str(
                source.fetch(
                    args.ra,
                    args.dec,
                    args.source_band,
                    args.size,
                    Path(args.output_dir),
                    mjd_start=args.mjd_start,
                    mjd_end=args.mjd_end,
                )
            )
        except TemplateSourceError as e:
            log.error("Failed to fetch a %s template: %s", source.name, e)
            return 1
    else:
        log.error("Must provide --fits when using --skip-download")
        return 1

    # Step 2: Convert to an LSST Exposure.
    exposure = fits_to_lsst_exposure(
        fits_path,
        args.band,
        source,
        degrade_to_fwhm=args.degrade_seeing,
        force_unity_photocalib=args.unity_photocalib,
    )

    # Optional: save as LSST FITS for inspection.
    lsst_fits_path = (
        Path(args.output_dir) / f"lsst_template_{source.name}_{args.band}.fits"
    )
    lsst_fits_path.parent.mkdir(parents=True, exist_ok=True)

    # Only write if it isn't the input file itself (avoid clobbering --fits).
    if not lsst_fits_path.exists() or str(lsst_fits_path) != fits_path:
        if lsst_fits_path.exists():
            try:
                lsst_fits_path.unlink()
            except Exception as e:
                log.warning("Could not remove existing LSST Exposure: %s", e)
        exposure.writeFits(str(lsst_fits_path))
        log.info("Saved LSST Exposure to: %s", lsst_fits_path)
    else:
        log.info("Using existing LSST Exposure: %s", lsst_fits_path)

    if args.skip_ingest:
        log.info("Skipping Butler ingest (--skip-ingest)")
        log.info("Template FITS saved to: %s", lsst_fits_path)
        return 0

    # Step 3: Ingest into Butler.
    butler = dafButler.Butler(args.repo, writeable=True)

    data_id = ingest_exposure_to_butler(
        butler,
        exposure,
        args.ra,
        args.dec,
        args.band,
        args.collection,
        args.tract,
        overwrite=args.overwrite,
    )

    # Record template metadata (non-fatal on failure).
    try:
        from stips.pipeline_tools.template_metadata import TemplateMetadata

        metadata_mgr = TemplateMetadata(args.repo)
        metadata_mgr.record_template(
            collection=args.collection,
            start_date="PS1",
            end_date="PS1",
            tract=str(data_id["tract"]) if "tract" in data_id else None,
            band=args.band,
            description=f"{source.name} {args.source_band}-band template",
            source=args.source,
            ps1_filter=args.source_band,
            ps1_ra=args.ra,
            ps1_dec=args.dec,
            ps1_cutout_size=args.size,
        )
        log.info("Recorded %s template metadata", source.name)
    except Exception as e:
        log.warning("Failed to record metadata (non-fatal): %s", e)

    log.info("=" * 60)
    log.info("SUCCESS: %s template ingested!", source.name)
    log.info("  Collection: %s", args.collection)
    log.info("  Data ID: %s", data_id)
    log.info("  FITS file: %s", lsst_fits_path)
    log.info(
        "  %s filter: %s -> local band %s", source.name, args.source_band, args.band
    )
    log.info("")
    log.info("Next steps:")
    log.info(
        "  1. Verify template: butler query-datasets %s template_coadd \\", args.repo
    )
    log.info("       --collections %s", args.collection)
    log.info("  2. Run DIA against that collection with: stips dia <night> \\")
    log.info("       --template %s", args.collection)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
