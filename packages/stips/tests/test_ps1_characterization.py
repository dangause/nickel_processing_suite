"""Characterization guard for the PS1 -> LSST Exposure conversion.

Pins the OBSERVABLE behavior of the current converter so the extraction in
Task 2 is provably behavior-preserving. Runs only inside the LSST stack; the
plain-venv arithmetic checks live in test_external_template_imaging.py.
"""

import numpy as np
import pytest
from astropy.io import fits

pytest.importorskip("lsst.afw.image")


def _make_ps1_fits(tmp_path, filename="ps1_r.fits", filter_label="r"):
    """Build a synthetic PS1-like FITS with a plain TAN WCS and a known zeropoint.

    Non-square (nx=64, ny=48): a row/col transposition in the converter would
    change which axis is which and get caught by the width/height assertions.
    The bright source is placed at an asymmetric pixel (row=20, col=40) so a
    transposition would also move it to a location the test isn't looking at.
    """
    nx, ny = 64, 48
    rng = np.random.default_rng(1234)
    data = rng.normal(loc=10.0, scale=2.0, size=(ny, nx)).astype(np.float32)
    data[20, 40] = 500.0

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
    hdr["FILTER"] = filter_label
    hdr["ZPT"] = 25.0

    path = tmp_path / filename
    fits.PrimaryHDU(data=data, header=hdr).writeto(path)
    return str(path)


@pytest.fixture
def ps1_fits(tmp_path):
    """A synthetic PS1-like FITS with header FILTER='r'.

    This matches the local band ("r") that `_convert` passes, so on its own
    this fixture cannot distinguish "uses the function argument" from "uses
    the header value" -- see test_filter_label_uses_argument_not_header for
    the fixture that decouples the two.
    """
    return _make_ps1_fits(tmp_path, filter_label="r")


def _convert(ps1_fits):
    from stips.pipeline_tools.ingest_ps1_template import convert_ps1_to_lsst_exposure

    return convert_ps1_to_lsst_exposure(ps1_fits, "r")


def test_exposure_geometry_is_pinned(ps1_fits):
    exp = _convert(ps1_fits)
    # Non-square fixture (nx=64, ny=48): pins width and height independently
    # so a row/col transposition in the converter would be caught.
    assert exp.getBBox().getWidth() == 64
    assert exp.getBBox().getHeight() == 48


def test_photocalib_is_identity_and_pixels_are_njy(ps1_fits):
    """ZPT=25.0 -> 363.078 nJy/ADU; pixels are pre-scaled, PhotoCalib is 1.0."""
    exp = _convert(ps1_fits)
    assert exp.getPhotoCalib().getCalibrationMean() == pytest.approx(1.0)

    expected_factor = 3631e9 * 10 ** (-0.4 * 25.0)
    assert expected_factor == pytest.approx(363.078, rel=1e-4)
    # The injected 500.0 ADU source (row=20, col=40) becomes 500 * factor nJy.
    assert exp.image.array[20, 40] == pytest.approx(500.0 * expected_factor, rel=1e-4)


def test_filter_label_is_local_band(ps1_fits):
    """Baseline case: header FILTER ('r') agrees with the local-band argument
    ('r'), so this alone does not prove which source flows through. Kept for
    the straightforward "current behavior" pin; the decoupled proof is in
    test_filter_label_uses_argument_not_header below.
    """
    exp = _convert(ps1_fits)
    assert exp.getFilter().bandLabel == "r"


def test_filter_label_uses_argument_not_header(tmp_path):
    """The header FILTER ('i') and the local-band argument ('r') disagree.

    `convert_ps1_to_lsst_exposure` currently builds `FilterLabel(band=nickel_band)`
    -- i.e. it uses the caller-supplied ARGUMENT and ignores the header-parsed
    `ps1_filter`. A refactor that accidentally threaded the parsed header
    filter into FilterLabel instead would flip this to "i" and fail here (it
    would NOT be caught by test_filter_label_is_local_band above, since that
    fixture's header and argument agree).
    """
    from stips.pipeline_tools.ingest_ps1_template import convert_ps1_to_lsst_exposure

    path = _make_ps1_fits(tmp_path, filename="ps1_i_header.fits", filter_label="i")
    exp = convert_ps1_to_lsst_exposure(path, "r")
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
    """Pin the WCS conversion at an off-center pixel with a tight tolerance.

    Point2D(32, 32) (the old check) sits at/next to CRPIX, and for a TAN
    projection the reference pixel maps to CRVAL regardless of the CD
    matrix's scale, rotation, or parity -- so it mostly proved "CRVAL was
    copied", not that the CD-matrix conversion is preserved. It also used
    abs=1e-3 deg (~3.6 arcsec), ~14x the fixture's own 0.25 arcsec/px scale,
    so a multi-pixel indexing or parity error would still have passed.

    Here we evaluate at the (0, 0) corner -- far from CRPIX=(32, 24) -- with
    a tolerance of abs=1e-5 deg (~0.036 arcsec, ~0.15 px), and compute the
    expected RA/Dec independently via astropy's WCS rather than a
    hand-derived number.
    """
    import lsst.geom as geom
    from astropy.wcs import WCS as AstropyWCS

    header = fits.getheader(ps1_fits)
    astropy_wcs = AstropyWCS(header)

    exp = _convert(ps1_fits)
    wcs = exp.getWcs()

    corner = geom.Point2D(0.0, 0.0)
    sky = wcs.pixelToSky(corner)
    expected_ra, expected_dec = astropy_wcs.all_pix2world(
        corner.getX(), corner.getY(), 0
    )
    assert sky.getRa().asDegrees() == pytest.approx(float(expected_ra), abs=1e-5)
    assert sky.getDec().asDegrees() == pytest.approx(float(expected_dec), abs=1e-5)

    # Sanity check only (not the bite): the reference pixel still maps close
    # to CRVAL.
    center = geom.Point2D(header["CRPIX1"] - 1, header["CRPIX2"] - 1)
    center_sky = wcs.pixelToSky(center)
    assert center_sky.getRa().asDegrees() == pytest.approx(210.910750, abs=1e-3)
    assert center_sky.getDec().asDegrees() == pytest.approx(54.311694, abs=1e-3)
