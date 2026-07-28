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

from . import imaging
from .core import fits_to_lsst_exposure, ingest_exposure_to_butler
from .sources import TemplateSourceError, get_source

log = logging.getLogger(__name__)


def _profile_fov_arcmin():
    """Approximate science FOV (arcmin) from the active instrument profile.

    This is the source of the value behind the template-coverage warning: a
    cutout narrower than the science field leaves dithered pointings with no
    PSF-matching kernel candidates. Profiles that do not declare
    ``fov_arcmin`` return None, which keeps the warning silent rather than
    guessing a field size.
    """
    try:
        from stips.core.config import load_active_profile

        prof = load_active_profile()
    except Exception as e:
        log.debug("Could not load instrument profile for the FOV warning: %s", e)
        return None

    value = getattr(prof, "fov_arcmin", None)
    if value is None:
        return None
    try:
        fov = float(value)
    except (TypeError, ValueError):
        log.warning("Profile fov_arcmin is not numeric (%r); ignoring it.", value)
        return None
    return fov if fov > 0 else None


def _resolve_source_band(source, local_band):
    """Resolve the survey band to download for a LOCAL science band.

    The band->template policy lives in the active instrument profile's
    ``template_band_maps[source]`` (with the legacy ``ps1_band_map`` as the
    fallback for ``ps1``), so a fork expresses its own filter policy in the
    profile instead of editing the framework. Routed through the adapter's own
    ``TemplateSource.band_map(config)`` (``sources/base.py``) rather than
    duck-typing ``template_band_map`` directly here, so that protocol method
    has a real production caller instead of only being implemented.

    Args:
        source: The resolved ``TemplateSource`` adapter (``get_source(name)``).
        local_band: Local science band requested on the command line.

    Returns the survey band name, or None when ``local_band`` is not eligible
    for this source. If the profile cannot be loaded at all (e.g.
    ``INSTRUMENT_DIR`` unset) this falls back to an identity mapping, preserving
    the tool's historical standalone behavior.
    """
    try:
        from stips.core.config import load_active_profile

        prof = load_active_profile()

        class _ConfigLike:
            """Minimal duck-type so ``TemplateSource.band_map`` stays the one
            source of truth for the map lookup (it reads only
            ``config.profile``)."""

            def __init__(self, profile):
                self.profile = profile

        band_map = source.band_map(_ConfigLike(prof))
    except Exception as e:
        log.warning(
            "Could not load instrument profile (%s); assuming %s band == "
            "local band %r",
            e,
            source.name,
            local_band,
        )
        return local_band

    if local_band in band_map:
        return band_map[local_band]
    log.error(
        "Band %r is not %s-eligible for the active instrument; eligible: %s",
        local_band,
        source.name,
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
        args.source_band = _resolve_source_band(source, args.band)
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
                    fov_arcmin=_profile_fov_arcmin(),
                )
            )
        except TemplateSourceError as e:
            log.error("Failed to fetch a %s template: %s", source.name, e)
            return 1

        # Step 1b: Validate what was actually delivered, for EVERY source. An
        # adapter's byte-count floor only proves the response was not an error
        # page; a valid-but-edge-trimmed survey frame would otherwise be
        # converted and ingested silently, and only surface as a DIA
        # NoKernelCandidatesError much later.
        if not getattr(source, "fetch_validates_cutout", False):
            reason = imaging.validate_cutout(
                fits_path,
                args.ra,
                args.dec,
                imaging.effective_cutout_size(
                    args.size, getattr(source, "max_cutout_deg", None)
                ),
            )
            if reason:
                log.error(
                    "Rejecting the %s cutout %s: %s", source.name, fits_path, reason
                )
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

    # Record template metadata (non-fatal on failure). The date-range fields
    # are a sentinel, not a real observation date range, for every external
    # survey: "PS1" is kept literally for PS1 (so existing recorded metadata
    # stays consistent); every other source gets the source-agnostic
    # "EXTERNAL" sentinel rather than the misleading literal "PS1".
    try:
        from stips.pipeline_tools.template_metadata import TemplateMetadata

        date_sentinel = "PS1" if source.name == "ps1" else "EXTERNAL"
        metadata_mgr = TemplateMetadata(args.repo)
        metadata_mgr.record_template(
            collection=args.collection,
            start_date=date_sentinel,
            end_date=date_sentinel,
            tract=str(data_id["tract"]) if "tract" in data_id else None,
            band=args.band,
            description=f"{source.name} {args.source_band}-band template",
            source=source.name,
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
