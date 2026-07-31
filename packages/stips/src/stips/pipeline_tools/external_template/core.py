#!/usr/bin/env python3
"""LSST-dependent half of the external-template framework.

This is the ONE module in ``packages/stips`` that imports ``lsst`` at module
scope: it only ever executes inside the LSST stack, via
:mod:`stips.pipeline_tools.external_template.ingest`. The survey adapters in
``sources/`` must stay lsst-free (an AST scan in the test suite enforces that),
so everything that needs ``lsst.afw`` / ``lsst.daf.butler`` lives here instead.

The conversion is source-agnostic: the three survey-specific decisions
(which header cards carry the zeropoint, what zeropoint to assume when none is
present, and what the frame's seeing is) are delegated to the adapter.
"""

import logging
import math
import os
import sys

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

try:
    import lsst.afw.detection as afwDetection
    import lsst.afw.image as afwImage
    import lsst.afw.math as afwMath

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

from . import imaging  # noqa: E402  (must follow the guarded lsst import block)

#: Library-style logger: handler/level setup belongs to the caller. The CLI
#: entry point (``ingest.py``) calls ``logging.basicConfig`` for standalone runs.
log = logging.getLogger(__name__)


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


def fits_to_lsst_exposure(
    path,
    local_band,
    source,
    *,
    degrade_to_fwhm=None,
    force_unity_photocalib=False,
):
    """Convert a survey FITS cutout to an LSST ExposureF in nJy.

    Source-agnostic. The three survey-specific decisions are delegated to the
    adapter: which header cards carry the zeropoint, what zeropoint to assume
    when none is present, and what the frame's seeing is.
    """
    log.info(
        "Converting %s image to LSST Exposure (target band: %s)",
        source.name,
        local_band,
    )

    with fits.open(path) as hdul:
        image_hdu = imaging.find_first_image_hdu(hdul)
        data = image_hdu.data
        header = image_hdu.header

        astropy_wcs = WCS(header)
        merged = {}
        for hdu in hdul:
            for key, value in hdu.header.items():
                if key and key not in merged:
                    merged[key] = value

        # (1) Zeropoint: adapter-supplied card list, adapter-supplied default.
        zp = None
        for keyword in source.zeropoint_keywords:
            if keyword in merged:
                zp = float(merged[keyword])
                log.info("Found zeropoint in header[%s]: %.3f", keyword, zp)
                break
        if zp is None:
            zp = source.default_zeropoint(merged)
            log.warning(
                "No zeropoint card in header; using %s default: %.3f", source.name, zp
            )

        lsst_wcs = convert_astropy_wcs_to_lsst(astropy_wcs)

        # (1a) Linearise the pixels BEFORE anything reads them as flux.
        # A PS1 stack pulled straight from the archive is asinh-compressed;
        # only the fitscut service hands back linear pixels. The zeropoint
        # above describes the DECODED counts, so this has to happen before the
        # PhotoCalib scaling below (and before the finite check, so a decode
        # that overflows to inf is caught as BAD rather than silently kept).
        data, asinh_decoded = imaging.decode_asinh_scaling(
            data, header if "BSOFTEN" in header else merged
        )

        # NOTE: Do NOT mask negative pixels — sky-subtracted images legitimately
        # have negative values.
        bad_mask = ~np.isfinite(data)
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)

        if force_unity_photocalib:
            calibration_mean = 1.0
            log.info("PhotoCalib forced to unity (no flux conversion)")
        else:
            calibration_mean = imaging.zeropoint_to_calibration_mean(zp)
            log.info(
                "PhotoCalib from zeropoint: %.3e nJy/ADU (zp=%.3f)",
                calibration_mean,
                zp,
            )

        # Pre-calibrate to nJy so the DIA kernel only handles PSF matching and
        # not a large template-to-science flux ratio.
        #
        # The LSST DIA subtractImages task does NOT normalize flux scales before
        # kernel fitting — the PSF-matching kernel absorbs any template-to-science
        # flux ratio. When the template is stored in raw ADU (PhotoCalib ~ 363
        # nJy/ADU) but science PVIs are already in nJy (PhotoCalib = 1.0), the
        # kernel must absorb a ~363x scale factor on top of the PSF shape change,
        # causing numerical instability and biased difference-image photometry.
        if calibration_mean != 1.0:
            data = data * calibration_mean

        masked_image = afwImage.MaskedImageF(data.shape[1], data.shape[0])
        masked_image.image.array[:, :] = data.astype(np.float32)
        masked_image.mask.array[bad_mask] = masked_image.mask.getPlaneBitMask("BAD")

        good = data[~bad_mask]
        if len(good) > 100:
            mad = np.median(np.abs(good - np.median(good)))
            variance = np.maximum((1.4826 * mad) ** 2, np.abs(good))
            masked_image.variance.array[:, :] = variance.mean()
        else:
            masked_image.variance.array[:, :] = 1.0
            log.warning("Too few good pixels for variance estimate, using 1.0")

        exposure = afwImage.ExposureF(masked_image)
        exposure.setWcs(lsst_wcs)
        exposure.setPhotoCalib(PhotoCalib(1.0))
        exposure.setFilter(afwImage.FilterLabel(band=local_band))

        # (2) Seeing: adapter-supplied per-frame value, not a constant.
        fwhm_arcsec = source.native_fwhm(merged)
        sigma_pix = 1.3
        try:
            bbox = exposure.getBBox()
            center = geom.Point2D(bbox.getCenterX(), bbox.getCenterY())
            pix_scale = exposure.getWcs().getPixelScale(center).asArcseconds()
            if pix_scale > 0:
                sigma_pix = (fwhm_arcsec / pix_scale) / 2.3548
        except Exception as e:  # noqa: BLE001
            log.warning("Could not compute pixel scale for PSF: %s", e)
        exposure.setPsf(afwDetection.GaussianPsf(21, 21, sigma_pix))
        log.info(
            '  Set synthetic PSF: FWHM~%.2f" -> sigma=%.2f pix', fwhm_arcsec, sigma_pix
        )

        if degrade_to_fwhm is not None:
            exposure = degrade_exposure_psf(
                exposure,
                target_fwhm_arcsec=degrade_to_fwhm,
                current_fwhm_arcsec=fwhm_arcsec,
            )

        # (3) Provenance metadata is namespaced by source, not hardcoded PS1_*.
        metadata = exposure.getMetadata()
        exposure.getInfo().setMetadata(metadata)
        metadata.set("TEMPLATE_SOURCE", source.name)
        metadata.set("TEMPLATE_ASINH_DECODED", bool(asinh_decoded))
        metadata.set("TEMPLATE_ZEROPOINT", zp)
        metadata.set("TEMPLATE_FWHM_ARCSEC", fwhm_arcsec)
        metadata.set("TEMPLATE_ORIGIN_FILE", str(path))

        log.info("Created LSST Exposure: %s", exposure.getBBox())
        log.info("  Pixels: nJy (pre-calibrated, PhotoCalib=1.0)")
        return exposure


