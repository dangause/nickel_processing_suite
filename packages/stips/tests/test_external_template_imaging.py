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


def test_effective_cutout_size_is_the_silent_clamp():
    """The size a fetch will actually return, with no warning side effect."""
    assert imaging.effective_cutout_size(0.4, 0.17) == 0.17
    assert imaging.effective_cutout_size(0.15, 0.17) == 0.15
    assert imaging.effective_cutout_size(0.4, None) == 0.4


def test_validate_cutout_accepts_a_good_frame(tmp_path):
    path = _write_fits(tmp_path / "ok.fits", 1087, 1087)
    assert imaging.validate_cutout(path, 102.2475, -36.0053, 0.15) is None


def test_validate_cutout_reports_missing_target(tmp_path):
    path = _write_fits(tmp_path / "off.fits", 1087, 1087)
    reason = imaging.validate_cutout(path, 150.0, 2.0, 0.15)
    assert reason and "cover" in reason


def test_validate_cutout_reports_undersized_frame(tmp_path):
    """An edge-trimmed SIA frame clears the 10 kB floor but not this check."""
    path = _write_fits(tmp_path / "trim.fits", 300, 300)
    reason = imaging.validate_cutout(path, 102.2475, -36.0053, 0.15)
    assert reason and "smaller" in reason


# --- asinh (Lupton) pixel scaling -------------------------------------------
#
# PS1 stack images store pixels asinh-compressed, with the softening
# parameters in BSOFTEN/BOFFSET. Reading them as linear flux crushes a ~1e6:1
# dynamic range down to ~10:1, which subtracts faint stars fine (asinh is
# linear near sky, and the DIA kernel absorbs the constant scale) while
# leaving progressively larger POSITIVE residuals at bright stars.

#: Real BSOFTEN/BOFFSET from rings.v3.skycell.2381.052.stk.r.unconv.fits.
PS1_BSOFTEN = 181.7245144826
PS1_BOFFSET = 9.203996658325


def _ps1_header(bsoften=PS1_BSOFTEN, boffset=PS1_BOFFSET):
    hdr = fits.Header()
    if bsoften is not None:
        hdr["BSOFTEN"] = bsoften
    if boffset is not None:
        hdr["BOFFSET"] = boffset
    return hdr


def _encode(flux, bsoften=PS1_BSOFTEN, boffset=PS1_BOFFSET):
    """The forward asinh transform, for round-trip tests."""
    return 2.5 / np.log(10.0) * np.arcsinh((flux - boffset) / (2.0 * bsoften))


def test_decode_asinh_is_a_no_op_without_bsoften():
    """A fitscut cutout is already linear and carries no BSOFTEN."""
    data = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)
    out, decoded = imaging.decode_asinh_scaling(data, fits.Header())
    assert decoded is False
    np.testing.assert_array_equal(out, data)


def test_decode_asinh_round_trips_real_ps1_softening():
    """decode(encode(flux)) recovers the flux across the full dynamic range."""
    flux = np.array([-500.0, 0.0, 30.0, 1000.0, 5e4, 9e5], dtype=np.float64)
    out, decoded = imaging.decode_asinh_scaling(_encode(flux), _ps1_header())
    assert decoded is True
    np.testing.assert_allclose(out, flux, rtol=1e-6)


def test_decode_asinh_zero_maps_to_boffset():
    """sinh(0) == 0, so a stored zero decodes to exactly BOFFSET."""
    out, _ = imaging.decode_asinh_scaling(np.zeros((2, 2)), _ps1_header())
    np.testing.assert_allclose(out, PS1_BOFFSET, rtol=1e-12)


def test_decode_asinh_restores_the_crushed_dynamic_range():
    """The observed PS1 skycell spans [-2.855, 9.467] stored -> ~1e6 in flux.

    This is the bug's signature: read linearly, the brightest star sits only
    ~10x above sky instead of ~1e6x, so the template under-represents it by
    orders of magnitude.
    """
    stored = np.array([-2.855, 1.0, 9.467])
    out, _ = imaging.decode_asinh_scaling(stored, _ps1_header())
    assert out[2] > 1e6
    assert out[2] / out[1] > 1000
    # ...whereas the raw stored values differ by less than 10x.
    assert stored[2] / stored[1] < 10


def test_decode_asinh_defaults_boffset_to_zero():
    """BSOFTEN alone is enough; BOFFSET is an optional additive term."""
    out, decoded = imaging.decode_asinh_scaling(
        np.zeros((2, 2)), _ps1_header(boffset=None)
    )
    assert decoded is True
    np.testing.assert_allclose(out, 0.0, atol=1e-12)


def test_decode_asinh_preserves_non_finite_pixels():
    """NaNs must stay NaN so the BAD-mask logic downstream still sees them."""
    data = np.array([np.nan, np.inf, 1.0])
    out, _ = imaging.decode_asinh_scaling(data, _ps1_header())
    assert np.isnan(out[0]) and np.isinf(out[1]) and np.isfinite(out[2])


