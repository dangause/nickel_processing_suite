"""Tests for the external-template source registry and the SkyMapper adapter."""

import pytest

from stips.pipeline_tools.external_template.sources import (
    SOURCES,
    TemplateSourceError,
    get_source,
)


def test_registry_exposes_ps1_and_skymapper():
    assert set(SOURCES) == {"ps1", "skymapper"}


def test_get_source_returns_adapter():
    assert get_source("skymapper").name == "skymapper"


def test_get_source_rejects_unknown_and_lists_valid():
    with pytest.raises(TemplateSourceError) as excinfo:
        get_source("panstarrs2")
    message = str(excinfo.value)
    assert "panstarrs2" in message
    assert "ps1" in message and "skymapper" in message


def test_skymapper_declares_verified_service_limit():
    """0.17 deg is a hard SIA limit: above it the service returns HTTP 400."""
    assert get_source("skymapper").max_cutout_deg == 0.17


def test_ps1_declares_no_service_limit():
    assert get_source("ps1").max_cutout_deg is None
