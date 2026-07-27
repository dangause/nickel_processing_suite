"""Tests for the external-template source registry and the SkyMapper adapter."""

from types import SimpleNamespace

import pytest
from stips.core.pipeline import template_band_map
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


# --- template_band_maps resolution -------------------------------------------


def _config(*, ps1_band_map=None, template_band_maps=None):
    return SimpleNamespace(
        profile=SimpleNamespace(
            ps1_band_map=ps1_band_map or {},
            template_band_maps=template_band_maps or {},
        )
    )


def test_template_band_map_reads_named_source():
    cfg = _config(template_band_maps={"skymapper": {"r": "r", "i": "i"}})
    assert template_band_map(cfg, "skymapper") == {"r": "r", "i": "i"}


def test_template_band_map_ps1_falls_back_to_ps1_band_map():
    """Existing profiles set only ps1_band_map; PS1 must keep working."""
    cfg = _config(ps1_band_map={"r": "r", "i": "i"})
    assert template_band_map(cfg, "ps1") == {"r": "r", "i": "i"}


def test_template_band_map_explicit_ps1_entry_wins_over_fallback():
    cfg = _config(
        ps1_band_map={"r": "r"},
        template_band_maps={"ps1": {"r": "r", "i": "i"}},
    )
    assert template_band_map(cfg, "ps1") == {"r": "r", "i": "i"}


def test_template_band_map_unknown_source_is_empty():
    """Empty means 'this instrument has no templates from that survey'."""
    assert template_band_map(_config(), "skymapper") == {}


def test_ctio1m_profile_excludes_skymapper_v_band():
    """SkyMapper v is a 384nm violet filter, NOT Johnson V (551nm)."""
    # NOTE: this loader lives in lsst.obs.stips, NOT stips.core.config.
    # lsst.obs.stips is an editable install and IS importable in the plain venv
    # (only lsst.afw / lsst.daf.butler are absent). See test_config_yaml.py:248
    # for the same import in an existing passing test.
    from lsst.obs.stips.profile_loader import load_profile_from_dir

    prof = load_profile_from_dir("instruments/ctio1m")
    sm = prof.template_band_maps.get("skymapper", {})
    assert sm == {"r": "r", "i": "i"}
    assert "v" not in sm


def test_adapters_do_not_import_lsst():
    """Adapters must stay importable in a plain venv (no lsst dependency).

    Static scan rather than a sys.modules check, for two reasons:
      * `lsst.obs.stips` IS importable in this venv (it is an editable install);
        only the heavy stack pieces (lsst.afw, lsst.daf.butler) are absent. So a
        sys.modules assertion is not the clean signal it looks like.
      * Mutating sys.modules to isolate the check would poison later tests in the
        same session.
    A source scan also catches FUNCTION-SCOPED lsst imports, which an
    import-and-inspect check never exercises.
    """
    import ast
    import pathlib

    import stips.pipeline_tools.external_template.sources as pkg

    offenders = []
    for path in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "lsst" or name.startswith("lsst."):
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, "adapters must not import lsst: " + "; ".join(offenders)
