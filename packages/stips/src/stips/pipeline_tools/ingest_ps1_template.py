#!/usr/bin/env python3
"""
Ingest Pan-STARRS1 (PS1) images as templates for DIA.

This script downloads PS1 stacked images from the STScI MAST archive,
converts them to LSST Exposure format with proper WCS and PhotoCalib,
and ingests them into a Butler repository as template_coadd datasets.

Usage:
    python ingest_ps1_template.py \
        --repo $REPO \
        --ra 150.123 \
        --dec 2.456 \
        --band r \
        --size 0.2 \
        --collection templates/ps1/r \
        --output-dir ./ps1_templates

Requirements:
    - astropy
    - astroquery
    - requests
    - lsst.afw.image
    - lsst.daf.butler
    - lsst.geom
"""

import argparse
import logging
import math
import os
import sys
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

try:
    import lsst.afw.detection as afwDetection
    import lsst.afw.image as afwImage
    import lsst.afw.math as afwMath
    import lsst.daf.butler as dafButler

    try:
        # Preferred public path
        from lsst.daf.butler.registry import ConflictingDefinitionError
    except Exception:
        try:
            # Older releases expose it from _exceptions
            from lsst.daf.butler.registry._exceptions import (
                ConflictingDefinitionError,
            )
        except Exception:

            class ConflictingDefinitionError(Exception):
                """Fallback when LSST ConflictingDefinitionError is unavailable."""

                pass

    import lsst.geom as geom
    from lsst.afw.image import PhotoCalib
except ImportError:
    print("ERROR: LSST stack not found. Make sure you've run 'setup lsst_distrib'")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# The download/metadata helpers (download_ps1_cutout and its Method 1/2/3
# fallbacks, open_ps1_fits, collect_ps1_metadata, write_ps1_cutout), the
# PS1_ZEROPOINTS table, and _resolve_ps1_band now live in
# stips.pipeline_tools.external_template.sources.ps1 — an astropy-only module
# (no lsst import) shared with the generic external-template framework. Only
# the names still called from this module are imported back here
# (download_ps1_via_fitscut/download_ps1_via_ps1filenames/open_ps1_fits/
# write_ps1_cutout have no remaining callers in this file).
# ps1_file_covers_target / ps1_file_meets_requested_size: kept as aliases of
# the extracted imaging.* implementations. Tests
# (tests/test_ps1_templates.py::TestPS1CutoutValidation) still call these
# names on this module, so they must stay importable here even though the
# implementations now live in the shared imaging module.
from stips.pipeline_tools.external_template.imaging import (  # noqa: E402
    file_covers_target as ps1_file_covers_target,  # noqa: F401
)
from stips.pipeline_tools.external_template.imaging import (  # noqa: E402
    file_meets_requested_size as ps1_file_meets_requested_size,  # noqa: F401
)
from stips.pipeline_tools.external_template.sources.ps1 import (  # noqa: E402
    PS1_ZEROPOINTS,
    _resolve_ps1_band,
    collect_ps1_metadata,
    download_ps1_cutout,
)

# PS1 effective wavelengths (Angstroms) for colorterm calculations
PS1_EFFECTIVE_WAVELENGTHS = {
    "g": 4866,
    "r": 6215,
    "i": 7545,
    "z": 8679,
    "y": 9633,
}


def zeropoint_to_calibration_mean(ps1_zp):
    """
    Convert a PS1 AB zeropoint to a PhotoCalib calibration mean (nJy/ADU).
    """
    ab_zero_flux_njy = 3631e9  # 3631 Jy in nJy
    return ab_zero_flux_njy * 10 ** (-0.4 * ps1_zp)


# ps1_file_covers_target, ps1_file_meets_requested_size, find_first_image_hdu,
# open_ps1_fits, collect_ps1_metadata, write_ps1_cutout, download_ps1_cutout,
# download_ps1_via_fitscut, and download_ps1_via_ps1filenames moved to
# stips.pipeline_tools.external_template.sources.ps1 (see the import above).
# The coverage/size checks now live as imaging.file_covers_target /
# imaging.file_meets_requested_size in that module.


