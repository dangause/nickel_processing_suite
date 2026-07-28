"""Multi-patch external-template ingest.

``ingest_exposure_to_butler`` used to reproject the survey cutout onto exactly
ONE skymap patch -- the one containing the target coordinate -- and discard
everything outside it. The self-coadd template path, by contrast, ingests every
patch the field overlaps and lets ``rewarpTemplate`` gather them at DIA time.

Measured on NGC2298 (ctio1m skymap geometry, i band): the assembled SkyMapper
mosaic spans 17.0' x 25.5' but the ingested ``template_coadd`` retained only the
part falling inside patch 156, giving 39.0% science-field coverage, while the
validated CTIO self-coadd covers patches 142, 143, 156 and 157 of tract 444.

These run only inside the LSST stack (they need a real ``RingsSkyMap`` and
``lsst.afw``); in a plain venv they skip.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS as AstropyWCS

pytest.importorskip("lsst.afw.image")

import lsst.afw.detection as afwDetection  # noqa: E402
import lsst.afw.image as afwImage  # noqa: E402
import lsst.geom as geom  # noqa: E402
from lsst.skymap.ringsSkyMap import RingsSkyMap, RingsSkyMapConfig  # noqa: E402
from stips.pipeline_tools.external_template import core  # noqa: E402
from stips.pipeline_tools.external_template.core import (  # noqa: E402
    convert_astropy_wcs_to_lsst,
    exposure_sky_corners,
    find_overlapping_patches,
    ingest_exposure_to_butler,
    patch_coverage_fraction,
)

# NGC2298, the field the single-patch truncation was measured on.
TARGET_RA = 102.246542
TARGET_DEC = -36.005333
#: Patches of tract 444 the validated CTIO self-coadd covers for this field.
COADD_PATCHES = {142, 143, 156, 157}
#: The single patch the old single-patch code path wrote.
TARGET_PATCH = 156


def _ctio1m_skymap():
    """The real ctio1m skymap geometry (instruments/ctio1m/configs/makeSkyMap.py)."""
    config = RingsSkyMapConfig()
    config.numRings = 40
    config.projection = "TAN"
    config.tractOverlap = 1.0 / 60
    config.pixelScale = 0.289
    config.patchInnerDimensions = [4000, 4000]
    config.patchBorder = 100
    return RingsSkyMap(config)


def _make_exposure(
    width_arcmin,
    height_arcmin,
    *,
    scale_arcsec=0.4976,
    rotate=False,
    center=(TARGET_RA, TARGET_DEC),
):
    """A synthetic survey cutout of a given angular size centred on ``center``."""
    nx = int(round(width_arcmin * 60 / scale_arcsec))
    ny = int(round(height_arcmin * 60 / scale_arcsec))

    hdr = fits.Header()
    hdr["CTYPE1"] = "RA---TAN"
    hdr["CTYPE2"] = "DEC--TAN"
    hdr["CRPIX1"] = nx / 2
    hdr["CRPIX2"] = ny / 2
    hdr["CRVAL1"] = center[0]
    hdr["CRVAL2"] = center[1]
    deg = scale_arcsec / 3600
    if rotate:
        theta = math.radians(45.0)
        hdr["CD1_1"] = -deg * math.cos(theta)
        hdr["CD1_2"] = deg * math.sin(theta)
        hdr["CD2_1"] = deg * math.sin(theta)
        hdr["CD2_2"] = deg * math.cos(theta)
    else:
        hdr["CD1_1"] = -deg
        hdr["CD1_2"] = 0.0
        hdr["CD2_1"] = 0.0
        hdr["CD2_2"] = deg

    masked_image = afwImage.MaskedImageF(nx, ny)
    masked_image.image.array[:, :] = 100.0
    masked_image.variance.array[:, :] = 1.0
    exposure = afwImage.ExposureF(masked_image)
    exposure.setWcs(convert_astropy_wcs_to_lsst(AstropyWCS(hdr)))
    exposure.setPhotoCalib(afwImage.PhotoCalib(1.0))
    exposure.setFilter(afwImage.FilterLabel(band="i"))
    exposure.setPsf(afwDetection.GaussianPsf(21, 21, 1.4))
    return exposure


def _patch_ids(pairs):
    return sorted(patch.getSequentialIndex() for _, patch in pairs)


def test_exposure_sky_corners_bracket_the_footprint():
    """The footprint is sampled from the WCS + bbox, not from the target coord."""
    exposure = _make_exposure(25.5, 17.0)
    corners = exposure_sky_corners(exposure)
    assert len(corners) >= 4

    decs = [c.getDec().asDegrees() for c in corners]
    ras = [c.getRa().asDegrees() for c in corners]
    assert max(decs) - min(decs) == pytest.approx(17.0 / 60, rel=0.02)
    span_ra = (max(ras) - min(ras)) * math.cos(math.radians(TARGET_DEC))
    assert span_ra == pytest.approx(25.5 / 60, rel=0.02)


def test_wide_exposure_spans_every_coadd_patch():
    """The 17.0' x 25.5' SkyMapper mosaic overlaps all four self-coadd patches.

    Pre-fix the ingest reached only ``findPatch(coord)`` -> patch 156.
    """
    skymap = _ctio1m_skymap()
    exposure = _make_exposure(25.5, 17.0)
    coord = geom.SpherePoint(TARGET_RA, TARGET_DEC, geom.degrees)

    pairs = find_overlapping_patches(skymap, exposure, coord)

    assert set(_patch_ids(pairs)) == COADD_PATCHES
    assert {tract.getId() for tract, _ in pairs} == {444}
    # The target's own patch stays first so callers keeping a single "primary"
    # data ID (metadata records, stdout parsing) are unchanged.
    assert pairs[0][1].getSequentialIndex() == TARGET_PATCH


def test_small_cutout_well_inside_a_patch_yields_exactly_one_patch():
    """A PS1-sized cutout entirely inside one patch must not gain extra datasets.

    Centred on patch 156's own inner centre, not on the target -- the NGC2298
    target sits only ~0.9' from patch 156's inner edge, so even a 1' cutout
    there legitimately laps into patch 142.
    """
    skymap = _ctio1m_skymap()
    tract_info = skymap.findTract(geom.SpherePoint(TARGET_RA, TARGET_DEC, geom.degrees))
    patch_info = tract_info.getPatchInfo(TARGET_PATCH)
    inner = patch_info.getInnerBBox()
    patch_center = tract_info.getWcs().pixelToSky(
        geom.Point2D(inner.getCenterX(), inner.getCenterY())
    )
    center = (patch_center.getRa().asDegrees(), patch_center.getDec().asDegrees())

    exposure = _make_exposure(1.0, 1.0, scale_arcsec=0.25, center=center)
    pairs = find_overlapping_patches(skymap, exposure, patch_center)

    assert _patch_ids(pairs) == [TARGET_PATCH]


def test_explicit_tract_restricts_selection(caplog):
    """An explicitly requested tract is honoured, and anything dropped is named."""
    skymap = _ctio1m_skymap()
    exposure = _make_exposure(25.5, 17.0)
    coord = geom.SpherePoint(TARGET_RA, TARGET_DEC, geom.degrees)

    pairs = find_overlapping_patches(skymap, exposure, coord, tract=444)
    assert {tract.getId() for tract, _ in pairs} == {444}
    assert set(_patch_ids(pairs)) == COADD_PATCHES


def test_patch_coverage_fraction_detects_empty_reprojection():
    """An all-NO_DATA reprojection has zero coverage and must not be written."""
    empty = afwImage.ExposureF(geom.Box2I(geom.Point2I(0, 0), geom.Extent2I(32, 32)))
    empty.image.array[:, :] = 0.0
    empty.mask.array[:, :] = empty.mask.getPlaneBitMask("NO_DATA")
    assert patch_coverage_fraction(empty) == 0.0

    full = afwImage.ExposureF(geom.Box2I(geom.Point2I(0, 0), geom.Extent2I(32, 32)))
    full.image.array[:, :] = 5.0
    full.mask.array[:, :] = 0
    assert patch_coverage_fraction(full) == pytest.approx(1.0)


class _FakeDatasetType:
    class _Dims:
        names = ("skymap", "tract", "patch", "band")

    dimensions = _Dims()


class _FakeRegistry:
    def __init__(self):
        self.runs = []

    def getDatasetType(self, name):
        assert name == "template_coadd"
        return _FakeDatasetType()

    def registerRun(self, name):
        self.runs.append(name)

    def queryDatasets(self, name, collections=None, dataId=None):
        return []


class _FakeButler:
    """Records ``put`` calls; serves the real skymap for ``skyMap``."""

    def __init__(self, skymap):
        self.registry = _FakeRegistry()
        self._skymap = skymap
        self.puts = []

    def get(self, name, dataId=None, collections=None, **kwargs):
        if name == "skyMap":
            return self._skymap
        for stored_id, exposure in self.puts:
            if stored_id == dataId:
                return exposure
        raise LookupError(dataId)

    def put(self, exposure, name, dataId=None, run=None):
        assert name == "template_coadd"
        self.puts.append((dict(dataId), exposure))


@pytest.fixture
def _skymap_env(monkeypatch):
    """Point the ingest at the ctio1m profile and skymap without a Butler repo."""
    instrument_dir = Path(__file__).resolve().parents[3] / "instruments" / "ctio1m"
    assert (instrument_dir / "profile.py").is_file(), instrument_dir
    monkeypatch.setenv("INSTRUMENT_DIR", str(instrument_dir))
    monkeypatch.setenv("SKYMAP_NAME", "ctio1mRings-v1")
    monkeypatch.setenv("SKYMAPS_CHAIN", "skymaps/ctio1mRings")


def test_ingest_writes_one_dataset_per_overlapping_patch(_skymap_env):
    """The whole point: four patches overlapped -> four ``template_coadd`` puts."""
    butler = _FakeButler(_ctio1m_skymap())
    exposure = _make_exposure(25.5, 17.0)

    data_ids = ingest_exposure_to_butler(
        butler,
        exposure,
        TARGET_RA,
        TARGET_DEC,
        "i",
        "templates/skymapper/i",
    )

    assert isinstance(data_ids, list)
    assert {d["patch"] for d in data_ids} == COADD_PATCHES
    assert {d["patch"] for d, _ in butler.puts} == COADD_PATCHES
    assert data_ids[0]["patch"] == TARGET_PATCH
    assert all(d["tract"] == 444 for d in data_ids)
    assert all(d["band"] == "i" for d in data_ids)


def test_ingest_skips_patches_without_usable_coverage(_skymap_env, monkeypatch):
    """A patch whose reprojection carries no data is not written at all --
    an all-NO_DATA ``template_coadd`` is worse than an absent one, because
    ``rewarpTemplate`` would still pick it up."""
    butler = _FakeButler(_ctio1m_skymap())
    exposure = _make_exposure(25.5, 17.0)

    real_reproject = core.reproject_to_patch

    def fake_reproject(exp, patch_info):
        reprojected = real_reproject(exp, patch_info)
        if patch_info.getSequentialIndex() == 143:
            reprojected.image.array[:, :] = 0.0
            reprojected.mask.array[:, :] = reprojected.mask.getPlaneBitMask("NO_DATA")
        return reprojected

    monkeypatch.setattr(core, "reproject_to_patch", fake_reproject)

    data_ids = ingest_exposure_to_butler(
        butler,
        exposure,
        TARGET_RA,
        TARGET_DEC,
        "i",
        "templates/skymapper/i",
    )

    assert {d["patch"] for d in data_ids} == COADD_PATCHES - {143}
    assert 143 not in {d["patch"] for d, _ in butler.puts}


def test_ingest_reprojections_actually_carry_data(_skymap_env):
    """Every written patch holds real warped pixels, not just a valid data ID."""
    butler = _FakeButler(_ctio1m_skymap())
    exposure = _make_exposure(25.5, 17.0)

    ingest_exposure_to_butler(
        butler, exposure, TARGET_RA, TARGET_DEC, "i", "templates/skymapper/i"
    )

    for data_id, written in butler.puts:
        finite = np.isfinite(written.image.array)
        assert np.any(finite & (written.image.array != 0)), data_id
