"""CTIO 1.0m (Yale/SMARTS 1m) Y4KCam profile.

Y4KCam is a single-CCD, 4-amplifier ITL 4K detector (15 um px, 0.289"/px).
Adapted from the reference Nickel profile to Y4KCam's (cleaner) header
conventions:

  - Single filter wheel: ``FILTERID`` carries the band name (U/B/V/R/I), so it is
    used as ``filter_key`` (Nickel uses ``FILTNAM``). There is no "clear"
    filter, so an unrecognized filter is a hard error rather than a fallback.
  - Times: ``MJD-OBS`` (UTC float) is authoritative; ``DATE-OBS`` (ISO UTC) is
    the documented fallback.
  - Coords: ``RA`` is sexagesimal HOURS, ``DEC`` is sexagesimal degrees, frame
    from ``RADESYS``/``RADECSYS``/``EQUINOX`` (ICRS default).
  - Exposure sequence number is parsed from the filename (``yYYMMDD.NNNN.fits``)
    carried in ``FILENAME``/``DTACQNAM``/``ORIGNAME``.

Module import is stack-free: only ``stips`` is imported at module load; astropy
is imported lazily inside the hooks.
"""

import logging
import re

# Safe to import at module load: fetch.py is stdlib-only at import time
# (urllib/json); the NOIRLab archive is hit only when fetch_data() runs.
from fetch import fetch_data as _fetch_data
from stips import (
    CrosstalkSpec,
    Field,
    InstrumentProfile,
    Site,
    coerce_date,
    hook,
    pack_exposure_id,
)

log = logging.getLogger("lsst.obs.stips.ctio1m.profile")

