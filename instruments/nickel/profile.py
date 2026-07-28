"""Nickel 1-meter telescope profile (Lick Observatory).

Copy this directory and edit profile.py + camera/ for your telescope."""

import logging

# Safe to import at module load: fetch.py is stdlib-only at import time
# (the lick_archive client is lazy-imported inside the fetch implementation).
from fetch import fetch_data as _fetch_data
from stips import Field, InstrumentProfile, Site, hook, make_exposure_id

log = logging.getLogger("lsst.obs.stips.nickel.profile")

profile = InstrumentProfile(
    name="Nickel",
    policy_name="Nickel",
    # `name` takes precedence in to_location(): EarthLocation.of_site("Lick
    # Observatory") is used, so the lat/lon/elevation below are never consulted
    # for Nickel. They are informational and serve as the documented fallback
    # for forks whose astropy lacks an of_site entry for this observatory.
    site=Site(
        latitude=37.343333,
        longitude=-121.636667,
        elevation=1290.0,
        name="Lick Observatory",
    ),
    # physical_filter -> band
    filters={
        "B": "b",
        "V": "v",
        "R": "r",
        "I": "i",
        "clear": None,
        "gp": "gp",
        "rp": "rp",
        "Halpha": "halpha",
        "OIII": "oiii",
    },
    # raw FITS FILTNAM value (upper-cased on lookup) -> physical_filter
    filter_aliases={
        "B": "B",
        "V": "V",
        "R": "R",
        "I": "I",
        "OPEN": "clear",
        "C": "clear",
        "CLEAR": "clear",
        "GP": "gp",
        "G'": "gp",
        "RP": "rp",
        "R'": "rp",
        "HALPHA": "Halpha",
        "H-ALPHA": "Halpha",
        "6563/100": "Halpha",
        "OIII": "OIII",
        "[OIII]": "OIII",
        "5000/100": "OIII",
    },
    filter_key="FILTNAM",
    # PS1 templates (LOCAL band -> PS1 band). PS1 serves grizy; Nickel's r/i
    # (Cousins R/I) map to PS1 r/i. b/v have no PS1 equivalent and fall back to
    # coadd templates in "auto" mode. This reproduces the historical r/i policy.
    ps1_band_map={"r": "r", "i": "i"},
    # The Nickel direct-imaging camera covers ~6.3' (2048 px at 0.37"/px).
    fov_arcmin=6.3,
    header_map={
        "exposure_time": Field("EXPTIME", unit="s", default=0.0),
        "dark_time": Field("EXPTIME", unit="s", default=0.0),
        "boresight_airmass": Field("AIRMASS", default=float("nan")),
        "object": Field("OBJECT", default="UNKNOWN"),
        "science_program": Field("PROGRAM", default="unknown"),
        "relative_humidity": Field("HUMIDITY", default=0.0),
        "telescope": Field("TELESCOP", default="Nickel 1m"),
    },
    const_map={"boresight_rotation_angle": 0.0, "boresight_rotation_coord": "sky"},
    camera="camera/nickel.yaml",
    instrument_class="lsst.obs.stips.active.Instrument",
    night_to_dayobs_offset_days=1,
    skymap_name="nickelRings-v1",
    skymap_collection="skymaps/nickelRings",
    obs_data_package="obs_nickel_data",
    fetch_data=_fetch_data,
)


# ---------------------------------------------------------------------------
# Shared helpers (DRY): several legacy ``to_*`` methods call into each other
# (exposure_id -> datetime_end; day_obs -> datetime_end; observation_id ->
# day_obs). The bodies below are ported VERBATIM from the legacy translator;
# only the way the date is read from the header (a self-free equivalent of
# ``FitsTranslator._from_fits_date(key, scale="utc")``) changes.
# ---------------------------------------------------------------------------


