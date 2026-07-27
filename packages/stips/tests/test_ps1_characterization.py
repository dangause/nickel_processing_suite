"""Characterization guard for the PS1 -> LSST Exposure conversion.

Pins the OBSERVABLE behavior of the current converter so the extraction in
Task 2 is provably behavior-preserving. Runs only inside the LSST stack; the
plain-venv arithmetic checks live in test_external_template_imaging.py.
"""

import numpy as np
import pytest
from astropy.io import fits

pytest.importorskip("lsst.afw.image")


@pytest.fixture
def ps1_fits(tmp_path):
    """A synthetic PS1-like FITS with a plain TAN WCS and a known zeropoint."""
    ny = nx = 64
    rng = np.random.default_rng(1234)
    data = rng.normal(loc=10.0, scale=2.0, size=(ny, nx)).astype(np.float32)
    data[32, 32] = 500.0

    hdr = fits.Header()
    hdr["CTYPE1"] = "RA---TAN"
    hdr["CTYPE2"] = "DEC--TAN"
    hdr["CRPIX1"] = nx / 2
    hdr["CRPIX2"] = ny / 2
    hdr["CRVAL1"] = 210.910750
    hdr["CRVAL2"] = 54.311694
    hdr["CD1_1"] = -0.25 / 3600
    hdr["CD1_2"] = 0.0
    hdr["CD2_1"] = 0.0
    hdr["CD2_2"] = 0.25 / 3600
    hdr["FILTER"] = "r"
    hdr["ZPT"] = 25.0

    path = tmp_path / "ps1_r.fits"
    fits.PrimaryHDU(data=data, header=hdr).writeto(path)
    return str(path)


def _convert(ps1_fits):
    from stips.pipeline_tools.ingest_ps1_template import convert_ps1_to_lsst_exposure

    return convert_ps1_to_lsst_exposure(ps1_fits, "r")


def test_exposure_geometry_is_pinned(ps1_fits):
    exp = _convert(ps1_fits)
    assert exp.getBBox().getWidth() == 64
    assert exp.getBBox().getHeight() == 64


def test_photocalib_is_identity_and_pixels_are_njy(ps1_fits):
    """ZPT=25.0 -> 363.078 nJy/ADU; pixels are pre-scaled, PhotoCalib is 1.0."""
    exp = _convert(ps1_fits)
    assert exp.getPhotoCalib().getCalibrationMean() == pytest.approx(1.0)

    expected_factor = 3631e9 * 10 ** (-0.4 * 25.0)
    assert expected_factor == pytest.approx(363.078, rel=1e-4)
    # The injected 500.0 ADU source becomes 500 * factor nJy.
    assert exp.image.array[32, 32] == pytest.approx(500.0 * expected_factor, rel=1e-4)


def test_filter_label_is_local_band(ps1_fits):
    exp = _convert(ps1_fits)
    assert exp.getFilter().bandLabel == "r"


def test_psf_sigma_from_default_fwhm(ps1_fits):
    """PS1 assumes 1.2 arcsec FWHM; at 0.25 arcsec/px that is 2.039 px sigma."""
    import lsst.geom as geom

    exp = _convert(ps1_fits)
    bbox = exp.getBBox()
    center = geom.Point2D(bbox.getCenterX(), bbox.getCenterY())
    sigma = exp.getPsf().computeShape(center).getDeterminantRadius()
    assert sigma == pytest.approx(1.2 / 0.25 / 2.3548, rel=0.05)


def test_wcs_roundtrips_to_crval(ps1_fits):
    import lsst.geom as geom

    exp = _convert(ps1_fits)
    sky = exp.getWcs().pixelToSky(geom.Point2D(32.0, 32.0))
    assert sky.getRa().asDegrees() == pytest.approx(210.910750, abs=1e-3)
    assert sky.getDec().asDegrees() == pytest.approx(54.311694, abs=1e-3)