def _rescale_psf_for_target_geometry(
    source_psf, source_wcs, source_bbox, target_wcs, target_bbox
):
    """Rebuild a Gaussian PSF's width for a different pixel grid.

    ``GaussianPsf`` stores its width in PIXELS. Carrying a PSF measured on
    one pixel grid onto another grid with a different pixel scale, unchanged,
    silently misrepresents the seeing by the ratio of the two scales (e.g.
    PS1's ~0.25"/px -> the skymap patch's ~0.29"/px understates FWHM by
    ~13%; SkyMapper's ~0.50"/px -> the same patch scale understates it by
    ~42%, i.e. the reprojected FWHM reads ~0.58x the true value).

    Parameters
    ----------
    source_psf : lsst.afw.detection.Psf
        The PSF measured on the source (survey) pixel grid.
    source_wcs, target_wcs : lsst.afw.geom.SkyWcs
        WCS of the source exposure and of the target (patch) geometry.
    source_bbox, target_bbox : lsst.geom.Box2I
        Bounding boxes of the source exposure and of the target geometry.

    Returns
    -------
    lsst.afw.detection.GaussianPsf or None
        A new PSF with sigma rescaled to the target pixel grid, or ``None``
        if a pixel scale could not be determined (e.g. missing WCS) -- the
        caller should fall back to carrying the original PSF unchanged and
        log a warning that the seeing may be misrepresented.
    """
    if source_wcs is None or target_wcs is None:
        return None

    try:
        source_center = geom.Point2D(source_bbox.getCenterX(), source_bbox.getCenterY())
        target_center = geom.Point2D(target_bbox.getCenterX(), target_bbox.getCenterY())
        source_scale = source_wcs.getPixelScale(source_center).asArcseconds()
        target_scale = target_wcs.getPixelScale(target_center).asArcseconds()
        if not (source_scale > 0 and target_scale > 0):
            return None

        sigma_source_px = source_psf.computeShape(source_center).getDeterminantRadius()
        sigma_target_px = sigma_source_px * (source_scale / target_scale)
        if not (np.isfinite(sigma_target_px) and sigma_target_px > 0):
            return None

        # 3-sigma kernel on each side, same convention as degrade_exposure_psf.
        kernel_size = max(21, int(sigma_target_px * 6) | 1)
        return afwDetection.GaussianPsf(kernel_size, kernel_size, sigma_target_px)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not rescale PSF for target pixel grid: %s", e)
        return None