def degrade_exposure_psf(exposure, target_fwhm_arcsec, current_fwhm_arcsec):
    """
    Convolve exposure to a target seeing FWHM (arcsec) using a Gaussian kernel.
    """
    if target_fwhm_arcsec <= current_fwhm_arcsec:
        log.info(
            f'Requested degrade seeing to {target_fwhm_arcsec:.2f}", '
            f'but current FWHM is {current_fwhm_arcsec:.2f}"; skipping.'
        )
        return exposure

    try:
        bbox = exposure.getBBox()
        center = geom.Point2D(bbox.getCenterX(), bbox.getCenterY())
        pix_scale = exposure.getWcs().getPixelScale(center).asArcseconds()
    except Exception as e:
        log.warning(f"Could not measure pixel scale for PSF degradation: {e}")
        return exposure

    if pix_scale <= 0:
        log.warning("Pixel scale is non-positive; skipping PSF degradation")
        return exposure

    target_sigma_pix = (target_fwhm_arcsec / pix_scale) / 2.3548
    current_sigma_pix = (current_fwhm_arcsec / pix_scale) / 2.3548
    blur_sigma_pix = math.sqrt(max(target_sigma_pix**2 - current_sigma_pix**2, 0.0))

    if blur_sigma_pix <= 0:
        log.info(
            f"Target sigma {target_sigma_pix:.3f} <= current sigma "
            f"{current_sigma_pix:.3f}; no convolution applied"
        )
        return exposure

    kernel_size = max(7, int(blur_sigma_pix * 6) | 1)  # 3-sigma kernel on each side
    log.info(
        f'Convolving template to ~{target_fwhm_arcsec:.2f}" '
        f"(add sigma={blur_sigma_pix:.2f} pix, kernel={kernel_size}x{kernel_size})"
    )

    gauss1d = afwMath.GaussianFunction1D(blur_sigma_pix)
    kernel = afwMath.SeparableKernel(kernel_size, kernel_size, gauss1d, gauss1d)
    conv_ctrl = afwMath.ConvolutionControl()
    conv_ctrl.setDoCopyEdge(True)

    masked_image = exposure.getMaskedImage()
    convolved = masked_image.clone()
    afwMath.convolve(convolved, masked_image, kernel, conv_ctrl)
    exposure.setMaskedImage(convolved)

    # Update PSF to reflect degraded seeing
    new_sigma_pix = target_sigma_pix
    psf_size = max(21, kernel_size)
    exposure.setPsf(afwDetection.GaussianPsf(psf_size, psf_size, new_sigma_pix))

    return exposure