def _from_fits_date_utc(header, key):
    """Self-free equivalent of ``FitsTranslator._from_fits_date(key, scale='utc')``.

    Returns an ``astropy.time.Time`` if the key is present and defined,
    otherwise ``None``. Equivalent to ``Time(value, format='isot', scale='utc')``.
    """
    import astropy.time

    value = header.get(key)
    if value is None:
        return None
    return astropy.time.Time(value, format="isot", scale="utc")


def _datetime_begin(header):
    """Use DATE-BEG if present; otherwise fall back to DATE-OBS."""
    t = _from_fits_date_utc(header, "DATE-BEG")
    if t is not None:
        return t
    return _from_fits_date_utc(header, "DATE-OBS")


def _datetime_end(header):
    """Prefer DATE-END; if missing or earlier than begin, use begin + EXPTIME.

    This also handles EXPTIME==0 (bias) by returning 'begin'.
    """
    import astropy.time

    begin = _datetime_begin(header)

    end = _from_fits_date_utc(header, "DATE-END")
    if end is None or (begin is not None and end < begin):
        exptime = float(header.get("EXPTIME", 0.0) or 0.0)
        if begin is not None:
            if exptime > 0.0:
                # Use TAI for a pure elapsed-time delta; choice doesn’t matter
                # as long as begin and end are compared consistently.
                end = begin + astropy.time.TimeDelta(exptime, format="sec", scale="tai")
            else:
                end = begin
    return end


def _day_obs(header):
    """Derive day_obs (YYYYMMDD int) from the end-of-exposure datetime."""
    return int(_datetime_end(header).datetime.strftime("%Y%m%d"))


# ---------------------------------------------------------------------------
# Quirk hooks: bodies ported VERBATIM from the legacy NickelTranslator.
# ---------------------------------------------------------------------------


@hook(profile)
def observation_type(header):
    """Return one of: object | flat | bias | dark | focus."""
    obstype = header.get("OBSTYPE", "").strip().lower()
    obj = header.get("OBJECT", "").strip().lower()

    # Explicit types
    if obstype == "dark":
        return "bias" if "bias" in obj else "dark"
    if obstype == "flat" or "flat" in obj:
        return "flat"

    # Focus/pointing/tests
    if any(w in obj for w in ("focus", "focusing", "point")):
        return "focus"
    if "test" in obj or "post" in obj:
        return "focus"

    if "bias" in obj:
        return "bias"

    return "science"


@hook(profile)
def observation_reason(header):
    object_str = header.get("OBJECT", "").strip().lower()
    if any(w in object_str for w in ("flat", "bias", "dark")):
        return "calibration"
    if "focus" in object_str:
        return "focus"
    if "test" in object_str or "post" in object_str:
        return "test"
    if object_str == "point":
        return "pointing"
    return "science"


@hook(profile)
def temperature(header):
    import astropy.units as u

    temp_celsius = header.get("TEMPDET", -999.0)
    return (temp_celsius + 273.15) * u.K


@hook(profile)
def exposure_id(header):
    """Unique exposure/visit ID that fits in 31 bits.

    ID = (days_since_2000 * 10000) + OBSNUM
    """
    obsnum = int(header["OBSNUM"])
    return make_exposure_id(_datetime_end(header), obsnum)


@hook(profile)
def visit_id(header):
    return exposure_id(header)


@hook(profile)
def datetime_begin(header):
    """Use DATE-BEG if present; otherwise fall back to DATE-OBS."""
    return _datetime_begin(header)


@hook(profile)
def datetime_end(header):
    """Prefer DATE-END; if missing or earlier than begin, use begin + EXPTIME."""
    return _datetime_end(header)


@hook(profile)
def day_obs(header):
    """Observing day as YYYYMMDD (UTC), using only DATE."""
    return _day_obs(header)


@hook(profile)
def observation_id(header):
    """String ID that must be globally unique for the instrument."""
    return f"{_day_obs(header):08d}_{int(header.get('OBSNUM', 0))}"