profile = InstrumentProfile(
    name="CTIO1m",
    policy_name="CTIO1m",
    collection_prefix="CTIO1m",
    # Cerro Tololo: lat -30:09:55.5, long 70:48:52.7 W, alt 2200 m.
    site=Site(
        latitude=-30.165417,
        longitude=-70.814639,
        elevation=2200.0,
        name="Cerro Tololo Interamerican Observatory",
    ),
    # physical_filter -> band. "U+CuSO4" is Y4KCam's near-UV filter (U glass +
    # CuSO4 red-leak block); it maps to band u. It must be present or bias frames
    # parked at that wheel slot raise in unknown_filter and fail to ingest (a night
    # whose biases are all U+CuSO4 then has zero ingestable biases — real: 20100120).
    filters={
        "U": "u",
        "U+CuSO4": "u",
        "B": "b",
        "V": "v",
        "R": "r",
        "I": "i",
    },
    # raw FITS FILTERID value (upper-cased on lookup) -> physical_filter
    filter_aliases={
        "U": "U",
        "U+CuSO4": "U+CuSO4",
        "B": "B",
        "V": "V",
        "R": "R",
        "I": "I",
    },
    filter_key="FILTERID",
    # PS1 templates (LOCAL band -> PS1 band). PS1 serves grizy; Y4KCam's r/i map
    # to PS1 r/i. u/b/v have no PS1 equivalent and fall back to coadd templates in
    # "auto" mode. Matches the reference Nickel r/i policy.
    ps1_band_map={"r": "r", "i": "i"},
    # External-template band policy per source. SkyMapper (DR4) serves u/v/g/r/i/z
    # but its "v" is a Stromgren-like ~384nm VIOLET filter — NOT Johnson V
    # (~551nm), which Y4KCam's "v" is. Mapping v->v would fetch a near-UV
    # template for a green science image. The nearest SkyMapper match to
    # Johnson V is g (~510nm), but that needs a colorterm nobody has fit, so v
    # is EXCLUDED rather than silently mismapped.
    template_band_maps={
        "skymapper": {"r": "r", "i": "i"},
    },
    # FITS INSTRUME is "Y4KCam" (the camera), not the instrument name "CTIO1m".
    instrument_header_value="Y4KCam",
    header_map={
        "exposure_time": Field("EXPTIME", unit="s", default=0.0),
        "dark_time": Field("DARKTIME", unit="s", default=0.0),
        "boresight_airmass": Field("SECZ", default=float("nan")),
        "object": Field("OBJECT", default="UNKNOWN"),
        "science_program": Field("DTPROPID", default="unknown"),
        "relative_humidity": Field("HUMIDITY", default=0.0),
        "telescope": Field("TELESCOP", default="ct1m"),
    },
    # Y4KCam is mounted 180 deg rotated vs the assumed convention: the FITS
    # headers carry no field-rotation keyword, and with rotation_angle=0 the
    # initial sky WCS is 180 deg off, so the matcher cannot seed and astrometry
    # collapses to 13-46" "fits" on spurious matches. Empirically (initial-WCS
    # source-vs-refcat alignment sweep), orientation=180 deg is the clear winner
    # (~10x more sources align), letting the matcher converge. The mount has no
    # field rotation (equatorial), so this is a constant.
    const_map={"boresight_rotation_angle": 180.0, "boresight_rotation_coord": "sky"},
    # NOIRLab Astro Data Archive fetch (stips download); see fetch.py.
    fetch_data=_fetch_data,
    camera="camera/y4kcam.yaml",
    instrument_class="lsst.obs.stips.active.Instrument",
    # The Butler ``day_obs`` dimension is derived by astro_metadata_translator's
    # ``to_observing_day`` from the UT exposure datetime (NOT the profile's
    # ``day_obs`` hook / DTCALDAT, which only feeds ``observation_id``). CTIO is
    # at -70 deg longitude, so a local observing night (e.g. 2007-03-21) records
    # UT dates on the following calendar day (2007-03-22). Like Nickel, the local
    # night therefore maps to UT day_obs + 1. (Verified on real 2007-03-21 data:
    # DTCALDAT=2007-03-21 but the ingested exposures carry day_obs=20070322.)
    night_to_dayobs_offset_days=1,
    skymap_name="ctio1mRings-v1",
    skymap_collection="skymaps/ctio1mRings",
    obs_data_package="obs_ctio1m_data",
    # ISR config overrides, applied to every ISR invocation (calib build +
    # science) so the master bias/flat and the science frames are corrected
    # consistently:
    #   - doDefect=True: CTIO ships curated defect maps via obs_ctio1m_data
    #     (mirroring obs_nickel_data). The base defect (valid from 1970) is
    #     empty and masks nothing; an epoch-scoped defect covers the dead amp
    #     A01 region for the Jan-2010 SA98 run.
    #   - overscan.doParallelOverscan=True: Y4KCam reads 4 amps toward the
    #     detector centre with parallel-overscan strips on the inner edges.
    #     Serial overscan alone leaves a per-frame amp-row bias step (~2.5 ADU,
    #     visible as a top/bottom seam in the assembled image); the parallel pass
    #     tracks it. Must be on for the bias build too, else the master bias keeps
    #     the parallel structure and science would double-subtract it.
    #   - growSaturationFootprintSize=8 + doSaturationInterpolation: the camera
    #     saturation level (65535 ADU) masks only the saturated CORES (~tens of
    #     px); the default grow of 1 leaves the bleed wings of bright stars
    #     unmasked, and DIA then detects them as trailed sources (~40% of the
    #     spurious detections on the dense SA98 standard field). Growing the SAT
    #     footprint covers the bleed wings so they are excluded from detection.
    isr_overrides={
        "doDefect": True,
        "overscan.doParallelOverscan": True,
        "doSaturation": True,
        "growSaturationFootprintSize": 8,
        "doSaturationInterpolation": True,
    },
    # Intra-detector crosstalk for the 4-amp Y4KCam. MEASURED with
    # `stips measure-crosstalk` (cp_pipe cpCrosstalk) on the E2 standard field,
    # night 20111113 (2x2-binned, B/V/R/I, 93 science exposures; full 12-element
    # off-diagonal solve, ~20k-34k samples/element). Row i = receiving amp, col j
    # = source amp (coeffs[i][j] = fraction of amp j's signal appearing in amp i);
    # amp order is the camera order A00,A01,A02,A03; diagonal is 0.0. Magnitudes
    # are ~0.1-0.4% (largest between adjacent quadrants), as expected; per-element
    # errors are order-of-coefficient (crosstalk measured from stars, not saturated
    # trails), so treat these as indicative. STIPS builds a CrosstalkCalib from
    # this matrix, certifies it into the calib chain, and enables ISR doCrosstalk.
    # Re-measure with a brighter/denser field to tighten the values.
    crosstalk=CrosstalkSpec(
        coeffs=[
            [0.0, 2.796342e-03, 8.545585e-04, 7.248305e-04],
            [3.919039e-03, 0.0, 9.806441e-04, 8.650255e-04],
            [1.106021e-03, 8.157125e-04, 0.0, 2.590679e-03],
            [1.432011e-03, 1.140293e-03, 4.400683e-03, 0.0],
        ],
        units="adu",
    ),
)


