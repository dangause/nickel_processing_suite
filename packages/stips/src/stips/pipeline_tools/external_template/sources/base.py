"""The contract every external-template survey adapter implements."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class TemplateSourceError(RuntimeError):
    """Raised when a source cannot produce a usable template.

    Adapters raise this rather than returning None: a falsy return is how a
    "no usable frames" condition degrades into an opaque downstream Butler
    error instead of an actionable message.
    """


class TemplateSource(Protocol):
    """A survey that can supply an external DIA template.

    Deliberately NOT ``@runtime_checkable``: a runtime protocol check only
    verifies member *names* exist, so ``isinstance(x, TemplateSource)`` would
    advertise a conformance guarantee it cannot make. Adapters are resolved by
    name through the ``SOURCES`` registry instead.
    """

    #: Registry key and collection namespace (templates/<name>/<band>).
    name: str
    #: Hard service limit on cutout size in degrees, or None if unlimited.
    max_cutout_deg: float | None
    #: FITS header cards to try, in order, when reading the zeropoint.
    zeropoint_keywords: list[str]
    #: FITS header cards to try, in order, for the detector saturation level.
    #: Empty for sources whose frames cannot saturate (deep stacks), which
    #: makes saturation masking a no-op for them.
    saturation_keywords: list[str]
    #: True when ``fetch`` already ran the coverage/size validators on the file
    #: it returns, so the ingest entry point must not repeat them. Only set this
    #: when validation is structurally part of the fetch (PS1 needs it to decide
    #: whether to fall through to its next download method); the default False
    #: is what gives every other source the check for free.
    fetch_validates_cutout: bool

    def band_map(self, config: Any) -> dict[str, str]:
        """LOCAL science band -> this survey's band name."""

    def default_zeropoint(self, header: Any) -> float:
        """Fallback AB zeropoint when no header card is present."""

    def native_fwhm(self, header: Any) -> float:
        """Seeing FWHM in arcsec for this specific frame."""

    def fetch(
        self,
        ra: float,
        dec: float,
        src_band: str,
        size_deg: float,
        out_dir: Path,
        **kwargs: Any,
    ) -> Path:
        """Download a cutout and return its path, or raise TemplateSourceError."""