def reproject_to_patch(exposure, patch_info):
    """
    Reproject exposure to match patch WCS and bounding box.

    This ensures the external template has the exact geometry expected by the
    DIA pipeline.

    Parameters
    ----------
    exposure : lsst.afw.image.ExposureF
        Input exposure (external template)
    patch_info : lsst.skymap.PatchInfo
        Target patch from skymap

    Returns
    -------
    lsst.afw.image.ExposureF
        Reprojected exposure matching patch geometry
    """
    from lsst.afw.math import WarpingControl, warpExposure

    log.info("Reprojecting template to match patch geometry...")

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

    # Carry provenance metadata (TEMPLATE_SOURCE / TEMPLATE_ZEROPOINT /
    # TEMPLATE_FWHM_ARCSEC / TEMPLATE_ORIGIN_FILE, set by
    # fits_to_lsst_exposure) onto the reprojected exposure -- it is otherwise
    # silently dropped since we construct a fresh ExposureF above.
    try:
        source_metadata = exposure.getMetadata()
        reprojected.getInfo().setMetadata(source_metadata.deepCopy())
        log.info(
            "  Carried source metadata (TEMPLATE_* provenance) onto reprojected exposure"
        )
    except Exception as e:
        log.warning(f"  Could not copy metadata to reprojected exposure: {e}")

    # Preserve PSF if present (we add a synthetic PSF earlier), rescaled to
    # the target pixel grid: GaussianPsf stores its width in PIXELS, and the
    # patch grid generally has a different pixel scale than the source
    # survey frame, so carrying sigma_px over unchanged would misrepresent
    # the angular FWHM by the ratio of the two scales.
    try:
        source_psf = exposure.getPsf()
    except Exception as e:
        log.warning(f"  Could not read PSF from source exposure: {e}")
        source_psf = None

    if source_psf is not None:
        rescaled_psf = _rescale_psf_for_target_geometry(
            source_psf,
            exposure.getWcs(),
            exposure.getBBox(),
            patch_wcs,
            patch_bbox,
        )
        if rescaled_psf is not None:
            reprojected.setPsf(rescaled_psf)
            log.info("  Rescaled PSF onto reprojected exposure's pixel grid")
        else:
            # Fallback: carry the PSF over unchanged, but say so loudly --
            # the seeing may now be misrepresented by the scale ratio.
            reprojected.setPsf(source_psf)
            log.warning(
                "  Could not determine source/target pixel scales; carrying "
                "PSF onto reprojected exposure UNCHANGED (sigma in pixels) -- "
                "seeing will be misrepresented if pixel scales differ"
            )

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


#: Minimum fraction of a patch's pixels that must carry warped data for the
#: patch to be worth writing. ``findPatchList`` is documented as a naive
#: bounding-box search that "may find some patches that do not overlap the
#: region", and an all-NO_DATA ``template_coadd`` is worse than an absent one:
#: ``rewarpTemplate`` would still gather it and contribute nothing but mask.
MIN_PATCH_COVERAGE_FRACTION = 0.001

#: Number of samples taken along each edge of the exposure bbox when tracing
#: its sky footprint. Corners alone are enough for a plain TAN cutout; the
#: extra edge samples keep the footprint honest under a rotated or distorted
#: WCS, where the great-circle edges bow away from the corner-to-corner chords.
_FOOTPRINT_SAMPLES_PER_EDGE = 8