def convert_ps1_to_lsst_exposure(
    ps1_fits_path, nickel_band, degrade_to_fwhm=None, force_unity_photoCalib=False
):
    """
    Convert PS1 FITS image to LSST Exposure format.

    Parameters
    ----------
    ps1_fits_path : str
        Path to PS1 FITS file
    nickel_band : str
        Target Nickel band (b, v, r, i)

    Returns
    -------
    lsst.afw.image.ExposureF
        LSST Exposure with WCS and PhotoCalib set
    """
    log.info(f"Converting PS1 image to LSST Exposure (target band: {nickel_band})")

    # Read PS1 FITS and pick the first HDU that actually contains image data.
    with fits.open(ps1_fits_path) as hdul:
        image_hdu = None
        for idx, hdu in enumerate(hdul):
            data = getattr(hdu, "data", None)
            if data is not None and isinstance(data, np.ndarray) and data.ndim >= 2:
                image_hdu = hdu
                image_idx = idx
                break

        if image_hdu is None:
            raise ValueError(f"No image HDU found in PS1 file: {ps1_fits_path}")

        ps1_data = image_hdu.data
        ps1_header = image_hdu.header
        log.info(
            f"Using HDU {image_idx} ('{getattr(image_hdu, 'name', '')}') with shape {ps1_data.shape}"
        )

        # Extract WCS from the image HDU
        ps1_wcs = WCS(ps1_header)
        center = ps1_wcs.all_pix2world(ps1_data.shape[1] / 2, ps1_data.shape[0] / 2, 0)
        log.info(f"  PS1 image center (WCS): RA={center[0]:.4f}, Dec={center[1]:.4f}")

        metadata = collect_ps1_metadata(hdul)

        # Get PS1 filter from header (if available)
        ps1_filter = metadata.get("FILTER", metadata.get("FILTNAM", "r"))
        ps1_filter = str(ps1_filter).strip().lower()
        if ps1_filter not in PS1_ZEROPOINTS:
            log.warning(f"Unknown PS1 filter '{ps1_filter}', assuming 'r'")
            ps1_filter = "r"

        # Extract PS1 zeropoint from FITS header (various possible keywords)
        ps1_zp_keywords = ["ZPT", "FPA.ZP", "MAGZERO", "MAGZPT"]
        ps1_zp = None
        for keyword in ps1_zp_keywords:
            if keyword in metadata:
                ps1_zp = float(metadata[keyword])
                log.info(f"Found PS1 zeropoint in header[{keyword}]: {ps1_zp:.3f}")
                break

        if ps1_zp is None:
            ps1_zp = PS1_ZEROPOINTS.get(ps1_filter, 25.0)
            log.warning(
                f"No zeropoint in FITS header, using default for {ps1_filter}: {ps1_zp:.3f}"
            )

        log.info(f"PS1 filter: {ps1_filter}, image shape: {ps1_data.shape}")
        log.info(f"PS1 zeropoint (AB mag): {ps1_zp:.3f}")

        # Convert WCS to LSST format
        lsst_wcs = convert_astropy_wcs_to_lsst(ps1_wcs)

        # Handle NaN and bad values
        # NOTE: Do NOT mask negative pixels - sky-subtracted images legitimately have negative values
        bad_mask = ~np.isfinite(ps1_data)
        ps1_data = np.nan_to_num(ps1_data, nan=0.0, posinf=0.0, neginf=0.0)

        # Compute the nJy-per-ADU calibration factor from the PS1 zeropoint.
        if force_unity_photoCalib:
            calibration_mean = 1.0
            log.info("PhotoCalib forced to unity (no flux conversion)")
        else:
            calibration_mean = zeropoint_to_calibration_mean(ps1_zp)
            log.info(
                f"PhotoCalib from PS1 zeropoint: {calibration_mean:.3e} nJy/ADU "
                f"(zp={ps1_zp:.3f})"
            )

        # Pre-calibrate pixel values to nanojansky.
        #
        # The LSST DIA subtractImages task does NOT normalize flux scales
        # before kernel fitting — the PSF-matching kernel absorbs any
        # template-to-science flux ratio.  When the template is stored in
        # raw ADU (PhotoCalib ≈ 363 nJy/ADU) but science PVIs are already
        # in nJy (PhotoCalib = 1.0), the kernel must absorb a ~363× scale
        # factor on top of the PSF shape change.  This causes numerical
        # instability (high condition numbers, unreliable kernel sums) and
        # systematically biased difference-image photometry.
        #
        # Fix: multiply pixel values by calibration_mean here so the
        # template is stored in nJy, matching science PVIs.  PhotoCalib is
        # then set to 1.0 (identity).  The DIA kernel only needs to handle
        # PSF matching (kernel_sum ≈ 1.0).
        if calibration_mean != 1.0:
            log.info(
                f"Pre-calibrating template pixels to nJy "
                f"(multiplying by {calibration_mean:.3e})"
            )
            ps1_data = ps1_data * calibration_mean

        # Create LSST MaskedImage
        masked_image = afwImage.MaskedImageF(ps1_data.shape[1], ps1_data.shape[0])
        masked_image.image.array[:, :] = ps1_data.astype(np.float32)

        # Set mask for zero/bad pixels
        masked_image.mask.array[bad_mask] = masked_image.mask.getPlaneBitMask("BAD")

        # Set variance (improved estimate from image statistics, in nJy² units)
        good_pixels = ps1_data[~bad_mask]
        if len(good_pixels) > 100:
            # Use median absolute deviation for robust variance estimate
            median_val = np.median(good_pixels)
            mad = np.median(np.abs(good_pixels - median_val))
            variance_estimate = (1.4826 * mad) ** 2  # Convert MAD to std dev
            # Add Poisson noise estimate
            variance_estimate = np.maximum(variance_estimate, np.abs(good_pixels))
            masked_image.variance.array[:, :] = variance_estimate.mean()
            log.info(f"Variance estimate: {variance_estimate.mean():.2f} (from MAD)")
        else:
            variance_estimate = 1.0
            masked_image.variance.array[:, :] = variance_estimate
            log.warning("Too few good pixels for variance estimate, using 1.0")

        # Create Exposure
        exposure = afwImage.ExposureF(masked_image)
        exposure.setWcs(lsst_wcs)

        # Set PhotoCalib to identity — pixels are already in nJy.
        exposure.setPhotoCalib(PhotoCalib(1.0))

        # Set filter
        filter_label = afwImage.FilterLabel(band=nickel_band)
        exposure.setFilter(filter_label)

        # Attach a simple Gaussian PSF so downstream warping/subtraction have a PSF
        fwhm_arcsec = 1.2  # typical PS1 stack seeing
        sigma_pix = 1.3
        try:
            exp_bbox = exposure.getBBox()
            center = geom.Point2D(exp_bbox.getCenterX(), exp_bbox.getCenterY())
            pix_scale = exposure.getWcs().getPixelScale(center).asArcseconds()
            if pix_scale > 0:
                sigma_pix = (fwhm_arcsec / pix_scale) / 2.3548
        except Exception as e:
            log.warning(
                f"Could not compute pixel scale for PSF; using default sigma {sigma_pix:.2f} ({e})"
            )

        psf = afwDetection.GaussianPsf(21, 21, sigma_pix)
        exposure.setPsf(psf)
        log.info(
            f'  Set synthetic PSF: FWHM~{fwhm_arcsec:.2f}" -> sigma={sigma_pix:.2f} pix'
        )

        # Optionally degrade PS1 seeing to be closer to Nickel for more stable kernels
        if degrade_to_fwhm is not None:
            exposure = degrade_exposure_psf(
                exposure,
                target_fwhm_arcsec=degrade_to_fwhm,
                current_fwhm_arcsec=fwhm_arcsec,
            )
            fwhm_arcsec = max(fwhm_arcsec, degrade_to_fwhm)

        # Add metadata
        exposure_info = exposure.getInfo()
        exposure_info.setMetadata(exposure.getMetadata())
        metadata = exposure.getMetadata()
        metadata.set("PS1_FILTER", ps1_filter)
        metadata.set("PS1_ZEROPOINT", ps1_zp)
        metadata.set("PS1_SOURCE", ps1_fits_path)

        log.info(f"Created LSST Exposure: {exposure.getBBox()}")
        log.info(f"  WCS: {lsst_wcs.getPixelOrigin()}")
        log.info("  Pixels: nJy (pre-calibrated, PhotoCalib=1.0)")
        log.info(
            f"  Original ZP={ps1_zp:.3f} → calibration={calibration_mean:.3e} nJy/ADU"
        )
        log.info(f"  Masked pixels: {np.sum(bad_mask)} / {bad_mask.size}")

        return exposure


