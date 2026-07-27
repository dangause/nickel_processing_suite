"""The contract every external-template survey adapter implements."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class TemplateSourceError(RuntimeError):
    """Raised when a source cannot produce a usable template.

    Adapters raise this rather than returning None: a falsy return is how a
    "no usable frames" condition degrades into an opaque downstream Butler
    error instead of an actionable message.
    """


@runtime_checkable
class TemplateSource(Protocol):
    """A survey that can supply an external DIA template."""

    #: Registry key and collection namespace (templates/<name>/<band>).
    name: str
    #: Hard service limit on cutout size in degrees, or None if unlimited.
    max_cutout_deg: float | None
    #: FITS header cards to try, in order, when reading the zeropoint.
    zeropoint_keywords: list[str]

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
