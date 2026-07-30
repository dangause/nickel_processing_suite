"""In-stack tests for the external-template ingest entry point.

``ingest.py`` imports ``lsst.daf.butler`` (and, through ``core``, ``lsst.afw``)
at module scope, so these run only inside an activated LSST stack -- which is
how CI runs the suite. In a plain venv they skip.

They cover the two source-agnostic responsibilities ``ingest.py`` owns between
``adapter.fetch()`` and the Butler ingest: passing the profile's field of view
to the adapter, and validating the fetched cutout for every source.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
from astropy.io import fits

pytest.importorskip("lsst.daf.butler")

from stips.pipeline_tools.external_template import ingest  # noqa: E402
from stips.pipeline_tools.external_template.sources.base import (  # noqa: E402
    TemplateSourceError,
)


class _RecordingSource:
    """Minimal TemplateSource that records fetch kwargs and then bails out.

    Raising ``TemplateSourceError`` keeps ``main()`` from reaching the LSST
    conversion/Butler stages, which need a real repo.
    """

    name = "recorder"
    max_cutout_deg = None
    zeropoint_keywords = ["ZP"]
    fetch_validates_cutout = False

    def __init__(self):
        self.kwargs = None

    def band_map(self, config):
        return {"i": "i"}

    def default_zeropoint(self, header):
        return 25.0

    def native_fwhm(self, header):
        return 2.0

    def fetch(self, ra, dec, src_band, size_deg, out_dir, **kwargs):
        self.kwargs = kwargs
        raise TemplateSourceError("stop here -- kwargs recorded")


def _write_fits(path, *, nx=64, ny=64, scale_deg, crval):
    """Write a tiny TAN-WCS image of a known angular size."""
    data = np.zeros((ny, nx), dtype=np.float32)
    header = fits.Header()
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRPIX1"] = nx / 2.0
    header["CRPIX2"] = ny / 2.0
    header["CRVAL1"] = crval[0]
    header["CRVAL2"] = crval[1]
    header["CD1_1"] = -scale_deg
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = scale_deg
    fits.PrimaryHDU(data=data, header=header).writeto(path, overwrite=True)
    return str(path)


class _FitsSource(_RecordingSource):
    """Fetches by writing a cutout of ``covered_size_deg`` on a side."""

    name = "fitsy"

    def __init__(self, covered_size_deg, crval=(102.2475, -36.0053)):
        super().__init__()
        self.covered_size_deg = covered_size_deg
        self.crval = crval

    def fetch(self, ra, dec, src_band, size_deg, out_dir, **kwargs):
        self.kwargs = kwargs
        out_dir.mkdir(parents=True, exist_ok=True)
        return _write_fits(
            out_dir / "cutout.fits",
            scale_deg=self.covered_size_deg / 64.0,
            crval=self.crval,
        )


def _use_source(monkeypatch, source):
    monkeypatch.setattr(ingest, "get_source", lambda name: source)


def _use_profile(monkeypatch, **attrs):
    import stips.core.config as cfg

    prof = type("_Prof", (), {"ps1_band_map": {}, "template_band_maps": {}, **attrs})
    monkeypatch.setattr(cfg, "load_active_profile", lambda *a, **k: prof())


def _argv(tmp_path, *flags, **over):
    args = {
        "--repo": str(tmp_path / "repo"),
        "--source": "recorder",
        "--ra": "102.2475",
        "--dec": "-36.0053",
        "--band": "i",
        "--collection": "templates/x/i",
        "--size": "0.17",
        "--output-dir": str(tmp_path / "dl"),
    }
    args.update(over)
    return [part for kv in args.items() for part in kv] + list(flags)


def test_profile_fov_is_passed_to_fetch(monkeypatch, tmp_path):
    """The coverage warning's input has a real production source."""
    source = _RecordingSource()
    _use_source(monkeypatch, source)
    _use_profile(
        monkeypatch, fov_arcmin=20.0, template_band_maps={"recorder": {"i": "i"}}
    )

    assert ingest.main(_argv(tmp_path)) == 1
    assert source.kwargs["fov_arcmin"] == pytest.approx(20.0)