def convert_astropy_wcs_to_lsst(astropy_wcs):
    """
    Convert Astropy WCS to LSST WCS.

    Parameters
    ----------
    astropy_wcs : astropy.wcs.WCS
        Astropy WCS object

    Returns
    -------
    lsst.afw.geom.SkyWcs
        LSST WCS object
    """
    from lsst.afw.geom import makeSkyWcs

    # Try to create from FITS header
    try:
        # Convert WCS to FITS header
        header = astropy_wcs.to_header()

        # Create metadata for makeSkyWcs
        from lsst.daf.base import PropertyList

        metadata = PropertyList()
        for key, value in header.items():
            if key and value is not None:
                # makeSkyWcs expects native Python scalars/strings
                if isinstance(value, (int, float, bool)):
                    metadata.set(key, value)
                elif isinstance(value, str):
                    metadata.set(key, str(value))

        # Create LSST WCS from metadata
        lsst_wcs = makeSkyWcs(metadata)

        return lsst_wcs

    except Exception as e:
        log.error(f"Failed to convert WCS: {e}")
        raise


def reproject_to_patch(exposure, patch_info):
    """
    Reproject exposure to match patch WCS and bounding box.

    This ensures the PS1 template has the exact geometry expected by the DIA pipeline.

    Parameters
    ----------
    exposure : lsst.afw.image.ExposureF
        Input exposure (PS1 template)
    patch_info : lsst.skymap.PatchInfo
        Target patch from skymap

    Returns
    -------
    lsst.afw.image.ExposureF
        Reprojected exposure matching patch geometry
    """
    from lsst.afw.math import WarpingControl, warpExposure

    log.info("Reprojecting PS1 template to match patch geometry...")

    # Get patch WCS and bounding box
    patch_wcs = patch_info.getWcs()
    patch_bbox = patch_info.getOuterBBox()

    log.info(f"  Patch bbox: {patch_bbox}")
    log.info(f"  Input exposure bbox: {exposure.getBBox()}")

    # Create output exposure with patch geometry
    reprojected = afwImage.ExposureF(patch_bbox)
    reprojected.setWcs(patch_wcs)
    reprojected.setFilter(exposure.getFilter())
    reprojected.setPhotoCalib(exposure.getPhotoCalib())
    # Preserve PSF if present (we add a synthetic PSF earlier)
    try:
        if exposure.getPsf() is not None:
            reprojected.setPsf(exposure.getPsf())
            log.info("  Carried PSF onto reprojected exposure")
    except Exception as e:
        log.warning(f"  Could not copy PSF to reprojected exposure: {e}")

    # Warp input exposure onto patch geometry
    warping_control = WarpingControl("lanczos4")
    # Set growth to allow proper interpolation at edges
    warping_control.setGrowFullMask(0)  # Don't grow mask during warping
    warping_control.setMaskWarpingKernelName("bilinear")  # Faster mask warping

    # Perform the warp
    warpExposure(reprojected, exposure, warping_control)

    log.info(f"  Reprojected exposure bbox: {reprojected.getBBox()}")

    # Check mask statistics
    mask = reprojected.mask.array
    valid_mask = mask == 0
    edge_bit = reprojected.mask.getPlaneBitMask("EDGE")
    no_data_bit = reprojected.mask.getPlaneBitMask("NO_DATA")

    log.info(f"  Valid pixels (mask==0): {np.sum(valid_mask)} / {mask.size}")
    log.info(f"  Pixels with EDGE set: {np.sum((mask & edge_bit) != 0)}")
    log.info(f"  Pixels with NO_DATA set: {np.sum((mask & no_data_bit) != 0)}")
    log.info(f"  Finite image pixels: {np.sum(np.isfinite(reprojected.image.array))}")

    # CRITICAL FIX: Clear EDGE for pixels with finite warped data.
    # After warping, EDGE is set on interpolated pixels (these are valid for templates).
    # Keep NO_DATA to avoid unmasking pixels outside the input footprint.
    has_finite_data = np.isfinite(reprojected.image.array)

    # Clear EDGE for any finite pixel (interpolated pixels are still valid for templates).
    reprojected.mask.array[has_finite_data] &= ~edge_bit

    valid_after = reprojected.mask.array == 0
    log.info(
        f"  Valid pixels after EDGE clearing: {np.sum(valid_after)} / {mask.size} ({100*np.sum(valid_after)/mask.size:.1f}%)"
    )

    # Estimate coverage as finite pixels not marked NO_DATA.
    has_coverage = has_finite_data & ((mask & no_data_bit) == 0)
    log.info(
        f"  Patch coverage: {100*np.sum(has_coverage)/mask.size:.1f}% of pixels have warped data"
    )

    return reprojected