@hook(profile)
def unknown_filter(header, raw):
    log.warning("Unrecognized FILTNAM %r, falling back to 'clear'", raw)
    return "clear"


@hook(profile)
def tracking_radec(header, default=None):
    """Get tracking RA/Dec with validation against telescope position.

    Nickel telescope has a known issue where CRVAL1/CRVAL2 (WCS keywords)
    sometimes disagree with RA/DEC (telescope control system keywords).
    This implements a tiered approach:

    1. Try CRVAL1/CRVAL2 (preferred WCS solution)
    2. If available, compare with RA/DEC keywords
    3. If they disagree by more than tolerance, use RA/DEC instead
    4. Log a warning when coordinates are corrected

    Tolerance is set to 1 degree to catch major discrepancies while
    allowing for small pointing adjustments or proper motion.
    """
    import astropy.units as u
    from astropy.coordinates import Angle, SkyCoord

    tolerance_deg = 1.0  # Degree tolerance for coordinate agreement

    # Try to get CRVAL coordinates (WCS solution). ``default`` is the generic
    # StipsTranslator path, which is exactly
    # ``tracking_from_degree_headers(self, ("RADECSYS","RADESYS"),
    # (("CRVAL1","CRVAL2"),), unit=deg)`` — identical to the legacy CRVAL read.
    crval_coord = None
    try:
        crval_coord = default() if default is not None else None
    except Exception as e:
        log.warning(f"Failed to read CRVAL1/CRVAL2: {e}")

    # Try to get RA/DEC coordinates (telescope control system)
    radec_coord = None
    try:
        # RA/DEC are in sexagesimal format, need to parse them
        ra_str = header.get("RA")
        dec_str = header.get("DEC")

        if ra_str and dec_str:
            # Parse sexagesimal coordinates (HH:MM:SS.SS format)
            ra_angle = Angle(ra_str, unit=u.hourangle)
            dec_angle = Angle(dec_str, unit=u.deg)

            # Get reference frame from RADECSYS/RADESYS
            ref_system = header.get("RADECSYS") or header.get("RADESYS") or "ICRS"

            # Create SkyCoord to match the format from tracking_from_degree_headers
            radec_coord = SkyCoord(ra_angle, dec_angle, frame=ref_system.lower())
    except Exception as e:
        log.debug(f"Failed to read RA/DEC keywords: {e}")

    # If we have both, validate they agree
    if crval_coord and radec_coord:
        # Extract RA/Dec values from SkyCoord objects
        crval_ra = crval_coord.ra.to(u.deg).value
        crval_dec = crval_coord.dec.to(u.deg).value
        radec_ra = radec_coord.ra.to(u.deg).value
        radec_dec = radec_coord.dec.to(u.deg).value

        # Calculate angular separation
        ra_diff = abs(crval_ra - radec_ra)
        dec_diff = abs(crval_dec - radec_dec)

        # Handle RA wrap-around at 0/360 degrees
        if ra_diff > 180:
            ra_diff = 360 - ra_diff

        # Check if coordinates disagree
        if ra_diff > tolerance_deg or dec_diff > tolerance_deg:
            log.warning(
                f"CRVAL1/CRVAL2 ({crval_ra:.4f}, {crval_dec:.4f}) "
                f"disagrees with RA/DEC ({radec_ra:.4f}, {radec_dec:.4f}) "
                f"by ΔRA={ra_diff:.2f}°, ΔDec={dec_diff:.2f}°. "
                f"Using RA/DEC from telescope control system."
            )
            return radec_coord

    # Use CRVAL if available and validated (or RA/DEC not available)
    if crval_coord:
        return crval_coord

    # Fall back to RA/DEC if CRVAL not available
    if radec_coord:
        log.info("CRVAL1/CRVAL2 not available, using RA/DEC keywords")
        return radec_coord

    # If we get here, we have no coordinates at all
    raise ValueError("No valid tracking coordinates found in FITS header")