def exposure_sky_corners(exposure, samples_per_edge=_FOOTPRINT_SAMPLES_PER_EDGE):
    """Trace the sky footprint of an exposure from its WCS and bounding box.

    Parameters
    ----------
    exposure : lsst.afw.image.ExposureF
        Exposure whose footprint is wanted. Must carry a WCS.
    samples_per_edge : int, optional
        How many points to sample along each bbox edge (>= 2; the endpoints
        are the bbox corners).

    Returns
    -------
    list of lsst.geom.SpherePoint
        Sky positions around the exposure's perimeter, or an empty list if the
        exposure has no usable WCS.
    """
    wcs = exposure.getWcs()
    if wcs is None:
        log.warning("Exposure has no WCS; cannot compute its sky footprint")
        return []

    bbox = geom.Box2D(exposure.getBBox())
    n = max(2, int(samples_per_edge))
    fractions = [i / (n - 1) for i in range(n)]

    x0, x1 = bbox.getMinX(), bbox.getMaxX()
    y0, y1 = bbox.getMinY(), bbox.getMaxY()
    pixels = []
    for f in fractions:
        x = x0 + f * (x1 - x0)
        y = y0 + f * (y1 - y0)
        pixels.extend(
            [
                geom.Point2D(x, y0),
                geom.Point2D(x, y1),
                geom.Point2D(x0, y),
                geom.Point2D(x1, y),
            ]
        )

    corners = []
    for pixel in pixels:
        try:
            corners.append(wcs.pixelToSky(pixel))
        except Exception as e:  # noqa: BLE001
            log.debug("Could not map pixel %s to sky: %s", pixel, e)
    return corners


def find_overlapping_patches(skymap, exposure, coord, tract=None):
    """Find every skymap patch the exposure's sky footprint overlaps.

    The historical behaviour was ``tract.findPatch(coord)`` -- a single patch,
    the one holding the target coordinate -- which silently discarded every
    part of the cutout falling outside it. Measured on NGC2298 that threw away
    three of the four patches the validated self-coadd template covers.

    Parameters
    ----------
    skymap : lsst.skymap.BaseSkyMap
        The skymap to search.
    exposure : lsst.afw.image.ExposureF
        The (un-reprojected) survey exposure.
    coord : lsst.geom.SpherePoint
        Target coordinate; its patch is always included and is returned first.
    tract : int, optional
        Restrict the result to this tract. When given, patches found in other
        tracts are dropped and named in the log.

    Returns
    -------
    list of (lsst.skymap.TractInfo, lsst.skymap.PatchInfo)
        Ordered with the target coordinate's tract/patch first.
    """
    if tract is None:
        target_tract_info = skymap.findTract(coord)
    else:
        target_tract_info = skymap[tract]
    target_tract_id = target_tract_info.getId()
    target_patch_info = target_tract_info.findPatch(coord)
    target_patch_id = target_patch_info.getSequentialIndex()

    footprint = exposure_sky_corners(exposure)
    if not footprint:
        log.warning(
            "No sky footprint for the exposure; falling back to the single "
            "target patch tract=%d patch=%d",
            target_tract_id,
            target_patch_id,
        )
        return [(target_tract_info, target_patch_info)]

    # Public skymap API: findTractPatchList does the tract sweep for us and
    # returns per-tract patch lists, so no geometry is hand-rolled here.
    try:
        overlaps = skymap.findTractPatchList(footprint)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "skymap.findTractPatchList failed (%s); falling back to the single "
            "target patch tract=%d patch=%d",
            e,
            target_tract_id,
            target_patch_id,
        )
        return [(target_tract_info, target_patch_info)]

    found_tracts = sorted(tract_info.getId() for tract_info, _ in overlaps)
    if len(found_tracts) > 1:
        log.info(
            "Exposure footprint spans %d tracts: %s",
            len(found_tracts),
            found_tracts,
        )

    dropped_tracts = []
    pairs = []
    seen = set()

    # Target tract/patch first: callers that keep one "primary" data ID (the
    # template-metadata record, the stdout tract/patch parse in
    # stips.core.external_template) must keep seeing the target's patch.
    pairs.append((target_tract_info, target_patch_info))
    seen.add((target_tract_id, target_patch_id))

    for tract_info, patch_list in overlaps:
        tract_id = tract_info.getId()
        if tract is not None and tract_id != target_tract_id:
            dropped_tracts.append(tract_id)
            continue
        for patch_info in patch_list:
            key = (tract_id, patch_info.getSequentialIndex())
            if key in seen:
                continue
            seen.add(key)
            pairs.append((tract_info, patch_info))

    if dropped_tracts:
        log.warning(
            "Exposure footprint also overlaps tract(s) %s, but tract=%d was "
            "requested explicitly; those tracts are NOT ingested. Drop the "
            "explicit tract to cover them.",
            sorted(set(dropped_tracts)),
            target_tract_id,
        )

    log.info(
        "Footprint overlaps %d patch(es): %s",
        len(pairs),
        [(t.getId(), p.getSequentialIndex()) for t, p in pairs],
    )
    return pairs