def ingest_exposure_to_butler(
    butler, exposure, ra, dec, band, collection, tract=None, overwrite=False
):
    """
    Ingest LSST Exposure into Butler as template_coadd.

    Parameters
    ----------
    butler : lsst.daf.butler.Butler
        Butler instance
    exposure : lsst.afw.image.ExposureF
        Exposure to ingest
    ra : float
        Center RA in degrees
    dec : float
        Center Dec in degrees
    band : str
        Nickel band (b, v, r, i)
    collection : str
        Output collection name
    tract : int, optional
        Tract number (will auto-determine if None)
    overwrite : bool, optional
        If True, allow replacing an existing template in the collection. Default False.

    Returns
    -------
    dict
        Data ID of ingested template
    """
    log.info(f"Ingesting exposure to Butler collection: {collection}")

    # Ensure the dataset type exists (fresh repos may not have template_coadd yet).
    from lsst.daf.butler import DatasetType

    dims = None
    try:
        dt = butler.registry.getDatasetType("template_coadd")
        dims = tuple(
            dt.dimensions.names
        )  # Use .names to get iterable list of dimension names
        log.info(f"Found existing template_coadd with dimensions: {dims}")
    except Exception:
        # Default to dimensions without instrument (matches DIA pipeline expectations)
        # PS1 templates are external, so they shouldn't have instrument dimension
        dims = ("skymap", "tract", "patch", "band")
        log.info(
            "Dataset type 'template_coadd' not found; registering it with dims %s", dims
        )
        dt = DatasetType(
            name="template_coadd",
            dimensions=dims,
            storageClass="ExposureF",
            universe=butler.dimensions,
        )
        butler.registry.registerDatasetType(dt)

    # Ensure target run/collection exists (registerRun is idempotent).
    try:
        butler.registry.registerRun(collection)
        log.info(f"Registered collection: {collection}")
    except Exception as e:
        # If it already exists (or is a chain), this will fail harmlessly.
        log.debug(f"Collection registration note: {e}")
        pass

    # Get skymap to determine tract/patch.
    # Prefer the active instrument's profile values, allowing env overrides.
    # Wrapped in try/except so this script stays runnable without a loaded
    # profile — but when the profile is unavailable AND no env override is
    # given, fail loud rather than silently assuming Nickel (F-043).
    profile_error: Exception | None = None
    try:
        from stips.core.config import load_active_profile

        prof = load_active_profile()
        prof_skymap_name = prof.skymap_name
        prof_skymap_collection = prof.skymap_collection
        prof_instrument = prof.name
    except Exception as exc:
        prof_skymap_name = None
        prof_skymap_collection = None
        prof_instrument = None
        profile_error = exc

    _profile_hint = (
        f"instrument profile not loaded ({profile_error})"
        if profile_error is not None
        else "the loaded instrument profile does not define it"
    )

    skymap_name = os.environ.get("SKYMAP_NAME") or prof_skymap_name
    if not skymap_name:
        raise RuntimeError(
            f"skymap name unavailable: {_profile_hint} and SKYMAP_NAME is "
            "unset; set INSTRUMENT_DIR to instruments/<name>/ (containing "
            "profile.py) in your config env: block, or export SKYMAP_NAME."
        )
    if not prof_instrument:
        raise RuntimeError(
            f"instrument name unavailable: {_profile_hint}; set INSTRUMENT_DIR "
            "to instruments/<name>/ (containing profile.py) in your config "
            "env: block."
        )
    skymap_collections = (
        os.environ.get("SKYMAPS_CHAIN") or prof_skymap_collection or "skymaps"
    )
    skymap_collections = [
        c.strip() for c in skymap_collections.split(",") if c.strip()
    ] or ["skymaps"]

    log.info(f"Looking for skymap '{skymap_name}' in collections: {skymap_collections}")

    try:
        skymap = butler.get(
            "skyMap", skymap=skymap_name, collections=skymap_collections
        )
        log.info(f"Successfully loaded skymap: {skymap_name}")
    except Exception as e:
        log.error(f"Failed to get skymap '{skymap_name}': {e}")
        log.info("Available skymaps:")
        try:
            for ref in butler.registry.queryDatasets("skyMap"):
                log.info(f"  - {ref.dataId['skymap']}")
        except Exception:
            log.warning("Could not query available skymaps")
        raise RuntimeError(
            f"Skymap '{skymap_name}' not found. Set SKYMAP_NAME environment variable or create skymap."
        )

    # Find tract/patch from coordinates
    coord = geom.SpherePoint(ra, dec, geom.degrees)

    if tract is None:
        tract_info = skymap.findTract(coord)
        tract = tract_info.getId()
        log.info(f"Auto-determined tract: {tract}")
    else:
        tract_info = skymap[tract]
        log.info(f"Using specified tract: {tract}")

    patch_info = tract_info.findPatch(coord)
    patch = patch_info.getSequentialIndex()

    log.info(f"Target tract={tract}, patch={patch}")

    # Verify the exposure WCS covers the patch
    exp_bbox = exposure.getBBox()
    exp_wcs = exposure.getWcs()
    if exp_wcs is not None:
        center_pixel = geom.Point2D(exp_bbox.getCenterX(), exp_bbox.getCenterY())
        center_sky = exp_wcs.pixelToSky(center_pixel)
        log.info(
            f"Input exposure center: RA={center_sky.getRa().asDegrees():.4f}, "
            f"Dec={center_sky.getDec().asDegrees():.4f}"
        )
    else:
        log.warning("Exposure has no WCS!")

    # CRITICAL: Reproject PS1 exposure to match patch geometry
    # This ensures the template has the exact WCS and bounding box expected by DIA pipeline
    log.info("Reprojecting PS1 template to patch geometry...")
    exposure = reproject_to_patch(exposure, patch_info)

    # Build data ID matching dataset type dimensions
    data_id = {
        "skymap": skymap_name,
        "tract": tract,
        "patch": patch,
        "band": band,
    }

    # Only add instrument/physical_filter if they're in the dataset type dimensions
    if dims and "instrument" in dims:
        data_id["instrument"] = prof_instrument
        log.info("Including 'instrument' dimension in data ID")
    if dims and "physical_filter" in dims:
        data_id["physical_filter"] = band.upper()
        log.info("Including 'physical_filter' dimension in data ID")

    # Check if template already exists
    try:
        existing_refs = list(
            butler.registry.queryDatasets(
                "template_coadd", collections=[collection], dataId=data_id
            )
        )
        if existing_refs and not overwrite:
            log.info(
                f"Template already exists for {data_id} in {collection}; skipping ingest"
            )
            return data_id
        if existing_refs and overwrite:
            log.info(
                f"Template already exists for {data_id} in {collection}; overwriting"
            )
            try:
                butler.pruneDatasets(existing_refs, purge=True, unstore=True)
                log.info(
                    "Pruned %d existing template_coadd dataset(s)", len(existing_refs)
                )
            except Exception as e:
                log.warning(
                    "Failed to prune existing template_coadd; will attempt to overwrite anyway: %s",
                    e,
                )
    except Exception as e:
        log.debug(f"Could not check for existing template: {e}")

    # Put exposure into Butler
    try:
        butler.put(exposure, "template_coadd", dataId=data_id, run=collection)
        log.info(f"Successfully ingested template_coadd with dataId: {data_id}")

        # Verify ingestion
        try:
            retrieved = butler.get(
                "template_coadd", dataId=data_id, collections=[collection]
            )
            log.info(f"Verified: template is retrievable (bbox: {retrieved.getBBox()})")
        except Exception as e:
            log.error(f"WARNING: Failed to verify ingestion: {e}")

    except Exception as e:
        if isinstance(e, ConflictingDefinitionError):
            log.info(
                "Template already exists in collection; treating as success "
                "(pass --overwrite to replace)"
            )
            return data_id
        log.error(f"Failed to ingest exposure: {e}")
        log.error(f"Data ID: {data_id}")
        log.error(f"Collection: {collection}")
        raise

    return data_id