@pytest.mark.parametrize("bad", [0.0, -5.0, "NaN", "not-a-number"])
def test_decode_asinh_refuses_an_unusable_softening(bad):
    """A zero/negative/NaN/garbage BSOFTEN is corrupt metadata, not a scaling.

    PS1 headers spell absent numeric values as the *string* ``'NaN'`` (see
    e.g. ``FPA.FOCUS``), and a FITS header cannot hold a float NaN at all --
    so the string forms are the ones that actually reach this code.
    """
    data = np.array([[1.0, 2.0]])
    out, decoded = imaging.decode_asinh_scaling(data, _ps1_header(bsoften=bad))
    assert decoded is False
    np.testing.assert_array_equal(out, data)


def test_decode_asinh_is_monotonic():
    stored = np.linspace(-3.0, 9.5, 50)
    out, _ = imaging.decode_asinh_scaling(stored, _ps1_header())
    assert np.all(np.diff(out) > 0)


# --- saturation masking ------------------------------------------------------
#
# SkyMapper serves single-epoch 100 s frames, not deep stacks, and bright stars
# reach the detector ceiling. The frames declare it: SATURATE = 65435, with real
# star cores pinned at 64538-64539 (flat-topped over 5+ pixels) and negative
# bleed artifacts at -97/-98 immediately adjacent. Left unmasked those clipped
# cores under-represent the star, and the negative pixels push the difference
# the wrong way, producing spurious positive DIA detections.

#: Real values read off a SkyMapper i-band SIA frame.
SM_SATURATE = 65435.0
SM_OBSERVED_CLIP = 64539.0


def test_saturation_mask_is_empty_without_a_keyword():
    data = np.full((4, 4), 1e9)
    mask, level = imaging.saturation_mask(data, fits.Header(), ["SATURATE"])
    assert level is None
    assert not mask.any()


def test_saturation_mask_is_empty_when_no_keywords_declared():
    """A source that does not saturate (e.g. a deep stack) declares none."""
    hdr = fits.Header({"SATURATE": SM_SATURATE})
    mask, level = imaging.saturation_mask(np.full((4, 4), 1e9), hdr, [])
    assert level is None
    assert not mask.any()


def test_saturation_mask_flags_the_observed_skymapper_clip():
    """The real clip sits at 98.6% of the declared SATURATE, so the default
    fraction has to be below that or it catches nothing."""
    hdr = fits.Header({"SATURATE": SM_SATURATE})
    data = np.array([[1108.0, SM_OBSERVED_CLIP], [3000.0, 500.0]])
    mask, level = imaging.saturation_mask(data, hdr, ["SATURATE"], grow=0)
    assert level == pytest.approx(0.9 * SM_SATURATE)
    assert mask[0, 1]
    assert not mask[0, 0] and not mask[1, 0] and not mask[1, 1]


def test_saturation_mask_grows_to_cover_bleed_artifacts():
    """The -97 pixel next to a saturated core must end up masked too."""
    hdr = fits.Header({"SATURATE": SM_SATURATE})
    data = np.full((7, 7), 1108.0)
    data[3, 3] = SM_OBSERVED_CLIP
    data[3, 4] = -97.0  # the adjacent undershoot
    mask, _ = imaging.saturation_mask(data, hdr, ["SATURATE"], grow=2)
    assert mask[3, 3] and mask[3, 4]
    assert not mask[0, 0]


def test_saturation_mask_grow_zero_flags_only_the_clipped_pixels():
    hdr = fits.Header({"SATURATE": SM_SATURATE})
    data = np.full((5, 5), 1108.0)
    data[2, 2] = SM_OBSERVED_CLIP
    mask, _ = imaging.saturation_mask(data, hdr, ["SATURATE"], grow=0)
    assert mask.sum() == 1


def test_saturation_mask_ignores_non_finite_pixels():
    hdr = fits.Header({"SATURATE": SM_SATURATE})
    data = np.array([[np.nan, np.inf], [1108.0, SM_OBSERVED_CLIP]])
    mask, _ = imaging.saturation_mask(data, hdr, ["SATURATE"], grow=0)
    assert mask[1, 1]
    assert not mask[0, 0]


@pytest.mark.parametrize("bad", [0.0, -1.0, "NaN", "not-a-number"])
def test_saturation_mask_refuses_an_unusable_level(bad):
    hdr = fits.Header({"SATURATE": bad})
    mask, level = imaging.saturation_mask(np.full((4, 4), 1e9), hdr, ["SATURATE"])
    assert level is None
    assert not mask.any()


def test_saturation_mask_uses_the_first_keyword_present():
    hdr = fits.Header({"SATLEVEL": 1000.0})
    data = np.array([[500.0, 950.0]])
    mask, level = imaging.saturation_mask(data, hdr, ["SATURATE", "SATLEVEL"], grow=0)
    assert level == pytest.approx(900.0)
    assert not mask[0, 0] and mask[0, 1]
