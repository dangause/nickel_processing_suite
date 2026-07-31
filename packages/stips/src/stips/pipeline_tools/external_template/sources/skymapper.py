"""SkyMapper Southern Survey DR4 external-template adapter.

VERIFIED CONSTRAINTS (queried 2026-07-27 against the live DR4 SIA):

- The service serves SINGLE-EPOCH CCD frames, not survey stacks. Rows carry
  ``image_type`` of "main" (100 s) or "short" (5 s). Only "main" is deep
  enough to be worth using as a template.
- Cutouts are hard-capped at 0.17 deg (10.2') PER REQUEST; above that the
  service returns ``HTTP 400: SIZE[0] must be between 0 and 0.17 deg``. The cap
  is not a property of the frame: several offset requests against the SAME
  ``image=`` id return sub-arrays of one CCD pixel grid, so a larger template is
  recovered by mosaicking (see :func:`assemble_tiles`).
- Zeropoints arrive as ``ZPAPPROX`` (with ``ZPTERR``) in the FITS header.
- Seeing arrives as ``QAFWHM`` in the header / ``mean_fwhm`` in the SIA table.
- WCS is ``RA---TPV`` (distortion terms), unlike PS1's plain TAN.

Because SkyMapper frames are shallow and ~2" seeing, this source is
EXPLICIT-ONLY: the ``auto`` template strategy never selects it.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import requests
from astropy.io import fits

from ..imaging import find_first_image_hdu
from .base import TemplateSourceError

log = logging.getLogger(__name__)

SIA_QUERY_URL = "https://api.skymapper.nci.org.au/public/siap/dr4/query"
SIA_IMAGE_URL = "https://api.skymapper.nci.org.au/public/siap/dr4/get_image"

#: Only these exposure classes are usable as templates.
TEMPLATE_IMAGE_TYPE = "main"

#: Fallback AB zeropoints by exposure class, from the observed DR4 spread.
DEFAULT_ZEROPOINTS = {"main": 28.75, "short": 25.4}

#: Fallback seeing when the frame carries no QAFWHM card.
DEFAULT_FWHM_ARCSEC = 2.0

_FLOAT_COLUMNS = ("exptime", "mjd_obs", "mean_fwhm", "zpapprox")

#: SkyMapper CCD geometry, read off the frame headers (``CCDSIZE`` is
#: ``[1:2048,1:4096]``) at the survey's 0.4976"/px plate scale. A mosaic can
#: never grow past the detector, so 2048 px = 17.0' is the hard ceiling on the
#: SHORT axis and 4096 px = 34.0' on the long one.
CCD_PIXEL_SCALE_ARCSEC = 0.4976
CCD_SHORT_AXIS_PX = 2048
CCD_LONG_AXIS_PX = 4096

#: Grid step as a fraction of the tile size. The service's own cutout boxes
#: already overlap slightly; keeping a 15% margin means rounding in the
#: service's pixel-box arithmetic can never open a one-pixel seam between tiles.
TILE_STEP_FRACTION = 0.85

#: Tolerances for the shared-pixel-grid check. CRVAL/CD are byte-identical
#: across tiles of one frame, so anything looser than "the same number" means
#: the tiles are not sub-arrays of a single CCD image.
_GRID_ABS_TOL = 1e-12
_GRID_REL_TOL = 1e-9
#: How far a CRPIX difference may sit from a whole pixel before the paste stops
#: being exact. Observed differences are exactly integral.
_SUBPIXEL_TOL = 0.01

#: WCS cards that must agree across every tile of a mosaic.
_SHARED_GRID_KEYS = ("CRVAL1", "CRVAL2", "CD1_1", "CD1_2", "CD2_1", "CD2_2")


def tile_centers(
    ra: float,
    dec: float,
    size_deg: float,
    tile_deg: float,
    *,
    step_fraction: float = TILE_STEP_FRACTION,
) -> list[tuple[float, float]]:
    """Centers of the smallest square tile grid covering ``size_deg``.

    The grid is symmetric about ``(ra, dec)``, so the requested position is
    always itself a tile center and the target never lands in a seam. RA steps
    are divided by ``cos(dec)`` — at Dec -36 a plain degree step would under-run
    the requested width by 19% and leave gaps — and wrapped into [0, 360) so a
    field near RA=0 does not ask the service for a negative coordinate.
    """
    if size_deg <= tile_deg:
        return [(ra % 360.0, dec)]

    step = tile_deg * step_fraction
    # (n-1) steps plus one tile width must span the request.
    n = int(math.ceil((size_deg - tile_deg) / step - 1e-9)) + 1
    half = (n - 1) / 2.0
    cos_dec = max(math.cos(math.radians(dec)), 1e-6)

    centers = []
    for row in range(n):
        tile_dec = dec + (row - half) * step
        for col in range(n):
            centers.append(((ra + (col - half) * step / cos_dec) % 360.0, tile_dec))
    return centers


def _read_tile(content: bytes, image_id: str) -> tuple[Any, np.ndarray]:
    """Decode one downloaded cutout into (header, float32 data)."""
    try:
        with fits.open(io.BytesIO(content)) as hdul:
            hdu = find_first_image_hdu(hdul)
            # `.data` applies BSCALE/BZERO, so the array is already de-scaled;
            # copy it before the file object closes.
            return hdu.header.copy(), np.asarray(hdu.data, dtype=np.float32)
    except TemplateSourceError:
        raise
    except Exception as e:  # noqa: BLE001 - any malformed tile is the same story
        raise TemplateSourceError(
            f"SkyMapper returned an unreadable FITS tile for frame {image_id}: " f"{e}"
        ) from e


def _extent_deg(header: Any, data: np.ndarray) -> tuple[float, float]:
    """Angular width and height of an image, in degrees, from its CD matrix."""
    scale_x = math.hypot(float(header["CD1_1"]), float(header["CD2_1"]))
    scale_y = math.hypot(float(header["CD1_2"]), float(header["CD2_2"]))
    height_px, width_px = data.shape
    return width_px * scale_x, height_px * scale_y


def _tile_grid_value(header: Any, key: str, image_id: str) -> float:
    try:
        return float(header[key])
    except (KeyError, TypeError, ValueError):
        raise TemplateSourceError(
            f"SkyMapper tile from frame {image_id} has no usable {key} card, so "
            "the tiles cannot be shown to share one pixel grid and must not be "
            "pasted together."
        ) from None


def assemble_tiles(
    tiles: list[tuple[Any, np.ndarray]], *, image_id: str = "?"
) -> tuple[Any, np.ndarray]:
    """Paste same-frame tiles into one array, by integer pixel offsets.

    Every tile returned for a given ``image=`` id is an exact sub-array of that
    CCD's pixel grid: identical ``CRVAL``, identical ``CD``, identical ``PV``
    distortion terms, differing only in ``CRPIX``. So the mosaic is a pure
    integer paste — **no reprojection, no resampling**, and therefore no
    interpolation error, no PSF change and no photometric change. Reprojecting
    would also invalidate the ``RA---TPV`` distortion solution, whose ``PV``
    coefficients are defined relative to this exact ``CRVAL``/``CD``.

    A tile's origin in the assembled frame is ``CRPIX_ref - CRPIX_tile`` per
    axis, where ``CRPIX_ref`` is the largest ``CRPIX`` over the tiles (a larger
    ``CRPIX`` means a sub-array that starts closer to the CCD origin). The
    assembled header therefore keeps ``CRPIX = CRPIX_ref``, which is the value
    that leaves every tile's WCS exactly where it was.

    Uncovered pixels are NaN: the downstream converter masks non-finite pixels
    as BAD, and the LSST reprojection carries them through as NO_DATA.

    Raises:
        TemplateSourceError: if the tiles do not share one pixel grid, rather
            than silently pasting misaligned sky.
    """
    if not tiles:
        raise TemplateSourceError(
            f"No SkyMapper tiles to assemble for frame {image_id}."
        )

    reference = tiles[0][0]
    for header, _ in tiles[1:]:
        for key in _SHARED_GRID_KEYS:
            first = _tile_grid_value(reference, key, image_id)
            other = _tile_grid_value(header, key, image_id)
            if not math.isclose(
                first, other, rel_tol=_GRID_REL_TOL, abs_tol=_GRID_ABS_TOL
            ):
                raise TemplateSourceError(
                    f"SkyMapper tiles of frame {image_id} disagree on {key} "
                    f"({first!r} vs {other!r}), so they are not sub-arrays of a "
                    "single CCD pixel grid and cannot be pasted together. "
                    "Refusing to assemble a misaligned template."
                )

    crpix1 = [_tile_grid_value(h, "CRPIX1", image_id) for h, _ in tiles]
    crpix2 = [_tile_grid_value(h, "CRPIX2", image_id) for h, _ in tiles]
    ref1, ref2 = max(crpix1), max(crpix2)

    offsets = []
    for c1, c2 in zip(crpix1, crpix2):
        dx, dy = ref1 - c1, ref2 - c2
        ix, iy = round(dx), round(dy)
        if abs(dx - ix) > _SUBPIXEL_TOL or abs(dy - iy) > _SUBPIXEL_TOL:
            raise TemplateSourceError(
                f"SkyMapper tiles of frame {image_id} are offset by a "
                f"non-integer number of pixels ({dx:.3f}, {dy:.3f}); pasting "
                "them would smear the PSF by a sub-pixel shift. Refusing to "
                "assemble."
            )
        offsets.append((ix, iy))

    height = max(oy + data.shape[0] for (_, oy), (_, data) in zip(offsets, tiles))
    width = max(ox + data.shape[1] for (ox, _), (_, data) in zip(offsets, tiles))

    mosaic = np.full((height, width), np.nan, dtype=np.float32)
    for (ox, oy), (_, data) in zip(offsets, tiles):
        ny, nx = data.shape
        mosaic[oy : oy + ny, ox : ox + nx] = data

    header = reference.copy()
    header["CRPIX1"] = ref1
    header["CRPIX2"] = ref2
    header["NAXIS1"] = width
    header["NAXIS2"] = height
    # The tile data was already de-scaled to float32 on read; leaving the tile's
    # integer scaling cards behind would have astropy re-apply them on write.
    for card in ("BSCALE", "BZERO", "BLANK", "CHECKSUM", "DATASUM"):
        if card in header:
            del header[card]
    header["HISTORY"] = (
        f"STIPS: mosaicked {len(tiles)} SkyMapper SIA cutouts of frame "
        f"{image_id} by integer pixel paste (no reprojection)"
    )
    return header, mosaic


def parse_sia_csv(text: str) -> list[dict]:
    """Parse a SIA CSV response into rows with numeric columns coerced."""
    rows: list[dict] = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = dict(raw)
        for column in _FLOAT_COLUMNS:
            value = row.get(column)
            if value in (None, ""):
                row[column] = None
                continue
            try:
                row[column] = float(value)
            except (TypeError, ValueError):
                row[column] = None
        rows.append(row)
    return rows


def select_frame(
    rows: list[dict],
    *,
    band: str,
    mjd_start: float | None = None,
    mjd_end: float | None = None,
) -> dict:
    """Pick the best usable frame, or raise with an actionable message.

    Policy: keep only ``main`` (100 s) frames in the requested band and MJD
    window, then take the one with the smallest seeing. ``short`` (5 s) frames
    are never used — they are far too shallow to template science data.
    """
    in_band = [r for r in rows if (r.get("band") or "").strip() == band]
    if not in_band:
        found = sorted({(r.get("band") or "?").strip() for r in rows})
        raise TemplateSourceError(
            f"SkyMapper returned no frames in band {band!r} at this position "
            f"(bands found: {', '.join(found) or 'none'})."
        )

    main = [
        r for r in in_band if (r.get("image_type") or "").strip() == TEMPLATE_IMAGE_TYPE
    ]
    if not main:
        # `main` is empty here, so count the "short" frames specifically
        # (rather than everything non-main) so the label stays accurate even
        # if the API ever returns a third image_type.
        n_short = len(
            [r for r in in_band if (r.get("image_type") or "").strip() == "short"]
        )
        n_other = len(in_band) - n_short
        other_note = f", plus {n_other} other frame(s)" if n_other else ""
        raise TemplateSourceError(
            f"SkyMapper has no 'main' (100 s) {band}-band frames at this "
            f"position — only {n_short} 'short' (5 s) frame(s){other_note}, "
            "which are far too shallow to use as a DIA template. SkyMapper "
            "cannot template this field; build a CTIO self-coadd template "
            "instead (template.type: coadd)."
        )

    windowed = main
    if mjd_start is not None or mjd_end is not None:
        # A window is requested: a row with no parseable mjd_obs cannot be
        # shown to satisfy it, so it must be excluded rather than defaulted
        # to 0.0 (which would arbitrarily pass one bound and fail the other).
        windowed = [r for r in windowed if r.get("mjd_obs") is not None]
        if mjd_start is not None:
            windowed = [r for r in windowed if r["mjd_obs"] >= mjd_start]
        if mjd_end is not None:
            windowed = [r for r in windowed if r["mjd_obs"] <= mjd_end]
    if not windowed:
        raise TemplateSourceError(
            f"SkyMapper has {len(main)} 'main' {band}-band frame(s) here, but "
            f"none inside the requested MJD window "
            f"[{mjd_start}, {mjd_end}]. Widen the window or drop it."
        )

    best = min(windowed, key=lambda r: r.get("mean_fwhm") or float("inf"))
    if best.get("mean_fwhm") is None:
        log.warning(
            "Selected SkyMapper frame %s has no usable mean_fwhm; the "
            "best-seeing pick among %d candidate(s) is arbitrary.",
            best.get("unique_image_id"),
            len(windowed),
        )
    log.info(
        'Selected SkyMapper frame %s: exptime=%.0fs, FWHM=%.2f", MJD=%.5f',
        best.get("unique_image_id"),
        best.get("exptime") or 0.0,
        best.get("mean_fwhm") or float("nan"),
        best.get("mjd_obs") or float("nan"),
    )
    return best


class SkyMapperSource:
    """Adapter for the SkyMapper DR4 Simple Image Access service."""

    name = "skymapper"
    #: Verified 2026-07-27: the DR4 SIA returns HTTP 400 above 0.17 deg. This is
    #: a PER-REQUEST cap — ``fetch`` mosaics several requests against one frame
    #: to deliver more than this (see :attr:`max_assembled_deg`).
    max_cutout_deg = 0.17
    #: Ceiling on the ASSEMBLED template: a mosaic cannot outgrow the CCD, which
    #: is 2048 x 4096 px at 0.4976"/px = 17.0' x 34.0'. The short axis is what
    #: binds, so ~0.283 deg is the most any square request can actually get, and
    #: a bigger ask comes back truncated by the detector edge (loudly — see
    #: ``fetch``). It is deliberately NOT a rejection threshold: a truncated
    #: 17' template still templates far more of a 20' field than a 10' one.
    max_assembled_deg = CCD_SHORT_AXIS_PX * CCD_PIXEL_SCALE_ARCSEC / 3600.0
    zeropoint_keywords = ["ZPAPPROX"]
    #: SkyMapper serves single-epoch ~100 s frames, not deep stacks, so bright
    #: stars DO reach the detector ceiling. Measured on an NGC2298 i-band frame:
    #: ``SATURATE = 65435`` with cores pinned at 64539 flat-topped over 5+ px,
    #: negative bleed undershoot (-97) immediately adjacent, and 893 px in 35
    #: blobs above 0.9x the level. Left unmasked those carry into the template.
    saturation_keywords = ["SATURATE"]
    #: The SIA download only checks a byte-count floor; coverage/size validation
    #: is the ingest entry point's job (see imaging.validate_cutout).
    fetch_validates_cutout = False

    def band_map(self, config: Any) -> dict[str, str]:
        from stips.core.pipeline import template_band_map

        return template_band_map(config, self.name)

    def default_zeropoint(self, header: Any) -> float:
        raw = header.get("EXPTIME", 0.0) or 0.0
        try:
            exptime = float(raw)
        except (TypeError, ValueError):
            log.warning(
                "SkyMapper frame has non-numeric EXPTIME %r; assuming 'short' "
                "(5 s) for the conservative zeropoint.",
                raw,
            )
            exptime = 0.0
        kind = "main" if exptime >= 50.0 else "short"
        return DEFAULT_ZEROPOINTS[kind]

    def native_fwhm(self, header: Any) -> float:
        value = header.get("QAFWHM")
        try:
            fwhm = float(value)
        except (TypeError, ValueError):
            return DEFAULT_FWHM_ARCSEC
        return fwhm if fwhm > 0 else DEFAULT_FWHM_ARCSEC

    def _warn_if_smaller_than_fov(self, size_deg: float, fov_arcmin: float | None):
        """Warn when the cutout cannot cover the science field of view."""
        if fov_arcmin is None:
            return
        cutout_arcmin = size_deg * 60.0
        if cutout_arcmin >= fov_arcmin:
            return
        log.warning(
            "SkyMapper cutout is %.1f' but the science FOV is ~%.1f'. Dithered "
            "pointings will fall outside the template and have NO PSF-matching "
            "kernel candidates (NoKernelCandidatesError). Expect partial or "
            "failed subtractions away from the field center.",
            cutout_arcmin,
            fov_arcmin,
        )

    def fetch(
        self,
        ra: float,
        dec: float,
        src_band: str,
        size_deg: float,
        out_dir: Path,
        *,
        mjd_start: float | None = None,
        mjd_end: float | None = None,
        fov_arcmin: float | None = None,
        session: Any = None,
        timeout: int = 180,
    ) -> Path:
        """Query the DR4 SIA, pick the best 'main' frame, and download it.

        At or below the per-request cap this is one query and one download. Above
        it, the SAME frame is requested on a small overlapping grid and the tiles
        are pasted together by :func:`assemble_tiles` — the frame is chosen once,
        because mixing frames would break the shared-pixel-grid property and the
        single-PSF / single-zeropoint assumption the ingest relies on.
        """
        http = session or requests
        tile_deg = min(size_deg, self.max_cutout_deg)
        centers = tile_centers(ra, dec, size_deg, tile_deg)
        if len(centers) > 1:
            grid = int(round(math.sqrt(len(centers))))
            log.warning(
                "Requested %.3f deg (%.1f') exceeds SkyMapper's %.3f deg "
                "per-request SIA cap; mosaicking a %dx%d grid of %.3f deg tiles "
                "from a single frame instead of returning a %.3f deg cutout.",
                size_deg,
                size_deg * 60.0,
                self.max_cutout_deg,
                grid,
                grid,
                tile_deg,
                self.max_cutout_deg,
            )
        else:
            self._warn_if_smaller_than_fov(size_deg, fov_arcmin)

        query_params = {
            "POS": f"{ra},{dec}",
            # The SIA query box obeys the same 0.17 deg cap as a cutout.
            "SIZE": f"{tile_deg:g}",
            "BAND": src_band,
            "FORMAT": "image/fits",
            "VERB": "3",
            "RESPONSEFORMAT": "CSV",
        }
        if mjd_start is not None:
            query_params["MJD_START"] = f"{mjd_start:g}"
        if mjd_end is not None:
            query_params["MJD_END"] = f"{mjd_end:g}"

        log.info(
            "Querying SkyMapper DR4 SIA for %s-band at RA=%.4f, Dec=%.4f "
            "(%.3f deg = %.1f')",
            src_band,
            ra,
            dec,
            size_deg,
            size_deg * 60.0,
        )
        response = http.get(SIA_QUERY_URL, params=query_params, timeout=timeout)
        if response.status_code != 200:
            raise TemplateSourceError(
                f"SkyMapper SIA query failed with HTTP {response.status_code} "
                f"({SIA_QUERY_URL} POS={ra},{dec} BAND={src_band})"
            )

        frame = select_frame(
            parse_sia_csv(response.text),
            band=src_band,
            mjd_start=mjd_start,
            mjd_end=mjd_end,
        )

        try:
            image_id = frame["unique_image_id"]
        except KeyError:
            # ingest.py catches TemplateSourceError only; a bare KeyError would
            # escape as an unhandled traceback rather than an actionable message.
            raise TemplateSourceError(
                "SkyMapper SIA row has no 'unique_image_id' column, so the "
                "frame cannot be downloaded. Columns present: "
                f"{', '.join(sorted(frame)) or 'none'}."
            ) from None

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / (
            f"skymapper_{src_band}_ra{ra:.4f}_dec{dec:.4f}_" f"{image_id}.fits"
        )

        if len(centers) == 1:
            content = self._download_tile(
                http, image_id, ra, dec, tile_deg, timeout=timeout
            )
            out_file.write_bytes(content)
            log.info(
                "Wrote SkyMapper cutout: %s (%d bytes)",
                out_file,
                out_file.stat().st_size,
            )
            return out_file

        tiles = []
        for tile_ra, tile_dec in centers:
            try:
                content = self._download_tile(
                    http, image_id, tile_ra, tile_dec, tile_deg, timeout=timeout
                )
                tiles.append(_read_tile(content, image_id))
            except (TemplateSourceError, requests.RequestException) as e:
                # Tiles that fall off the CCD edge legitimately fail, and one
                # flaky connection out of nine should not throw away the eight
                # tiles that did arrive; the mosaic is still worth assembling.
                log.warning(
                    "SkyMapper tile at RA=%.5f, Dec=%.5f is unusable, skipping "
                    "it: %s",
                    tile_ra,
                    tile_dec,
                    e,
                )

        if not tiles:
            raise TemplateSourceError(
                f"All {len(centers)} SkyMapper tile requests for frame "
                f"{image_id} failed, so no template could be assembled at "
                f"RA={ra}, Dec={dec}. See the per-tile warnings above for the "
                "underlying cause."
            )

        header, mosaic = assemble_tiles(tiles, image_id=image_id)
        self._report_mosaic(header, mosaic, len(tiles), len(centers), size_deg)

        fits.PrimaryHDU(data=mosaic, header=header).writeto(out_file, overwrite=True)
        log.info(
            "Wrote SkyMapper mosaic: %s (%d bytes)", out_file, out_file.stat().st_size
        )
        # The FOV warning now compares against what was actually assembled, not
        # against what was asked for.
        self._warn_if_smaller_than_fov(min(_extent_deg(header, mosaic)), fov_arcmin)
        return out_file

    def _download_tile(
        self,
        http: Any,
        image_id: str,
        ra: float,
        dec: float,
        size_deg: float,
        *,
        timeout: int,
    ) -> bytes:
        """Download one cutout from a named frame, or raise."""
        image_params = {
            "image": image_id,
            "format": "fits",
            "pos": f"{ra},{dec}",
            "size": f"{size_deg:g},{size_deg:g}",
        }
        response = http.get(SIA_IMAGE_URL, params=image_params, timeout=timeout)
        if response.status_code != 200:
            raise TemplateSourceError(
                f"SkyMapper image download failed with HTTP "
                f"{response.status_code} for frame "
                f"{image_id}"
            )
        if len(response.content) < 10000:
            raise TemplateSourceError(
                f"SkyMapper returned a response that is too small to be a FITS "
                f"cutout ({len(response.content)} bytes) for frame "
                f"{image_id}"
            )
        return response.content

    def _report_mosaic(
        self,
        header: Any,
        mosaic: np.ndarray,
        n_ok: int,
        n_requested: int,
        size_deg: float,
    ) -> None:
        """Say how big the mosaic actually came out and how much it covers."""
        height_px, width_px = mosaic.shape
        width_deg, height_deg = _extent_deg(header, mosaic)
        width_arcmin = width_deg * 60.0
        height_arcmin = height_deg * 60.0
        requested_arcmin = size_deg * 60.0
        area_fraction = (
            min(width_arcmin, requested_arcmin)
            * min(height_arcmin, requested_arcmin)
            / (requested_arcmin * requested_arcmin)
        )
        filled_fraction = float(np.isfinite(mosaic).mean())

        log.info(
            "Assembled %d of %d SkyMapper tiles into %d x %d px = %.1f' x %.1f' "
            "(requested %.1f' x %.1f'): %.0f%% of the requested area, and "
            "%.1f%% of the assembled pixels carry data.",
            n_ok,
            n_requested,
            width_px,
            height_px,
            width_arcmin,
            height_arcmin,
            requested_arcmin,
            requested_arcmin,
            area_fraction * 100.0,
            filled_fraction * 100.0,
        )

        short_axis_arcmin = CCD_SHORT_AXIS_PX * CCD_PIXEL_SCALE_ARCSEC / 60.0
        long_axis_arcmin = CCD_LONG_AXIS_PX * CCD_PIXEL_SCALE_ARCSEC / 60.0
        if min(width_arcmin, height_arcmin) < requested_arcmin * 0.98:
            log.warning(
                "The SkyMapper mosaic is SMALLER THAN REQUESTED (%.1f' x %.1f' "
                "vs %.1f'): a single CCD is %d x %d px at %.4f\"/px = %.1f' x "
                "%.1f', so the detector edge truncates anything wider than "
                "%.1f' (%.3f deg). Everything outside the mosaic will be "
                "NO_DATA in the difference images.",
                width_arcmin,
                height_arcmin,
                requested_arcmin,
                CCD_SHORT_AXIS_PX,
                CCD_LONG_AXIS_PX,
                CCD_PIXEL_SCALE_ARCSEC,
                short_axis_arcmin,
                long_axis_arcmin,
                short_axis_arcmin,
                self.max_assembled_deg,
            )


__all__ = [
    "CCD_LONG_AXIS_PX",
    "CCD_PIXEL_SCALE_ARCSEC",
    "CCD_SHORT_AXIS_PX",
    "SIA_QUERY_URL",
    "SIA_IMAGE_URL",
    "TILE_STEP_FRACTION",
    "SkyMapperSource",
    "TemplateSourceError",
    "assemble_tiles",
    "parse_sia_csv",
    "select_frame",
    "tile_centers",
]