def main():
    parser = argparse.ArgumentParser(
        description="Ingest PS1 images as templates for Nickel DIA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download and ingest PS1 r-band template
  python ingest_ps1_template.py \\
      --repo /path/to/butler/repo \\
      --ra 150.123 --dec 2.456 \\
      --band r \\
      --collection templates/ps1/r

  # Use existing PS1 FITS file
  python ingest_ps1_template.py \\
      --repo /path/to/butler/repo \\
      --ps1-fits ./ps1_r_myfield.fits \\
      --ra 150.123 --dec 2.456 \\
      --band r \\
      --collection templates/ps1/r \\
      --tract 1099
        """,
    )

    parser.add_argument("--repo", required=True, help="Butler repository path")
    parser.add_argument(
        "--ra", type=float, required=True, help="Right ascension (degrees)"
    )
    parser.add_argument(
        "--dec", type=float, required=True, help="Declination (degrees)"
    )
    parser.add_argument(
        "--band",
        required=True,
        # No static choices: PS1 eligibility is instrument-specific and lives in
        # the active profile's ps1_band_map. Validated at runtime (see
        # _resolve_ps1_band), which also names the eligible bands on error.
        help="Local science band (must be PS1-eligible for the active instrument)",
    )
    parser.add_argument(
        "--ps1-band",
        choices=["g", "r", "i", "z", "y"],
        help="PS1 band to download (default: auto-map from --band via the profile)",
    )
    parser.add_argument(
        "--size",
        type=float,
        default=0.2,
        help="Cutout width in degrees (default: 0.2)",
    )
    parser.add_argument(
        "--collection", required=True, help="Output collection (e.g., templates/ps1/r)"
    )
    parser.add_argument(
        "--tract", type=int, help="Sky tract (auto-determined if not provided)"
    )
    parser.add_argument(
        "--output-dir",
        default="./ps1_templates",
        help="Directory for downloaded FITS files",
    )
    parser.add_argument(
        "--ps1-fits", help="Use existing PS1 FITS file instead of downloading"
    )
    parser.add_argument(
        "--degrade-seeing",
        type=float,
        help="Gaussian-convolve PS1 template to this FWHM (arcsec) before ingest",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip download (use with --ps1-fits)",
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
        help="Force PhotoCalib=1.0 instead of using PS1 zeropoint",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Map local band to PS1 band (via the active profile) if not specified.
    if args.ps1_band is None:
        args.ps1_band = _resolve_ps1_band(args.band)
        if args.ps1_band is None:
            sys.exit(1)
        log.info(f"Mapped local band {args.band} → PS1 {args.ps1_band}")

    # Step 1: Download or use existing PS1 FITS
    if args.ps1_fits:
        ps1_fits_path = args.ps1_fits
        if not os.path.exists(ps1_fits_path):
            log.error(f"PS1 FITS file not found: {ps1_fits_path}")
            sys.exit(1)
    elif not args.skip_download:
        ps1_fits_path = download_ps1_cutout(
            args.ra, args.dec, args.ps1_band, args.size, args.output_dir
        )

        if ps1_fits_path is None:
            log.error("Failed to download PS1 image")
            sys.exit(1)
    else:
        log.error("Must provide --ps1-fits when using --skip-download")
        sys.exit(1)

    # Step 2: Convert to LSST Exposure
    exposure = convert_ps1_to_lsst_exposure(
        ps1_fits_path,
        args.band,
        degrade_to_fwhm=args.degrade_seeing,
        force_unity_photoCalib=args.unity_photocalib,
    )

    # Optional: Save as LSST FITS for inspection
    lsst_fits_path = Path(args.output_dir) / f"lsst_template_{args.band}.fits"

    # Only write if it doesn't already exist (avoid overwriting when using --ps1-fits)
    if not lsst_fits_path.exists() or str(lsst_fits_path) != ps1_fits_path:
        if lsst_fits_path.exists():
            try:
                lsst_fits_path.unlink()
            except Exception as e:
                log.warning(f"Could not remove existing LSST Exposure: {e}")
        exposure.writeFits(str(lsst_fits_path))
        log.info(f"Saved LSST Exposure to: {lsst_fits_path}")
    else:
        log.info(f"Using existing LSST Exposure: {lsst_fits_path}")

    if args.skip_ingest:
        log.info("Skipping Butler ingest (--skip-ingest)")
        log.info(f"Template FITS saved to: {lsst_fits_path}")
        return 0

    # Step 3: Ingest to Butler
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

    # Record PS1 template metadata
    try:
        from stips.pipeline_tools.template_metadata import (
            TemplateMetadata,
        )

        metadata_mgr = TemplateMetadata(args.repo)
        metadata_mgr.record_template(
            collection=args.collection,
            start_date="PS1",
            end_date="PS1",
            tract=str(data_id["tract"]) if "tract" in data_id else None,
            band=args.band,
            description=f"PS1 {args.ps1_band}-band template",
            source="ps1",
            ps1_filter=args.ps1_band,
            ps1_ra=args.ra,
            ps1_dec=args.dec,
            ps1_cutout_size=args.size,
        )
        log.info("Recorded PS1 template metadata")
    except Exception as e:
        log.warning(f"Failed to record metadata (non-fatal): {e}")

    log.info("=" * 60)
    log.info("SUCCESS: PS1 template ingested!")
    log.info(f"  Collection: {args.collection}")
    log.info(f"  Data ID: {data_id}")
    log.info(f"  FITS file: {lsst_fits_path}")
    log.info(f"  PS1 filter: {args.ps1_band} → local band {args.band}")
    log.info("")
    log.info("Next steps:")
    log.info(
        f"  1. Verify template: butler query-datasets {args.repo} template_coadd \\"
    )
    log.info(f"       --collections {args.collection}")
    log.info("  2. Run DIA: ./scripts/pipeline/40_diff_imaging.sh \\")
    log.info(f"       --night YYYYMMDD --template {args.collection}")
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
