"""STIPS instrument profile: the single surface a forking telescope team edits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class Site:
    """Telescope location. If ``name`` is set, the translator uses
    ``EarthLocation.of_site(name)``; otherwise geodetic lat/lon/elev."""

    latitude: float
    longitude: float
    elevation: float
    name: Optional[str] = None


@dataclass(frozen=True)
class Field:
    """One FITS-header to metadata mapping. unit is an astropy unit name (e.g. "s")."""

    key: str
    unit: Optional[str] = None
    default: Any = None


@dataclass(frozen=True)
class CameraSpec:
    """Friendly single-CCD camera description. obs_stips builds the afw Camera
    from this at runtime (alternative to a raw camera/<name>.yaml)."""

    nx: int
    ny: int
    pixel_size_um: float
    plate_scale_arcsec_per_pixel: float
    flip_x: bool = False
    flip_y: bool = False
    name: str | None = None
    serial: str | None = None
    gain: float = 1.0
    read_noise: float = 0.0
    saturation: float = 65535.0


@dataclass(frozen=True)
class CrosstalkSpec:
    """Declarative intra-detector crosstalk coefficients for a multi-amp camera.

    ``coeffs`` is an N×N matrix (N = number of amplifiers) where ``coeffs[i][j]``
    is the fraction of amplifier ``j``'s signal that appears, spuriously, in
    amplifier ``i`` — the LSST ``CrosstalkCalib`` convention (amp index ``i``
    matches ``detector.getAmplifiers()[i]``). The diagonal is zero (an amp does
    not cross-talk into itself). ``units`` maps to
    ``CrosstalkCalib.crosstalkRatiosUnits`` ("adu" or "electron").

    This is stack-free and validates only structure (square, zero diagonal, N≥2).
    The N == camera-amp-count check happens at build time, where the camera is
    available.
    """

    coeffs: list[list[float]]
    units: str = "adu"

    def __post_init__(self) -> None:
        n = len(self.coeffs)
        if n < 2:
            raise ValueError(f"crosstalk needs at least 2 amplifiers, got {n}x{n}")
        for i, row in enumerate(self.coeffs):
            if len(row) != n:
                raise ValueError(
                    f"crosstalk matrix must be square; row {i} has "
                    f"{len(row)} entries, expected {n}"
                )
            if row[i] != 0.0:
                raise ValueError(
                    f"crosstalk diagonal must be zero; coeffs[{i}][{i}]={row[i]}"
                )

    @property
    def n_amp(self) -> int:
        """Number of amplifiers (matrix dimension)."""
        return len(self.coeffs)


@dataclass
class InstrumentProfile:
    """Everything instrument-specific, in one object.

    The two filter fields point in opposite directions: ``filters`` maps
    physical_filter->band; ``filter_aliases`` maps raw header values->physical_filter.
    """

    name: str
    site: Site
    # physical_filter -> band (canonical registry; drives FilterDefinitionCollection)
    filters: dict[str, str | None]
    header_map: dict[str, Field]
    # Either a path to a raw LSST camera/<name>.yaml, or a friendly CameraSpec
    # (obs_stips builds the afw Camera from a CameraSpec at runtime).
    camera: str | CameraSpec
    filter_key: str = "FILTNAM"
    # Substring matched (case-insensitive) against the FITS INSTRUME header to
    # decide whether this profile's translator handles a file. Defaults to
    # `name`; set it when the instrument name differs from INSTRUME (e.g. name
    # "CTIO1m" but INSTRUME "Y4KCam").
    instrument_header_value: Optional[str] = None
    # raw FITS filter value -> physical_filter (case-insensitive lookup; drives to_physical_filter)
    filter_aliases: dict[str, str] = field(default_factory=dict)
    const_map: dict[str, Any] = field(default_factory=dict)
    night_to_dayobs_offset_days: int = 1
    # FQ instrument class path for butler register-instrument,
    # e.g. "lsst.obs.stips.active.Instrument"
    instrument_class: Optional[str] = None
    policy_name: Optional[str] = None
    collection_prefix: Optional[str] = None
    skymap_name: Optional[str] = None
    skymap_collection: Optional[str] = None
    # ISR config overrides applied (as `pipetask -c <isr_label>:<key>=<value>`) to
    # every ISR invocation — calib build (`cpBiasIsr`/`cpFlatIsr`) and science
    # (`isr`) — so the master bias/flat and the science frames they correct stay
    # consistent. Lets an instrument toggle ISR steps without forking the shared
    # pipelines, e.g. `{"doDefect": False}` (no defect maps) or
    # `{"overscan.doParallelOverscan": True}` (multi-amp parallel overscan).
    isr_overrides: dict[str, Any] = field(default_factory=dict)
    # Declarative intra-detector crosstalk for multi-amp cameras. When set, STIPS
    # builds a CrosstalkCalib from this matrix, certifies it into the calib chain,
    # and enables ISR crosstalk correction. None disables crosstalk entirely.
    crosstalk: Optional["CrosstalkSpec"] = None
    # Name of an optional EUPS data package of curated calibrations (defects,
    # crosstalk, ...), e.g. "obs_nickel_data". STIPS eups-setup's it into the
    # stack environment when its directory resolves (see ``package_dir``).
    obs_data_package: Optional[str] = None
    # Explicit override for where ``obs_data_package`` lives on disk. Absolute
    # paths are used as-is; a relative path is resolved against the active
    # instrument dir (INSTRUMENT_DIR), so a fork can co-locate the data package
    # under its own instruments/<x>/ tree (e.g. package_dir="obs_<x>_data").
    # When None, STIPS looks for <instrument_dir>/<obs_data_package> and then the
    # reference packages/<obs_data_package> layout. See
    # ``stips.core.config.resolve_data_package_dir`` for the full precedence.
    package_dir: Optional[str] = None
    refcat_path: Optional[str] = None
    # PS1-template policy: maps a LOCAL science band name -> the PS1 band name to
    # download for it (orientation is LOCAL -> PS1, the direction the framework
    # asks in: "given this local science band, is it PS1-eligible and which PS1
    # cutout do I fetch?"). The map's KEYS are the local bands eligible for
    # external PS1 templates; every other band falls back to a coadd template in
    # "auto" mode. PS1 serves grizy, so a Johnson-Cousins instrument like Nickel
    # maps only its r/i bands (``{"r": "r", "i": "i"}``); a Sloan fork could add
    # ``{"g": "g"}``. The default (empty dict) means "no PS1 templates" — the safe
    # choice for an unknown fork, which then uses coadd templates for every band.
    ps1_band_map: dict[str, str] = field(default_factory=dict)
    # Per-source external-template band policy: SOURCE NAME -> (LOCAL band ->
    # that survey's band). Distinct from ``ps1_band_map`` above, which is
    # ALSO consumed by the in-stack refcat configs via STIPS_PS1_BAND_MAP and
    # therefore cannot be generalized away. For source "ps1" this field takes
    # precedence when present and falls back to ``ps1_band_map`` when absent,
    # so existing profiles keep working untouched.
    #
    # Band names are NOT interchangeable across surveys: SkyMapper's "v" is a
    # ~384nm violet filter, not Johnson V (~551nm). Map deliberately.
    template_band_maps: dict[str, dict[str, str]] = field(default_factory=dict)
    # Approximate science field of view in ARCMIN (the long dimension is fine —
    # this is an order-of-magnitude figure, not geometry). Its only consumer is
    # the external-template coverage warning: a survey cutout smaller than the
    # FOV leaves dithered pointings with no PSF-matching kernel candidates
    # (NoKernelCandidatesError), and even a mosaicked SkyMapper template stops
    # at the CCD's 17' short axis, under the FOV of some 1-m-class cameras
    # (Y4KCam is ~20'). Camera geometry is not a reliable
    # substitute (binning, partial illumination), so this is declared, not
    # derived. None means "not measured" and keeps the warning silent.
    fov_arcmin: Optional[float] = None
    # Optional data-fetch hook. Signature:
    #   fetch_data(night: str, config: Config, *, overwrite: bool = False) -> str
    # Returns one of "ok" | "not_found" | "failed". When None, `stips download`
    # reports that download is not configured for this instrument (no crash).
    fetch_data: Optional[Callable] = None
    hooks: dict[str, Callable] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.policy_name is None:
            self.policy_name = self.name
        if self.collection_prefix is None:
            self.collection_prefix = self.name


def hook(profile: InstrumentProfile, name: Optional[str] = None) -> Callable:
    """Decorator: register a quirk override on a profile, keyed by function name."""

    def deco(fn: Callable) -> Callable:
        profile.hooks[name or fn.__name__] = fn
        return fn

    return deco


def coerce_date(value):
    """Coerce a ``datetime``/``date``/``astropy.time.Time``/ISO-string/``None`` to a
    ``datetime.date`` (or ``None``). Fail-closed: unrecognized or unparseable input
    returns ``None`` rather than raising.

    Shared by both sides of the venv/stack boundary (the in-stack translator's
    date-window lookup and the venv orchestrator's coverage check) so the coercion
    rules cannot drift between two copies.
    """
    import datetime as _dt

    if value is None:
        return None
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.datetime):
        return value.date()
    to_dt = getattr(value, "datetime", None)  # astropy.time.Time
    if to_dt is not None:
        return to_dt.date()
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# Epoch for the reference 31-bit-safe exposure-id scheme (days since 2000-01-01).
EXPOSURE_ID_EPOCH = "2000-01-01T00:00:00"


def pack_exposure_id(days_since_2000: int, seqnum: int) -> int:
    """Pack a day number + sequence number into a 31-bit exposure id.

    ``id = days_since_2000 * 10000 + seqnum``. A full ``YYYYMMDD`` date * 10000
    overflows 31 bits, so the days-since-:data:`EXPOSURE_ID_EPOCH` term keeps the
    id within the signed 31-bit range required by the LSST ``exposure``/``visit``
    dimensions.

    This is the low-level packer. Instruments differ ONLY in which day number is
    correct for them, which is the reason this is separate from
    :func:`make_exposure_id`:

    - Nickel takes the UT day of the end-of-exposure time (via
      :func:`make_exposure_id`); its whole observing night lands on one UT day.
    - CTIO takes the LOCAL observing night parsed from the frame filename. At
      ~-70deg longitude a local night straddles UT midnight and the Y4KCam seqnum
      resets each local night, so the UT day is NOT a unique key — night N's
      post-midnight frames and night N+1's afternoon calibs collide on it.

    Raises ``ValueError`` if ``seqnum`` does not fit the low 4 digits (it would
    silently carry into the day term and alias onto another day's id), or if the
    packed id does not fit in 31 bits.
    """
    seqnum = int(seqnum)
    if not 0 <= seqnum < 10000:
        raise ValueError(
            f"seqnum {seqnum} is out of range [0, 10000); it would carry into "
            "the day term and alias onto a different day's exposure_id"
        )
    exposure_id = int(days_since_2000) * 10000 + seqnum
    if exposure_id >= 2**31:
        raise ValueError(f"exposure_id {exposure_id} is out of 31-bit range")
    return exposure_id


def make_exposure_id(end_time: Any, seqnum: int) -> int:
    """Pack an end-of-exposure time + sequence number into a 31-bit exposure id.

    Derives ``days_since_2000`` from ``end_time`` (an ``astropy.time.Time``) and
    delegates the packing and range checks to :func:`pack_exposure_id`.

    Instrument profiles whose observing night maps 1:1 onto a UT day call this
    from their ``exposure_id`` hook; only the ``seqnum`` source differs (e.g.
    Nickel reads ``OBSNUM``). Profiles whose local night straddles UT midnight
    (e.g. CTIO) must NOT use this — see :func:`pack_exposure_id`.
    """
    import astropy.time

    epoch0 = astropy.time.Time(EXPOSURE_ID_EPOCH, scale="utc")
    days = int((end_time - epoch0).to_value("day"))
    return pack_exposure_id(days, seqnum)
