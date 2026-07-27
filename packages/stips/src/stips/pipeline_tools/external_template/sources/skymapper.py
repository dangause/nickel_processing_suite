"""SkyMapper Southern Survey DR4 external-template adapter.

VERIFIED CONSTRAINTS (queried 2026-07-27 against the live DR4 SIA):

- The service serves SINGLE-EPOCH CCD frames, not survey stacks. Rows carry
  ``image_type`` of "main" (100 s) or "short" (5 s). Only "main" is deep
  enough to be worth using as a template.
- Cutouts are hard-capped at 0.17 deg (10.2'); above that the service returns
  ``HTTP 400: SIZE[0] must be between 0 and 0.17 deg``.
- Zeropoints arrive as ``ZPAPPROX`` (with ``ZPTERR``) in the FITS header.
- Seeing arrives as ``QAFWHM`` in the header / ``mean_fwhm`` in the SIA table.
- WCS is ``RA---TPV`` (distortion terms), unlike PS1's plain TAN.

Because SkyMapper frames are shallow, ~2" seeing, and at most 10' across, this
source is EXPLICIT-ONLY: the ``auto`` template strategy never selects it.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

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
    #: Verified 2026-07-27: the DR4 SIA returns HTTP 400 above 0.17 deg.
    max_cutout_deg = 0.17
    zeropoint_keywords = ["ZPAPPROX"]

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


__all__ = [
    "SIA_QUERY_URL",
    "SIA_IMAGE_URL",
    "SkyMapperSource",
    "TemplateSourceError",
    "parse_sia_csv",
    "select_frame",
]