def patch_coverage_fraction(reprojected, bbox=None):
    """Fraction of a reprojected patch's pixels that carry real warped data.

    "Real" means finite, non-zero, and not flagged NO_DATA -- the signature
    ``warpExposure`` leaves outside the input footprint.

    Parameters
    ----------
    reprojected : lsst.afw.image.ExposureF
        A patch-geometry exposure, as returned by ``reproject_to_patch``.
    bbox : lsst.geom.Box2I, optional
        Restrict the measurement to this sub-region (clipped to the exposure).
        Passing the patch's INNER bbox gives a figure that can be summed across
        patches without double-counting: outer bboxes overlap by
        ``patchBorder`` on every side, inner ones tile exactly.
    """
    image = reprojected.image.array
    mask = reprojected.mask.array
    if bbox is not None:
        full = reprojected.getBBox()
        clipped = geom.Box2I(bbox)
        clipped.clip(full)
        if clipped.isEmpty():
            return 0.0
        x0 = clipped.getMinX() - full.getMinX()
        y0 = clipped.getMinY() - full.getMinY()
        image = image[y0 : y0 + clipped.getHeight(), x0 : x0 + clipped.getWidth()]
        mask = mask[y0 : y0 + clipped.getHeight(), x0 : x0 + clipped.getWidth()]
    if image.size == 0:
        return 0.0
    no_data_bit = reprojected.mask.getPlaneBitMask("NO_DATA")
    usable = np.isfinite(image) & (image != 0) & ((mask & no_data_bit) == 0)
    return float(np.count_nonzero(usable)) / float(image.size)


def _pixel_area_arcsec2(wcs, bbox):
    """Solid angle of one pixel (arcsec^2) at the centre of ``bbox``."""
    try:
        center = geom.Point2D(bbox.getCenterX(), bbox.getCenterY())
        scale = wcs.getPixelScale(center).asArcseconds()
        return scale * scale if scale > 0 else None
    except Exception as e:  # noqa: BLE001
        log.debug("Could not compute pixel area: %s", e)
        return None


