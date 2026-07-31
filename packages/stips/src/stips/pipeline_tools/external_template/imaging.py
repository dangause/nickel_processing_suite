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


def effective_cutout_size(requested_deg: float, max_deg: float | None) -> float:
    """The size a fetch will actually return, given a source's service limit.

    The silent counterpart to :func:`clamp_cutout_size`, for callers that need
    the post-clamp size *after* the adapter has already warned about it — most
    importantly the post-fetch size validation, which must compare against what
    the service could deliver rather than what was asked for.
    """
    if max_deg is None or requested_deg <= max_deg:
        return requested_deg
    return max_deg


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


def decode_asinh_scaling(data, header, *, logger=log):
    """Undo asinh (Lupton) pixel compression, if the header declares it.

    Pan-STARRS1 stack images are stored asinh-compressed::

        stored = (2.5 / ln10) * asinh((flux - BOFFSET) / (2 * BSOFTEN))

    so the inverse this applies is::

        flux = BOFFSET + BSOFTEN * 2 * sinh(stored * ln10 / 2.5)

    Reading the stored values as linear flux crushes a ~1e6:1 dynamic range
    down to ~10:1. Because asinh is very nearly linear near sky, the damage is
    invisible on faint sources -- the DIA kernel simply absorbs the constant
    scale factor -- but it grows with brightness, leaving progressively larger
    POSITIVE residuals at bright stars and, in turn, spurious DIA detections.

    Only some fetch paths hit this. The PS1 fitscut service returns decoded,
    linear pixels and strips BSOFTEN, whereas downloading a stack file
    straight from the archive (the MAST and ps1filenames paths) yields the raw
    compressed pixels. Keying off BSOFTEN handles both without the caller
    needing to know which path produced the file.

    Returns:
        ``(data, decoded)`` -- the linear-flux array, and whether any
        transform was applied. ``data`` is returned untouched when the header
        declares no usable softening.
    """
    if "BSOFTEN" not in header:
        return data, False

    try:
        bsoften = float(header["BSOFTEN"])
        boffset = float(header.get("BOFFSET", 0.0))
    except (TypeError, ValueError):
        logger.warning(
            "BSOFTEN/BOFFSET are present but not numeric; leaving pixels as-is"
        )
        return data, False

    if not (np.isfinite(bsoften) and bsoften > 0):
        logger.warning(
            "BSOFTEN=%r is not a usable softening parameter; leaving pixels as-is",
            bsoften,
        )
        return data, False

    decoded = boffset + bsoften * 2.0 * np.sinh(
        np.asarray(data, dtype=np.float64) * np.log(10.0) / 2.5
    )
    logger.info(
        "Decoded asinh pixel scaling (BSOFTEN=%.6g, BOFFSET=%.6g); "
        "pixel range %.4g -> %.4g",
        bsoften,
        boffset,
        _finite_span(data),
        _finite_span(decoded),
    )
    return decoded, True


#: Fraction of the declared saturation level at which a pixel counts as
#: saturated. Detectors clip slightly below the card value -- SkyMapper
#: declares ``SATURATE = 65435`` but real cores pin at 64539, i.e. 98.6% -- and
#: the approach to the ceiling is already non-linear, so the threshold sits
#: below both.
SATURATION_FRACTION = 0.9

#: Pixels to grow the saturated footprint by. Saturation does not stop at the
#: clipped pixels: charge bleeds into neighbours and the frames carry negative
#: undershoot right against the cores (-97 next to a 64539 core on SkyMapper),
#: which pushes a difference image the WRONG way if left unmasked.
SATURATION_GROW_PIX = 2


def saturation_mask(
    data,
    header,
    keywords,
    *,
    fraction=SATURATION_FRACTION,
    grow=SATURATION_GROW_PIX,
    logger=log,
):
    """Boolean mask of saturated pixels, grown to cover bleed artifacts.

    Survey frames that are single exposures rather than deep stacks reach the
    detector ceiling on bright stars. A clipped core under-represents the star,
    so the template is too faint there and the difference keeps a large POSITIVE
    residual -- indistinguishable, to the detection stage, from a transient.

    ``keywords`` is adapter-supplied and ordered; a source that cannot saturate
    (a deep coadd, say) passes an empty list and this becomes a no-op.

    Returns:
        ``(mask, level)`` -- the boolean mask, and the flux level above which a
        pixel was called saturated (``None`` when no usable card was found, in
        which case the mask is all-False).
    """
    data = np.asarray(data)
    empty = np.zeros(data.shape, dtype=bool)

    card = next((k for k in keywords if k in header), None)
    if card is None:
        return empty, None

    try:
        saturate = float(header[card])
    except (TypeError, ValueError):
        logger.warning(
            "Saturation card %s=%r is not numeric; not masking saturation",
            card,
            header[card],
        )
        return empty, None

    if not (np.isfinite(saturate) and saturate > 0):
        logger.warning(
            "Saturation card %s=%r is not a usable level; not masking saturation",
            card,
            saturate,
        )
        return empty, None

    level = fraction * saturate
    with np.errstate(invalid="ignore"):
        mask = np.isfinite(data) & (data >= level)

    n_clipped = int(mask.sum())
    if n_clipped and grow > 0:
        from scipy.ndimage import binary_dilation

        mask = binary_dilation(mask, iterations=int(grow))

    if n_clipped:
        logger.info(
            "Masked saturation: %d pixels at/above %.6g (%s=%.6g x %.2f), "
            "%d after growing by %d px",
            n_clipped,
            level,
            card,
            saturate,
            fraction,
            int(mask.sum()),
            grow,
        )
    return mask, level


def _finite_span(data):
    """Peak-to-peak of the finite pixels, for the decode log line."""
    finite = np.asarray(data)[np.isfinite(data)]
    return float(finite.max() - finite.min()) if finite.size else float("nan")


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


def validate_cutout(
    path, ra: float, dec: float, size_deg: float, min_fraction: float = 0.85
) -> str | None:
    """Check a fetched cutout is usable as a template; None means it is.

    Runs both cutout validators as a unit so every source gets the same
    guarantee. The byte-count floor an adapter applies to an HTTP response only
    proves "this is not an error page" — an edge-trimmed survey frame is a
    perfectly well-formed FITS file that happens to miss the target or leave no
    DIA overlap margin.

    ``size_deg`` must be the EFFECTIVE (post-clamp) size — see
    :func:`effective_cutout_size` — or a source with a service cap would fail
    its own successful fetch.

    Returns:
        None when the cutout is usable, otherwise a human-readable reason.
    """
    if not file_covers_target(path, ra, dec):
        return (
            f"the cutout does not cover the requested position " f"(RA={ra}, Dec={dec})"
        )
    if not file_meets_requested_size(path, size_deg, min_fraction):
        return (
            f"the cutout is smaller than {min_fraction:.0%} of the "
            f"{size_deg:.3f} deg requested, leaving too little DIA overlap margin"
        )
    return None