# ---------------------------------------------------------------------------
# Shared helpers (DRY): several hooks call into each other (exposure_id ->
# day_obs/seqnum; day_obs -> datetime_end). Y4KCam's MJD-OBS is authoritative for
# the begin time. day_obs is the UT calendar day (NOT DTCALDAT, the local night —
# see _day_obs); the local night is recovered via night_to_dayobs_offset_days=1.
# ---------------------------------------------------------------------------

# Y4KCam raw filename: ``y{YYMMDD}.{seqnum}.fits``. YYMMDD is the LOCAL observing
# night; the seqnum resets each local night. So (local-night, seqnum) is the
# natural globally-unique exposure key (see _filename_fields / exposure_id) — it
# must NOT be derived from the UT day, which collides across consecutive nights.
_FILENAME_RE = re.compile(r"y(\d{6})\.(\d+)\.fits", re.IGNORECASE)


def _datetime_begin(header):
    """Begin time: prefer MJD-OBS (UTC float); fall back to DATE-OBS (ISO UTC)."""
    import astropy.time

    mjd = header.get("MJD-OBS")
    if mjd is not None:
        return astropy.time.Time(float(mjd), format="mjd", scale="utc")

    value = header.get("DATE-OBS")
    if value is None:
        return None
    return astropy.time.Time(value, format="isot", scale="utc")


def _datetime_end(header):
    """End time: begin + EXPTIME (handles EXPTIME==0 by returning begin)."""
    import astropy.time

    begin = _datetime_begin(header)
    if begin is None:
        return None
    exptime = float(header.get("EXPTIME", 0.0) or 0.0)
    if exptime > 0.0:
        return begin + astropy.time.TimeDelta(exptime, format="sec", scale="tai")
    return begin


# --- Date-characterized boresight pointing offsets (see tracking_radec) --------
# The CTIO Y4KCam mount carries a per-campaign systematic pointing offset: the
# TRUE field center is EAST/NORTH of the header RA/DEC by a campaign-specific
# amount (measured by BLIND astrometry.net solves). It is a pure boresight
# TRANSLATION -- camera plate scale (0.2889"/pix), orientation (PA~0) and
# distortion (negligible) were all confirmed correct -- NOT a parse/geometry bug.
#
# A row means the campaign has been CHARACTERIZED; the offset may be 0. Ranges are
# bounded to the MEASURED extent of each campaign (first/last night we have data
# for), NOT padded to month/year -- the range asserts what was VERIFIED, not mount
# stability across unmeasured time. An unmeasured night INSIDE a window inherits
# that offset (mild "stable within one run" interpolation); a night OUTSIDE every
# window is uncovered (offset 0) and is flagged by science.py's diagnostics.
#
# IMPORTANT: start_date/end_date are UT DATE-OBS dates (both consumers -- this
# table's own lookup via _datetime_begin, and science.py's day_obs diagnostic --
# match on UT), NOT local observing nights. A local CTIO night spans TWO UT
# calendar dates (evening + next-morning UT), so a window must extend one UT day
# past the last local night's date or it silently drops that night's morning
# frames. E.g. local night 20061216 (the last measured 2006 night) has frames on
# both UT 2006-12-16 (evening) and UT 2006-12-17 (morning) -- hence end 12-17
# below, not 12-16.
#
# (start_date, end_date, delta_east_arcsec, delta_north_arcsec, provenance)
_BORESIGHT_OFFSET_TABLE = [
    (
        "2006-09-27",
        "2006-12-17",
        257.0,
        320.0,
        "blind astrometry.net solve, 4 local nights 20060927-20061216 (2026-07); "
        'dRA*cosDec +257" (std 24), dDec +320" (std 57), ~412" @ PA~39 E-of-N; '
        "UT DATE-OBS extent 2006-09-27..2006-12-17 (each local night spans "
        "evening+next-morning UT, so the window end is the LAST night's UT "
        "morning date, one day past its local-night label)",
    ),
    (
        "2010-01-17",
        "2010-01-22",
        0.0,
        0.0,
        'SA98 run; ~60" offset already within matcher tolerance, no correction',
    ),
]


