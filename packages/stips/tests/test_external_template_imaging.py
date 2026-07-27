"""Tests for the astropy-only external-template helpers.

These deliberately import NO lsst modules, so unlike test_ps1_templates.py
they run in a plain venv.
"""

import logging

import numpy as np
import pytest
from astropy.io import fits

from stips.pipeline_tools.external_template import imaging

LOG = logging.getLogger(__name__)


def _write_fits(path, nx, ny, *, crval=(102.2475, -36.0053), scale_arcsec=0.4976):
    hdr = fits.Header()
    hdr["CTYPE1"] = "RA---TAN"
    hdr["CTYPE2"] = "DEC--TAN"
    hdr["CRPIX1"] = nx / 2
    hdr["CRPIX2"] = ny / 2
    hdr["CRVAL1"] = crval[0]
    hdr["CRVAL2"] = crval[1]
    hdr["CD1_1"] = -scale_arcsec / 3600
    hdr["CD1_2"] = 0.0
    hdr["CD2_1"] = 0.0
    hdr["CD2_2"] = scale_arcsec / 3600
    data = np.ones((ny, nx), dtype=np.float32)
    fits.PrimaryHDU(data=data, header=hdr).writeto(path)
    return str(path)


def test_zeropoint_to_calibration_mean_ab_zeropoint():
    """AB zeropoint 25.0 -> 363.078 nJy per ADU."""
    assert imaging.zeropoint_to_calibration_mean(25.0) == pytest.approx(
        363.078, rel=1e-4
    )


def test_zeropoint_to_calibration_mean_skymapper_main():
    """SkyMapper 'main' ZPAPPROX ~28.73 -> a much smaller nJy/ADU factor."""
    assert imaging.zeropoint_to_calibration_mean(28.7325) == pytest.approx(
        3631e9 * 10 ** (-0.4 * 28.7325), rel=1e-9
    )


def test_find_first_image_hdu_skips_empty_primary(tmp_path):
    path = tmp_path / "multi.fits"
    primary = fits.PrimaryHDU()
    image = fits.ImageHDU(data=np.zeros((4, 4), dtype=np.float32))
    fits.HDUList([primary, image]).writeto(path)

    with fits.open(path) as hdul:
        assert imaging.find_first_image_hdu(hdul) is hdul[1]


def test_find_first_image_hdu_raises_when_no_image(tmp_path):
    path = tmp_path / "empty.fits"
    fits.HDUList([fits.PrimaryHDU()]).writeto(path)

    with fits.open(path) as hdul:
        with pytest.raises(RuntimeError, match="No image HDU"):
            imaging.find_first_image_hdu(hdul)


def test_file_covers_target_true_at_center(tmp_path):
    path = _write_fits(tmp_path / "a.fits", 200, 200)
    assert imaging.file_covers_target(path, 102.2475, -36.0053) is True


def test_file_covers_target_false_when_far_away(tmp_path):
    path = _write_fits(tmp_path / "b.fits", 200, 200)
    assert imaging.file_covers_target(path, 150.0, 2.0) is False


def test_file_meets_requested_size_accepts_full_size(tmp_path):
    # 1087 px at 0.4976"/px = 0.1502 deg
    path = _write_fits(tmp_path / "c.fits", 1087, 1087)
    assert imaging.file_meets_requested_size(path, 0.15) is True


def test_file_meets_requested_size_rejects_trimmed(tmp_path):
    path = _write_fits(tmp_path / "d.fits", 300, 300)
    assert imaging.file_meets_requested_size(path, 0.15) is False


def test_clamp_cutout_size_passes_through_when_under_cap():
    assert imaging.clamp_cutout_size(0.15, 0.17, LOG) == 0.15


def test_clamp_cutout_size_clamps_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert imaging.clamp_cutout_size(0.4, 0.17, LOG) == 0.17
    assert "0.17" in caplog.text


def test_clamp_cutout_size_no_cap_is_identity():
    assert imaging.clamp_cutout_size(0.4, None, LOG) == 0.4
