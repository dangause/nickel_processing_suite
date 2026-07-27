"""Band->template policy is profile-driven (F-011).

The "PS1 for r/i, coadd otherwise" policy is NOT hardcoded: it comes from the
active profile's ``ps1_band_map`` (LOCAL band -> PS1 band; keys = PS1-eligible
bands). These tests pin Nickel's historical behavior AND prove a fork with a
different filter set (e.g. Sloan ``{"g": "g"}``) gets PS1 templates for its own
bands without editing the framework.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import stips.core.dia as dia
import stips.core.external_template as external_template
import stips.core.ps1_template as ps1_template
import stips.core.run as run

# Nickel's historical policy, expressed as a profile map.
NICKEL_MAP = {"r": "r", "i": "i"}


def _config(ps1_band_map):
    """A minimal stand-in Config carrying a profile with the given band map."""
    return SimpleNamespace(profile=SimpleNamespace(ps1_band_map=ps1_band_map))


# ---------------------------------------------------------------------------
# dia.find_template(strategy="auto")
# ---------------------------------------------------------------------------


def test_find_template_auto_prefers_ps1_for_ri(monkeypatch):
    monkeypatch.setattr(
        dia.butler_query,
        "collection_exists",
        lambda config, name: name == "templates/ps1/r",
    )
    got = dia.find_template(config=_config(NICKEL_MAP), band="r", strategy="auto")
    assert got == "templates/ps1/r"


def test_find_template_auto_uses_coadd_for_bv(monkeypatch):
    monkeypatch.setattr(
        dia.butler_query, "collection_exists", lambda config, name: True
    )
    monkeypatch.setattr(
        dia.butler_query,
        "list_collections",
        lambda config, pattern, prefix=None: ["templates/deep/tract1/v"],
    )
    # 'v' is not a key of the Nickel map -> coadd, even though a ps1 collection
    # would "exist" per the mock above.
    got = dia.find_template(config=_config(NICKEL_MAP), band="v", strategy="auto")
    assert got == "templates/deep/tract1/v"


def test_find_template_auto_ri_falls_back_to_coadd_without_ps1(monkeypatch):
    monkeypatch.setattr(
        dia.butler_query, "collection_exists", lambda config, name: False
    )
    monkeypatch.setattr(
        dia.butler_query,
        "list_collections",
        lambda config, pattern, prefix=None: ["templates/deep/tract1/r"],
    )
    got = dia.find_template(config=_config(NICKEL_MAP), band="r", strategy="auto")
    assert got == "templates/deep/tract1/r"


def test_find_template_auto_new_capability_ps1_for_g(monkeypatch):
    # A Sloan-style fork declaring {"g": "g"} gets PS1 templates for g with NO
    # framework edits — the whole point of F-011.
    monkeypatch.setattr(
        dia.butler_query,
        "collection_exists",
        lambda config, name: name == "templates/ps1/g",
    )
    got = dia.find_template(config=_config({"g": "g"}), band="g", strategy="auto")
    assert got == "templates/ps1/g"


# ---------------------------------------------------------------------------
# ps1_template.run band validation
# ---------------------------------------------------------------------------


def test_ps1_template_run_rejects_ineligible_band():
    # Nickel parity: 'v' is not PS1-eligible; run() fails early (before any stack
    # call) with a message naming the profile's eligible bands.
    res = ps1_template.run(ra=1.0, dec=2.0, band="v", config=_config(NICKEL_MAP))
    assert res.success is False
    assert res.band == "v"
    # Message names the profile's eligible bands (sorted), not a hardcoded "r/i".
    assert "i, r" in res.error
    assert "v" in res.error


def test_ps1_template_run_rejects_when_no_ps1_bands():
    res = ps1_template.run(ra=1.0, dec=2.0, band="r", config=_config({}))
    assert res.success is False
    assert "(none configured)" in res.error


# ---------------------------------------------------------------------------
# ps1_template.run skip-if-exists policy (F-041: single source of truth)
# ---------------------------------------------------------------------------


def test_ps1_template_run_skips_existing_without_overwrite(monkeypatch):
    # ps1_template.run is now a thin shim over external_template.run (F-054);
    # the skip-if-exists policy and stack dispatch live there, so that's what
    # must be patched for this to intercept.
    monkeypatch.setattr(
        external_template, "check_exists", lambda source, band, config, collection: True
    )
    stack = mock.Mock(side_effect=AssertionError("must not reach the stack on skip"))
    monkeypatch.setattr(external_template, "run_with_stack", stack)

    res = ps1_template.run(ra=1.0, dec=2.0, band="r", config=_config(NICKEL_MAP))

    assert res.success is True
    assert res.skipped is True
    assert res.collection == "templates/ps1/r"
    stack.assert_not_called()


def test_ps1_template_run_overwrite_bypasses_exists_check(monkeypatch):
    exists = mock.Mock(return_value=True)
    monkeypatch.setattr(external_template, "check_exists", exists)
    monkeypatch.setattr(
        external_template,
        "run_with_stack",
        mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr="")),
    )

    cfg = _config(NICKEL_MAP)
    cfg.repo = Path("/tmp/repo")  # output_dir default uses config.repo
    res = ps1_template.run(ra=1.0, dec=2.0, band="r", config=cfg, overwrite=True)

    assert res.success is True
    assert res.skipped is False
    exists.assert_not_called()


# ---------------------------------------------------------------------------
# run._run_auto_templates band split
# ---------------------------------------------------------------------------


def _capture_auto_split(monkeypatch, bands, ps1_band_map, template_nights):
    built = []
    monkeypatch.setattr(
        run,
        "_run_ps1_templates",
        lambda run_cfg, config, result, dry_run, bands=None: built.append(
            ("ps1", tuple(bands))
        ),
    )
    monkeypatch.setattr(
        run,
        "_run_coadd_templates",
        lambda run_cfg, config, result, science_cfg, dry_run, bands=None, executor=None: built.append(
            ("coadd", tuple(bands))
        ),
    )
    cfg = run.RunConfig(
        object_name="x",
        ra=1.0,
        dec=2.0,
        bands=bands,
        template_type="auto",
        template_nights=template_nights,
    )
    out = run._run_auto_templates(
        cfg,
        config=_config(ps1_band_map),
        result=mock.Mock(),
        science_cfg=mock.Mock(),
        dry_run=True,
    )
    return out, built


def test_auto_template_builds_ps1_for_ri_and_coadd_for_bv(monkeypatch):
    # Nickel parity: [b, v, r, i] -> ps1:[r, i], coadd:[b, v].
    out, built = _capture_auto_split(
        monkeypatch,
        bands=["r", "i", "b", "v"],
        ps1_band_map=NICKEL_MAP,
        template_nights=["20230728"],
    )
    assert out is None
    assert ("ps1", ("r", "i")) in built
    assert ("coadd", ("b", "v")) in built


def test_auto_template_new_capability_ps1_for_g(monkeypatch):
    # Fork with {"g": "g"}: g -> ps1, everything else -> coadd.
    _, built = _capture_auto_split(
        monkeypatch,
        bands=["g", "r"],
        ps1_band_map={"g": "g"},
        template_nights=["20230728"],
    )
    assert ("ps1", ("g",)) in built
    assert ("coadd", ("r",)) in built


def test_auto_template_skips_coadd_without_template_nights(monkeypatch):
    _, built = _capture_auto_split(
        monkeypatch,
        bands=["r", "b"],
        ps1_band_map=NICKEL_MAP,
        template_nights=[],
    )
    # PS1 runs for r; coadd skipped because no template_nights for b.
    assert ("ps1", ("r",)) in built
    assert not any(kind == "coadd" for kind, _ in built)


# ---------------------------------------------------------------------------
# SkyMapper is EXPLICIT-ONLY: the "auto" branch must never surface it, while
# legacy/explicit discovery MAY.
# ---------------------------------------------------------------------------


def test_auto_never_returns_skymapper_even_when_present(monkeypatch):
    """SkyMapper is EXPLICIT-ONLY: shallow, ~2" seeing, <=10' wide.

    Auto-selecting it would hand DIA a template worse than the science image.
    """
    from stips.core import dia

    monkeypatch.setattr(
        dia.butler_query, "collection_exists", lambda config, name: False
    )
    monkeypatch.setattr(
        dia.butler_query,
        "list_collections",
        lambda config, pattern, prefix=None: (
            ["templates/skymapper/i"] if "skymapper" in pattern else []
        ),
    )
    result = dia.find_template(_config({"r": "r", "i": "i"}), band="i", strategy="auto")
    assert result is None


def test_legacy_discovery_can_return_skymapper(monkeypatch):
    """Explicit/legacy discovery MAY surface it — that is the opt-in path."""
    from stips.core import dia

    monkeypatch.setattr(
        dia.butler_query,
        "list_collections",
        lambda config, pattern, prefix=None: (
            ["templates/skymapper/i"] if "skymapper" in pattern else []
        ),
    )
    result = dia.find_template(_config({"r": "r", "i": "i"}), band="i")
    assert result == "templates/skymapper/i"