def _boresight_offset_entry(dt):
    """Row whose inclusive [start, end] UT-date range contains ``dt``, else None.

    Fail-closed: returns None if the date cannot be determined.
    """
    import datetime as _d

    d = coerce_date(dt)
    if d is None:
        return None
    for row in _BORESIGHT_OFFSET_TABLE:
        start = _d.date.fromisoformat(row[0])
        end = _d.date.fromisoformat(row[1])
        if start <= d <= end:
            return row
    return None


def boresight_offset_arcsec(dt):
    """(delta_east_arcsec, delta_north_arcsec) for the campaign, else (0.0, 0.0)."""
    row = _boresight_offset_entry(dt)
    return (row[2], row[3]) if row is not None else (0.0, 0.0)


def boresight_offset_covered(dt):
    """True iff the observation date falls in a characterized campaign window."""
    return _boresight_offset_entry(dt) is not None


def _filename_fields(header):
    """Parse ``(local_night: int YYYYMMDD, seqnum: int)`` from the raw filename.

    The Y4KCam filename ``y{YYMMDD}.{NNNN}.fits`` is carried in
    FILENAME/DTACQNAM/ORIGNAME. YY is a 20xx year (Y4KCam operated 2003-2013).
    The local night + per-night seqnum form the natural unique exposure key.
    """
    for key in ("FILENAME", "DTACQNAM", "ORIGNAME"):
        value = header.get(key)
        if not value:
            continue
        m = _FILENAME_RE.search(str(value))
        if m:
            return int("20" + m.group(1)), int(m.group(2))
    raise ValueError(
        "Could not parse y{YYMMDD}.{seqnum}.fits from FILENAME/DTACQNAM/ORIGNAME"
    )


def _day_obs(header):
    """Derive day_obs (YYYYMMDD int) from the end-of-exposure UT datetime.

    This is the UT calendar day (matching astro_metadata_translator's default),
    which drives the Butler ``day_obs`` dimension via the ``day_obs`` hook ->
    ``StipsTranslator.to_observing_day``. The local observing night (e.g. the
    2007-03-21 DTCALDAT) is recovered at query time via
    ``night_to_dayobs_offset_days=1``; DTCALDAT must NOT be used here, or day_obs
    would disagree with the UT-based convention and break the offset mapping.
    """
    return int(_datetime_end(header).datetime.strftime("%Y%m%d"))


# ---------------------------------------------------------------------------
# Quirk hooks. Signatures mirror the reference Nickel profile's hooks exactly.
# ---------------------------------------------------------------------------


@hook(profile)
def observation_type(header):
    """Return an LSST observation_type: science | flat | bias | focus.

    NOTE: science frames map to "science" (the LSST vocabulary the pipeline
    filters on), NOT "object" — calibrateImage/the science step select
    observation_type='science'.
    """
    obstype = str(header.get("OBSTYPE") or header.get("IMGTYPE") or "").strip().lower()
    obj = str(header.get("OBJECT", "")).strip().lower()

    if obstype in ("bias", "zero"):
        return "bias"
    if obstype == "flat" or "flat" in obj:
        return "flat"
    if obstype == "object":
        return "science"

    if "bias" in obj or "zero" in obj:
        return "bias"
    if any(w in obj for w in ("focus", "focusing", "point")):
        return "focus"

    return "science"