def test_fov_is_none_when_profile_declares_none(monkeypatch, tmp_path):
    """A profile with no FOV keeps the warning silent rather than guessing."""
    source = _RecordingSource()
    _use_source(monkeypatch, source)
    _use_profile(
        monkeypatch, fov_arcmin=None, template_band_maps={"recorder": {"i": "i"}}
    )

    assert ingest.main(_argv(tmp_path)) == 1
    assert source.kwargs["fov_arcmin"] is None


def test_skymapper_warns_about_fov_with_a_profile_supplied_value(monkeypatch, caplog):
    """End-to-end: a profile FOV reaches SkyMapper's coverage warning."""
    from stips.pipeline_tools.external_template.sources import skymapper as sm

    fov = ingest._profile_fov_arcmin
    _use_profile(monkeypatch, fov_arcmin=20.0)
    with caplog.at_level(logging.WARNING):
        sm.SkyMapperSource()._warn_if_smaller_than_fov(0.17, fov())
    assert "20.0" in caplog.text
    assert "kernel" in caplog.text.lower()


def test_fetched_cutout_missing_target_aborts(monkeypatch, tmp_path):
    """An off-target cutout is rejected before conversion, for any source."""
    source = _FitsSource(0.17, crval=(200.0, 10.0))
    _use_source(monkeypatch, source)
    _use_profile(monkeypatch, template_band_maps={"fitsy": {"i": "i"}})

    assert ingest.main(_argv(tmp_path, **{"--source": "fitsy"})) == 1


def test_fetched_cutout_smaller_than_requested_aborts(monkeypatch, tmp_path):
    """An edge-trimmed frame passes the 10 kB floor but fails the size check."""
    source = _FitsSource(0.02)
    _use_source(monkeypatch, source)
    _use_profile(monkeypatch, template_band_maps={"fitsy": {"i": "i"}})

    assert ingest.main(_argv(tmp_path, **{"--source": "fitsy"})) == 1


def test_validation_uses_the_clamped_size_not_the_request(monkeypatch, tmp_path):
    """A source that clamps must not then fail its own size check.

    SkyMapper caps at 0.17 deg, so a 0.4 deg request yields a 0.17 deg cutout.
    Validating that against 0.4 would reject every clamped fetch.
    """

    class _Clamped(_FitsSource):
        name = "clamped"
        max_cutout_deg = 0.17

    source = _Clamped(0.17)
    _use_source(monkeypatch, source)
    _use_profile(monkeypatch, template_band_maps={"clamped": {"i": "i"}})

    seen = []
    from stips.pipeline_tools.external_template import imaging

    monkeypatch.setattr(
        imaging, "validate_cutout", lambda *a, **k: seen.append(a) or None
    )
    ingest.main(
        _argv(tmp_path, "--skip-ingest", **{"--source": "clamped", "--size": "0.4"})
    )
    assert seen, "post-fetch validation never ran"
    assert seen[0][-1] == pytest.approx(0.17)


def test_source_that_validates_its_own_fetch_is_not_revalidated(monkeypatch, tmp_path):
    """PS1's multi-method fallback already validates each candidate."""
    calls = []

    class _SelfValidating(_FitsSource):
        name = "selfval"
        fetch_validates_cutout = True

    source = _SelfValidating(0.02)  # would FAIL the size check
    _use_source(monkeypatch, source)
    _use_profile(monkeypatch, template_band_maps={"selfval": {"i": "i"}})

    from stips.pipeline_tools.external_template import imaging

    monkeypatch.setattr(
        imaging,
        "validate_cutout",
        lambda *a, **k: calls.append(a) or "should not be called",
    )
    ingest.main(_argv(tmp_path, "--skip-ingest", **{"--source": "selfval"}))
    assert calls == []


def test_ps1_source_declares_that_it_self_validates():
    from stips.pipeline_tools.external_template.sources.ps1 import PS1Source

    assert PS1Source.fetch_validates_cutout is True
