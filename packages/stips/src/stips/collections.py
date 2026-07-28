"""Butler collection-name builder, parameterized by instrument collection prefix."""

from __future__ import annotations

from datetime import datetime, timezone


def generate_run_timestamp() -> str:
    """Generate a UTC timestamp for collection naming."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# Template collections are deliberately NOT prefix-parameterized: they are
# shared across instruments (a PS1 cutout or a coadd of a given tract is the
# same product regardless of which telescope's science it templates). These
# module-level builders centralize the spelling without threading a prefix.


def template_external(source: str, band: str) -> str:
    """External-survey template collection (e.g. ``templates/skymapper/i``).

    ``source`` is a ``TemplateSource`` registry key. Callers that iterate the
    registry use this rather than spelling the f-string at each site.
    """
    return f"templates/{source}/{band}"


def template_external_glob(source: str) -> str:
    """Glob matching all template collections from one external survey."""
    return f"templates/{source}/*"


def template_ps1(band: str) -> str:
    """PS1 external-template collection for ``band`` (e.g. ``templates/ps1/r``).

    PS1 keeps a named builder because it is the one external source the ``auto``
    template strategy resolves by name (see ``dia.find_template``).
    """
    return template_external("ps1", band)


def template_ps1_glob() -> str:
    """Glob matching all PS1 template collections."""
    return template_external_glob("ps1")


def template_deep(tract: int | str, band: str) -> str:
    """Coadd (deep) template collection for ``tract``/``band``.

    ``tract`` accepts an ``int`` (the usual case) or a string placeholder such
    as ``"<TBD>"`` used by dry-run reporting when the real tract is not yet
    computed.
    """
    return f"templates/deep/tract{tract}/{band}"


def template_deep_run(tract: int | str, band: str, run_ts: str) -> str:
    """Timestamped RUN collection under a coadd-template parent."""
    return f"{template_deep(tract, band)}/{run_ts}"


def template_deep_glob() -> str:
    """Glob matching all coadd (deep) template collections."""
    return "templates/deep/*/*"


class CollectionNames:
    """Generate standard collection names for a pipeline run."""

    def __init__(self, night: str, run_ts: str | None = None, *, prefix: str):
        self.night = night
        self.run_ts = run_ts or generate_run_timestamp()
        self.prefix = prefix

    # Raw data
    @property
    def raw_run(self) -> str:
        return f"{self.prefix}/raw/{self.night}/{self.run_ts}"

    # Calibration products
    @property
    def cp_bias(self) -> str:
        return f"{self.prefix}/cp/{self.night}/bias/{self.run_ts}"

    @property
    def cp_bias_run(self) -> str:
        return f"{self.cp_bias}/run"

    @property
    def cp_flat(self) -> str:
        return f"{self.prefix}/cp/{self.night}/flat/{self.run_ts}"

    @property
    def cp_flat_run(self) -> str:
        return f"{self.cp_flat}/run"

    @property
    def curated_run(self) -> str:
        return f"{self.prefix}/calib/curated/{self.run_ts}"

    @property
    def curated_chain(self) -> str:
        return f"{self.prefix}/calib/curated"

    @property
    def crosstalk_gen(self) -> str:
        """RUN holding a freshly built/measured CrosstalkCalib (pre-certification)."""
        return f"{self.prefix}/calib/crosstalk/gen/{self.run_ts}"

    @property
    def crosstalk_calib(self) -> str:
        """CALIBRATION collection the crosstalk calib is certified into.

        Chained into ``curated_chain`` so ISR resolves it as the ``crosstalk``
        prerequisite input. Detector-scoped and timeless, so it carries no night.
        """
        return f"{self.prefix}/calib/crosstalk"

    @property
    def calib_out(self) -> str:
        return f"{self.prefix}/calib/{self.night}"

    @property
    def calib_chain(self) -> str:
        return f"{self.prefix}/calib/current"

    # Science processing
    @property
    def science_parent(self) -> str:
        return f"{self.prefix}/runs/{self.night}/processCcd/{self.run_ts}"

    @property
    def science_run(self) -> str:
        return f"{self.science_parent}/run"

    @property
    def coadd_parent(self) -> str:
        return f"{self.prefix}/runs/{self.night}/coadd/{self.run_ts}"

    @property
    def coadd_run(self) -> str:
        return f"{self.coadd_parent}/run"

    # Difference imaging
    @property
    def diff_parent(self) -> str:
        return f"{self.prefix}/runs/{self.night}/diff/{self.run_ts}"

    @property
    def diff_run(self) -> str:
        return f"{self.diff_parent}/run"

    # Forced photometry
    def forced_phot_parent(self, image_type: str, band: str | None = None) -> str:
        """CHAINED parent for forced photometry on ``image_type`` images.

        ``image_type`` is ``"visit"`` or ``"diffim"``; ``band`` (optional)
        suffixes the collection (e.g. ``diffim_r``) so per-band runs stay
        separate, matching the per-night per-band fphot layout.
        """
        band_suffix = f"_{band}" if band else ""
        return (
            f"{self.prefix}/runs/{self.night}/forcedPhotRaDec/"
            f"{self.run_ts}/{image_type}{band_suffix}"
        )

    def forced_phot_run(self, image_type: str, band: str | None = None) -> str:
        """RUN collection under :meth:`forced_phot_parent`."""
        return f"{self.forced_phot_parent(image_type, band)}/run"

    # Differential photometry
    @property
    def differential_phot(self) -> str:
        """Output collection for LSST differential photometry (no run timestamp)."""
        return f"{self.prefix}/runs/{self.night}/differentialPhot"

    # Glob patterns (for discovery / cleanup across nights and timestamps)
    @classmethod
    def science_glob(cls, prefix: str) -> str:
        """Glob matching every science (processCcd) collection for ``prefix``."""
        return f"{prefix}/runs/*/processCcd/*"

    @classmethod
    def forced_phot_glob(cls, prefix: str, *, night: str = "*", tail: str = "*") -> str:
        """Glob over forced-photometry collections under ``runs/{night}``.

        ``tail`` is the pattern after ``forcedPhotRaDec/`` (default ``*`` matches
        the whole subtree). Callers pin ``night`` and/or a more specific ``tail``
        such as ``"*/diffim*"`` or ``"*/run"``.
        """
        return f"{prefix}/runs/{night}/forcedPhotRaDec/{tail}"