def _put_template_patch(butler, exposure, data_id, collection, overwrite):
    """Write one ``template_coadd``, preserving the skip/overwrite semantics.

    Returns
    -------
    str
        ``"written"``, or ``"exists"`` when an existing dataset was left in
        place because ``overwrite`` is False.
    """
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
            return "exists"
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
            return "exists"
        log.error(f"Failed to ingest exposure: {e}")
        log.error(f"Data ID: {data_id}")
        log.error(f"Collection: {collection}")
        raise

    return "written"


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
        Local science band (b, v, r, i)
    collection : str
        Output collection name
    tract : int, optional
        Tract number (will auto-determine if None)
    overwrite : bool, optional
        If True, allow replacing an existing template in the collection. Default False.

    Returns
    -------
    list of dict
        Data IDs of every ``template_coadd`` now present in ``collection`` for
        this exposure -- one per skymap patch the exposure's sky footprint
        overlaps with usable data. The target coordinate's own patch is first.
        Patches with no usable overlap after reprojection are omitted.
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
        # External templates are not instrument-specific, so no instrument dimension
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

    # Find the tract/patch of the target coordinate...
    coord = geom.SpherePoint(ra, dec, geom.degrees)

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

    # ...then every patch the exposure's FOOTPRINT overlaps, not just that one.
    # Writing only the target's patch discarded everything outside it (39% of
    # the science field on the NGC2298 SkyMapper mosaic); the self-coadd
    # template path has always ingested every overlapping patch and let
    # rewarpTemplate gather them at DIA time.
    pairs = find_overlapping_patches(skymap, exposure, coord, tract=tract)
    if tract is None:
        tract = pairs[0][0].getId()
        log.info(f"Auto-determined target tract: {tract}")
    else:
        log.info(f"Using specified tract: {tract}")
    log.info(f"Target tract={tract}, patch={pairs[0][1].getSequentialIndex()}")
    log.info(
        "Considering %d patch(es) for ingest: %s",
        len(pairs),
        [(t.getId(), p.getSequentialIndex()) for t, p in pairs],
    )

    input_pixel_area = _pixel_area_arcsec2(exp_wcs, exp_bbox) if exp_wcs else None
    input_area_arcmin2 = (
        exp_bbox.getArea() * input_pixel_area / 3600.0 if input_pixel_area else None
    )

    data_ids = []
    skipped = []
    retained_area_arcmin2 = 0.0

    for tract_info, patch_info in pairs:
        tract_id = tract_info.getId()
        patch_id = patch_info.getSequentialIndex()

        # CRITICAL: reproject onto this patch's geometry, so the template has
        # exactly the WCS and bbox the DIA pipeline expects for that patch.
        log.info("Reprojecting template to tract=%d patch=%d ...", tract_id, patch_id)
        patch_exposure = reproject_to_patch(exposure, patch_info)

        coverage = patch_coverage_fraction(patch_exposure)
        if coverage < MIN_PATCH_COVERAGE_FRACTION:
            skipped.append((tract_id, patch_id, f"coverage {100 * coverage:.3f}%"))
            log.info(
                "Skipping tract=%d patch=%d: only %.3f%% of its pixels carry "
                "warped data (< %.3f%% threshold); an all-NO_DATA template is "
                "worse than an absent one",
                tract_id,
                patch_id,
                100 * coverage,
                100 * MIN_PATCH_COVERAGE_FRACTION,
            )
            continue

        # Build data ID matching dataset type dimensions
        data_id = {
            "skymap": skymap_name,
            "tract": tract_id,
            "patch": patch_id,
            "band": band,
        }

        # Only add instrument/physical_filter if they're in the dataset type dimensions
        if dims and "instrument" in dims:
            data_id["instrument"] = prof_instrument
        if dims and "physical_filter" in dims:
            data_id["physical_filter"] = band.upper()

        status = _put_template_patch(
            butler, patch_exposure, data_id, collection, overwrite
        )
        data_ids.append(data_id)

        # Area accounting uses the INNER bbox: patch outer bboxes overlap by
        # patchBorder on every side, so summing outer coverage double-counts
        # the shared sky (it reported 109% of the input area on the NGC2298
        # mosaic). Inner bboxes tile the tract exactly.
        inner_bbox = patch_info.getInnerBBox()
        patch_pixel_area = _pixel_area_arcsec2(patch_info.getWcs(), inner_bbox)
        if patch_pixel_area:
            inner_coverage = patch_coverage_fraction(patch_exposure, inner_bbox)
            retained_area_arcmin2 += (
                inner_coverage * inner_bbox.getArea() * patch_pixel_area / 3600.0
            )
        log.info(
            "tract=%d patch=%d: %s (coverage %.1f%%)",
            tract_id,
            patch_id,
            status,
            100 * coverage,
        )

    if not data_ids:
        raise RuntimeError(
            f"No skymap patch retained usable data from this exposure "
            f"(considered {len(pairs)}: {[(t.getId(), p.getSequentialIndex()) for t, p in pairs]}). "
            "The cutout probably does not overlap the skymap where it claims to."
        )

    log.info(
        "Ingested %d/%d patch(es): %s",
        len(data_ids),
        len(pairs),
        [(d["tract"], d["patch"]) for d in data_ids],
    )
    if skipped:
        log.info("Skipped patch(es): %s", skipped)
    if input_area_arcmin2:
        log.info(
            "Sky area: input exposure %.1f arcmin^2 -> retained %.1f arcmin^2 "
            "across %d patch(es), inner-patch (non-overlapping) area (%.0f%%)",
            input_area_arcmin2,
            retained_area_arcmin2,
            len(data_ids),
            100 * retained_area_arcmin2 / input_area_arcmin2,
        )

    return data_ids


__all__ = [
    "MIN_PATCH_COVERAGE_FRACTION",
    "ConflictingDefinitionError",
    "convert_astropy_wcs_to_lsst",
    "degrade_exposure_psf",
    "exposure_sky_corners",
    "find_overlapping_patches",
    "fits_to_lsst_exposure",
    "ingest_exposure_to_butler",
    "patch_coverage_fraction",
    "reproject_to_patch",
]
