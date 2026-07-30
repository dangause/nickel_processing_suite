"""Registry of external-template survey adapters."""

from __future__ import annotations

from .base import TemplateSource, TemplateSourceError
from .ps1 import PS1Source
from .skymapper import SkyMapperSource

SOURCES: dict[str, TemplateSource] = {
    "ps1": PS1Source(),
    "skymapper": SkyMapperSource(),
}


def get_source(name: str) -> TemplateSource:
    """Look up an adapter by name, naming the valid options on failure."""
    try:
        return SOURCES[name]
    except KeyError:
        valid = ", ".join(sorted(SOURCES))
        raise TemplateSourceError(
            f"Unknown template source {name!r}; valid sources: {valid}"
        ) from None


__all__ = ["SOURCES", "TemplateSource", "TemplateSourceError", "get_source"]
