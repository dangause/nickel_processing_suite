"""astropy-only helpers shared by every external-template source.

This module must NOT import lsst — it is imported by the adapters, which run
in the plain venv during unit tests.
"""

from __future__ import annotations

import logging

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

log = logging.getLogger(__name__)

#: 3631 Jy expressed in nJy — the AB magnitude zero-flux reference.
AB_ZERO_FLUX_NJY = 3631e9


def zeropoint_to_calibration_mean(zp: float) -> float:
    """Convert an AB zeropoint to a PhotoCalib calibration mean (nJy/ADU)."""
    return AB_ZERO_FLUX_NJY * 10 ** (-0.4 * zp)


def clamp_cutout_size(requested_deg: float, max_deg: float | None, logger=log) -> float:
    """Clamp a requested cutout size to a source's hard service limit.

    Clamping is deliberately non-fatal: the request is still satisfiable, just
    smaller than asked. SkyMapper's SIA returns HTTP 400 above 0.17 deg, so
    clamping here converts a hard failure into a warned-about degradation.
    """
    if max_deg is None or requested_deg <= max_deg:
        return requested_deg
    logger.warning(
        "Requested cutout %.3f deg exceeds this source's limit of %.3f deg; "
        "clamping to %.3f deg. The template will be SMALLER than requested.",
        requested_deg,
        max_deg,
        max_deg,
    )
    return max_deg


def find_first_image_hdu(hdul):
    """Return the first HDU holding 2-D image data."""
    for hdu in hdul:
        data = getattr(hdu, "data", None)
        if isinstance(data, np.ndarray) and data.ndim >= 2:
            return hdu
    raise RuntimeError("No image HDU with data found")


def file_covers_target(path, ra: float, dec: float) -> bool:
    """Check whether a FITS cutout actually covers the requested sky position."""
    try:
        with fits.open(path) as hdul:
            try:
                image_hdu = find_first_image_hdu(hdul)
            except RuntimeError:
                log.warning("  No image HDU found when checking coverage for %s", path)
                return False

            wcs = WCS(image_hdu.header)
            x, y = wcs.all_world2pix(ra, dec, 0)
            ny, nx = image_hdu.data.shape

            inside = bool(
                np.all(np.isfinite([x, y])) and (0 <= x < nx) and (0 <= y < ny)
            )
            log.info(
                "  Target pixel in image: x=%.1f, y=%.1f (image size %dx%d)",
                x,
                y,
                nx,
                ny,
            )
            if not inside:
                log.warning("  Target is outside the image footprint")
            return inside
    except Exception as e:  # noqa: BLE001 - diagnostic path, never fatal
        log.warning("  Coverage check failed for %s: %s", path, e)
        return False


def file_meets_requested_size(
    path, requested_size_deg: float, min_fraction: float = 0.85
) -> bool:
    """Check a cutout is close to the requested angular size.

    Guards against edge-trimmed cutouts that contain the target but leave too
    little template margin for DIA overlap.
    """
    try:
        with fits.open(path) as hdul:
            try:
                image_hdu = find_first_image_hdu(hdul)
            except RuntimeError:
                log.warning("  No image HDU found when checking size for %s", path)
                return False

            wcs = WCS(image_hdu.header)
            ny, nx = image_hdu.data.shape
            if nx < 2 or ny < 2:
                log.warning("  Invalid image shape %dx%d for size check", nx, ny)
                return False

            x_mid = (nx - 1) / 2.0
            y_mid = (ny - 1) / 2.0
            ra_w, dec_w = wcs.all_pix2world([0, nx - 1], [y_mid, y_mid], 0)
            ra_h, dec_h = wcs.all_pix2world([x_mid, x_mid], [0, ny - 1], 0)
            width_deg = (
                SkyCoord(ra=ra_w[0] * u.deg, dec=dec_w[0] * u.deg)
                .separation(SkyCoord(ra=ra_w[1] * u.deg, dec=dec_w[1] * u.deg))
                .deg
            )
            height_deg = (
                SkyCoord(ra=ra_h[0] * u.deg, dec=dec_h[0] * u.deg)
                .separation(SkyCoord(ra=ra_h[1] * u.deg, dec=dec_h[1] * u.deg))
                .deg
            )

            min_required = requested_size_deg * min_fraction
            ok = bool(width_deg >= min_required and height_deg >= min_required)
            log.info(
                "  Cutout angular size: %.3f x %.3f deg (requested %.3f, min %.3f)",
                width_deg,
                height_deg,
                requested_size_deg,
                min_required,
            )
            if not ok:
                log.warning("  Cutout is smaller than requested")
            return ok
    except Exception as e:  # noqa: BLE001 - diagnostic path, never fatal
        log.warning("  Size check failed for %s: %s", path, e)
        return False