@hook(profile)
def exposure_id(header):
    """Unique exposure/visit ID that fits in 31 bits.

    ID = (days_since_2000 of the LOCAL night) * 10000 + seqnum

    The date term is the LOCAL observing night (from the y{YYMMDD} filename), NOT
    the UT day. Keying on the UT day (end-of-exposure) collides across consecutive
    nights: at CTIO's -70deg longitude a local night straddles UT midnight and the
    seqnum resets each local night, so the late frames of night N and the early
    frames of night N+1 share a UT day + reset seqnum and map to the same id
    (Butler exposure-sync conflict on ingest). (local-night, seqnum) is unique.
    day_obs stays UT-based (see day_obs) for to_observing_day consistency.
    """
    import datetime as _dt

    night, seqnum = _filename_fields(header)
    d = _dt.date(night // 10000, (night // 100) % 100, night % 100)
    days = (d - _dt.date(2000, 1, 1)).days
    return pack_exposure_id(days, seqnum)


@hook(profile)
def visit_id(header):
    return exposure_id(header)


@hook(profile)
def datetime_begin(header):
    """Begin time: prefer MJD-OBS (UTC); fall back to DATE-OBS."""
    return _datetime_begin(header)


@hook(profile)
def datetime_end(header):
    """End time: begin + EXPTIME."""
    return _datetime_end(header)


@hook(profile)
def day_obs(header):
    """UT calendar day as YYYYMMDD int, from datetime_end (NOT DTCALDAT; see _day_obs)."""
    return _day_obs(header)


@hook(profile)
def observation_id(header):
    """String ID that must be globally unique for the instrument.

    Keyed on the LOCAL night for the same reason as exposure_id -- the UT day
    collides across consecutive nights ("20100121_69" for two distinct frames).
    """
    night, seqnum = _filename_fields(header)
    return f"{night:08d}_{seqnum}"


@hook(profile)
def unknown_filter(header, raw):
    """Y4KCam has no 'clear' filter: an unrecognized FILTERID is a hard error."""
    log.error("Unrecognized FILTERID %r for Y4KCam (no 'clear' fallback)", raw)
    raise ValueError(f"Unrecognized Y4KCam filter: {raw!r}")


@hook(profile)
def tracking_radec(header, default=None):
    """Parse tracking RA (sexagesimal HOURS) + DEC (sexagesimal deg).

    Frame is taken from RADESYS/RADECSYS, falling back to EQUINOX (2000 -> FK5,
    else ICRS).

    A campaign-specific boresight offset (see ``_BORESIGHT_OFFSET_TABLE``) is
    applied when the exposure date falls in a characterized window; uncharacterized
    dates get no shift.
    """
    import astropy.units as u
    from astropy.coordinates import Angle, SkyCoord

    ra_str = header.get("RA")
    dec_str = header.get("DEC")
    if not ra_str or not dec_str:
        raise ValueError("No valid tracking coordinates (RA/DEC) in FITS header")

    ra_angle = Angle(str(ra_str), unit=u.hourangle)
    dec_angle = Angle(str(dec_str), unit=u.deg)

    ref_system = header.get("RADESYS") or header.get("RADECSYS")
    if not ref_system:
        equinox = header.get("EQUINOX")
        ref_system = "FK5" if equinox in (2000, 2000.0, "2000") else "ICRS"

    coord = SkyCoord(ra_angle, dec_angle, frame=str(ref_system).lower())

    east_arcsec, north_arcsec = boresight_offset_arcsec(_datetime_begin(header))
    if east_arcsec or north_arcsec:
        coord = coord.spherical_offsets_by(
            east_arcsec * u.arcsec,
            north_arcsec * u.arcsec,
        )

    return coord


@hook(profile, name="boresight_offset_covered")
def _boresight_offset_covered_hook(dt):
    """Hook: True iff the observation date is in a characterized campaign window.

    Registered under the key ``boresight_offset_covered`` so science.py can query
    it via ``profile.hooks``. A thin wrapper over the module-level lookup (no
    duplicated logic); named distinctly so it does not shadow that function.
    """
    return boresight_offset_covered(dt)
