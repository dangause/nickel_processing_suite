"""Regression tests for ``reproject_to_patch``'s PSF and metadata handling.

Pins two bugs found during end-to-end validation of the external-template
framework on a real SkyMapper ingest:

1. ``GaussianPsf`` stores its width in PIXELS. ``reproject_to_patch`` used to
   carry the source exposure's PSF onto the reprojected exposure unchanged,
   even though the target (patch) grid has a different pixel scale than the
   source survey frame. That silently misrepresents the seeing by the ratio
   of the two pixel scales -- measured as a 1.72x understatement for
   SkyMapper (0.4976"/px -> 0.2887"/px).
2. The reprojected exposure did not carry over the source exposure's
   metadata, so the TEMPLATE_SOURCE / TEMPLATE_ZEROPOINT /
   TEMPLATE_FWHM_ARCSEC / TEMPLATE_ORIGIN_FILE provenance keys set by
   ``fits_to_lsst_exposure`` were lost.

Runs only inside the LSST stack.
"""

import pytest
from astropy.io import fits
from astropy.wcs import WCS as AstropyWCS

pytest.importorskip("lsst.afw.image")

import lsst.afw.detection as afwDetection  # noqa: E402
import lsst.afw.image as afwImage  # noqa: E402
import lsst.geom as geom  # noqa: E402
from stips.pipeline_tools.external_template.core import (  # noqa: E402
    convert_astropy_wcs_to_lsst,
    reproject_to_patch,
)

# Numbers from the real SkyMapper ingest that surfaced this bug.
SOURCE_SCALE_ARCSEC = 0.4976  # SkyMapper native scale
TARGET_SCALE_ARCSEC = 0.2887  # skymap patch scale
SOURCE_FWHM_ARCSEC = 1.68189  # measured QAFWHM


class _FakePatchInfo:
    """Duck-typed stand-in for ``lsst.skymap.PatchInfo``.

    ``reproject_to_patch`` only ever calls ``.getWcs()`` / ``.getOuterBBox()``
    on its ``patch_info`` argument, so a real skymap is unnecessary to
    exercise it.
    """

    def __init__(self, wcs, bbox):
        self._wcs = wcs
        self._bbox = bbox

    def getWcs(self):
        return self._wcs

    def getOuterBBox(self):
        return self._bbox


def _make_wcs(nx, ny, scale_arcsec, crval=(210.910750, 54.311694)):
    """Build an LSST SkyWcs with a given pixel scale, via the same
    astropy-header->LSST conversion path core.py uses for real cutouts."""
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
    astropy_wcs = AstropyWCS(hdr)
    return convert_astropy_wcs_to_lsst(astropy_wcs)


def _make_source_exposure():
    nx, ny = 80, 80
    wcs = _make_wcs(nx, ny, SOURCE_SCALE_ARCSEC)
    masked_image = afwImage.MaskedImageF(nx, ny)
    masked_image.image.array[:, :] = 100.0
    masked_image.variance.array[:, :] = 1.0
    exposure = afwImage.ExposureF(masked_image)
    exposure.setWcs(wcs)
    exposure.setPhotoCalib(afwImage.PhotoCalib(1.0))
    exposure.setFilter(afwImage.FilterLabel(band="r"))

    sigma_source_px = (SOURCE_FWHM_ARCSEC / SOURCE_SCALE_ARCSEC) / 2.3548
    exposure.setPsf(afwDetection.GaussianPsf(21, 21, sigma_source_px))

    metadata = exposure.getMetadata()
    exposure.getInfo().setMetadata(metadata)
    metadata.set("TEMPLATE_SOURCE", "skymapper")
    metadata.set("TEMPLATE_ZEROPOINT", 28.7325)
    metadata.set("TEMPLATE_FWHM_ARCSEC", SOURCE_FWHM_ARCSEC)
    metadata.set("TEMPLATE_ORIGIN_FILE", "/fake/path.fits")

    return exposure, sigma_source_px


def _make_target_patch_info():
    nx, ny = 60, 60
    wcs = _make_wcs(nx, ny, TARGET_SCALE_ARCSEC)
    bbox = geom.Box2I(geom.Point2I(0, 0), geom.Extent2I(nx, ny))
    return _FakePatchInfo(wcs, bbox)


def test_reprojected_psf_preserves_angular_fwhm():
    """The reprojected PSF's FWHM in arcsec must match the source's FWHM --
    not merely carry the source's sigma in PIXELS unchanged onto a grid with
    a different pixel scale.

    Pre-fix: reprojected sigma == source sigma_px (1.44 px), which at the
    target's finer pixel scale (0.2887"/px) reads as FWHM ~0.98" -- a 1.72x
    understatement of the true 1.68" seeing. Post-fix: the reprojected PSF's
    sigma is rescaled so the angular FWHM survives the regrid.
    """
    exposure, sigma_source_px = _make_source_exposure()
    patch_info = _make_target_patch_info()

    reprojected = reproject_to_patch(exposure, patch_info)

    target_bbox = reprojected.getBBox()
    target_center = geom.Point2D(target_bbox.getCenterX(), target_bbox.getCenterY())
    target_scale = reprojected.getWcs().getPixelScale(target_center).asArcseconds()

    reprojected_sigma_px = (
        reprojected.getPsf().computeShape(target_center).getDeterminantRadius()
    )
    reprojected_fwhm_arcsec = reprojected_sigma_px * target_scale * 2.3548

    assert reprojected_fwhm_arcsec == pytest.approx(SOURCE_FWHM_ARCSEC, rel=0.02)

    # Explicitly rule out the pre-fix bug: sigma carried unchanged in pixels.
    assert reprojected_sigma_px != pytest.approx(sigma_source_px, rel=0.05)


def test_reprojected_exposure_carries_source_metadata():
    """TEMPLATE_* provenance keys set by fits_to_lsst_exposure must survive
    reprojection -- previously they were dropped entirely."""
    exposure, _ = _make_source_exposure()
    patch_info = _make_target_patch_info()

    reprojected = reproject_to_patch(exposure, patch_info)

    md = reprojected.getMetadata()
    assert md.get("TEMPLATE_SOURCE") == "skymapper"
    assert md.get("TEMPLATE_ZEROPOINT") == pytest.approx(28.7325)
    assert md.get("TEMPLATE_FWHM_ARCSEC") == pytest.approx(SOURCE_FWHM_ARCSEC)
    assert md.get("TEMPLATE_ORIGIN_FILE") == "/fake/path.fits"
