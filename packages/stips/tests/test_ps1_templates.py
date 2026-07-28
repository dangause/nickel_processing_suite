#!/usr/bin/env python3
"""
Test suite for PS1 template ingestion

Tests download, conversion, and ingestion of PS1 templates for DIA.

Usage:
    pytest tests/test_ps1_templates.py
    # Or run individual tests:
    pytest tests/test_ps1_templates.py::test_download_ps1_cutout
    # Enable PS1 download flow tests:
    RUN_PS1_DOWNLOADS=1 pytest tests/test_ps1_templates.py
"""

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

# stips.pipeline_tools.ingest_ps1_template imports the LSST stack (and calls
# sys.exit if it is missing), so guard the whole module: skip cleanly in a
# plain venv instead of erroring during fixture setup.
pytest.importorskip("lsst.afw.image")

# Test coordinates (M67 open cluster - well-covered by PS1)
TEST_RA = 132.825
TEST_DEC = 11.8
TEST_SIZE = 0.1  # degrees (smaller for faster tests)
RUN_PS1_DOWNLOADS = os.getenv("RUN_PS1_DOWNLOADS") == "1"


@pytest.fixture
def temp_dir():
    """Create temporary directory for test outputs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def ps1_ingestion_module():
    """Import PS1 ingestion module."""
    try:
        import sys

        sys.path.insert(
            0,
            str(Path(__file__).resolve().parents[1] / "src"),
        )
        from stips.pipeline_tools import ingest_ps1_template

        return ingest_ps1_template
    except ImportError as e:
        pytest.skip(f"Could not import PS1 ingestion module: {e}")


class TestPS1Download:
    """Test PS1 download functionality."""

    @pytest.mark.integration
    def test_download_ps1_fitscut(self, ps1_ingestion_module, temp_dir):
        """Test PS1 download via fitscut service."""
        if not RUN_PS1_DOWNLOADS:
            pytest.skip("Set RUN_PS1_DOWNLOADS=1 to enable PS1 download tests.")
        output_file = Path(temp_dir) / "test_ps1_r.fits"

        result = ps1_ingestion_module.download_ps1_via_fitscut(
            TEST_RA, TEST_DEC, "r", TEST_SIZE, output_file
        )

        if result:  # Service might be down
            assert output_file.exists()
            assert output_file.stat().st_size > 10000  # Reasonable FITS size

            # Verify it's a valid FITS file
            with fits.open(output_file) as hdul:
                assert len(hdul) > 0
                assert hdul[0].data is not None or (
                    len(hdul) > 1 and hdul[1].data is not None
                )

    @pytest.mark.integration
    def test_download_ps1_ps1filenames(self, ps1_ingestion_module, temp_dir):
        """Test PS1 download via ps1filenames service."""
        if not RUN_PS1_DOWNLOADS:
            pytest.skip("Set RUN_PS1_DOWNLOADS=1 to enable PS1 download tests.")
        output_file = Path(temp_dir) / "test_ps1_i.fits"

        result = ps1_ingestion_module.download_ps1_via_ps1filenames(
            TEST_RA, TEST_DEC, "i", TEST_SIZE, output_file
        )

        if result:  # Service might be down
            assert output_file.exists()
            assert output_file.stat().st_size > 10000

    @pytest.mark.slow
    @pytest.mark.integration
    def test_download_full_workflow(self, ps1_ingestion_module, temp_dir):
        """Test complete download workflow with all fallbacks."""
        if not RUN_PS1_DOWNLOADS:
            pytest.skip("Set RUN_PS1_DOWNLOADS=1 to enable PS1 download tests.")
        result = ps1_ingestion_module.download_ps1_cutout(
            TEST_RA, TEST_DEC, "r", TEST_SIZE, temp_dir
        )

        # At least one method should work
        if result:
            assert Path(result).exists()
            print(f"Successfully downloaded: {result}")


class TestPS1CutoutValidation:
    """Test cutout size/coverage validation helpers."""

    def _write_mock_fits(self, path, nx, ny):
        from astropy.wcs import WCS as AstropyWCS

        data = np.random.poisson(100, size=(ny, nx)).astype(np.float32)
        wcs = AstropyWCS(naxis=2)
        wcs.wcs.crpix = [nx / 2.0, ny / 2.0]
        wcs.wcs.crval = [TEST_RA, TEST_DEC]
        wcs.wcs.cdelt = [-0.25 / 3600.0, 0.25 / 3600.0]  # PS1 plate scale
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        hdu = fits.PrimaryHDU(data=data, header=wcs.to_header())
        hdu.header["FILTER"] = "r"
        hdu.writeto(path, overwrite=True)

    def test_cutout_size_check_passes_for_requested_size(
        self, ps1_ingestion_module, temp_dir
    ):
        fits_path = Path(temp_dir) / "full_size.fits"
        # ~0.3 deg at 0.25"/pix
        self._write_mock_fits(fits_path, nx=4320, ny=4320)
        assert ps1_ingestion_module.ps1_file_meets_requested_size(
            fits_path, requested_size_deg=0.3
        )

    def test_cutout_size_check_rejects_trimmed_cutout(
        self, ps1_ingestion_module, temp_dir
    ):
        fits_path = Path(temp_dir) / "trimmed.fits"
        # Significantly smaller than requested 0.3 deg (mirrors observed trimmed case).
        self._write_mock_fits(fits_path, nx=3028, ny=3538)
        assert not ps1_ingestion_module.ps1_file_meets_requested_size(
            fits_path, requested_size_deg=0.3
        )


class TestPS1Conversion:
    """Test PS1 FITS to LSST Exposure conversion."""

    @pytest.fixture
    def sample_ps1_fits(self, temp_dir):
        """Create a mock PS1 FITS file for testing."""
        # Create minimal PS1-like FITS
        data = np.random.poisson(100, size=(100, 100)).astype(np.float32)

        # Create WCS
        from astropy.wcs import WCS as AstropyWCS

        wcs = AstropyWCS(naxis=2)
        wcs.wcs.crpix = [50.0, 50.0]
        wcs.wcs.crval = [TEST_RA, TEST_DEC]
        wcs.wcs.cdelt = [-0.25 / 3600, 0.25 / 3600]  # PS1 pixel scale
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]

        # Create FITS file
        hdu = fits.PrimaryHDU(data=data, header=wcs.to_header())
        hdu.header["FILTER"] = "r"
        hdu.header["ZPT"] = 25.5  # Typical PS1 zeropoint

        fits_path = Path(temp_dir) / "mock_ps1.fits"
        hdu.writeto(fits_path, overwrite=True)

        return str(fits_path)

    def test_convert_ps1_to_lsst(self, ps1_ingestion_module, sample_ps1_fits):
        """Test conversion of PS1 FITS to LSST Exposure."""
        exposure = ps1_ingestion_module.convert_ps1_to_lsst_exposure(
            sample_ps1_fits, nickel_band="r"
        )

        # Check exposure was created
        assert exposure is not None

        # Check WCS
        wcs = exposure.getWcs()
        assert wcs is not None

        # Check PhotoCalib
        photo_calib = exposure.getPhotoCalib()
        assert photo_calib is not None

        # Check filter
        filter_label = exposure.getFilter()
        assert filter_label.bandLabel == "r"

        # Check metadata. The provenance keys are now source-namespaced
        # (TEMPLATE_*) rather than PS1-specific, since the same converter now
        # serves every external-template survey.
        metadata = exposure.getMetadata()
        assert "TEMPLATE_SOURCE" in metadata.names()
        assert metadata.getScalar("TEMPLATE_SOURCE") == "ps1"
        assert "TEMPLATE_ZEROPOINT" in metadata.names()

    def test_zeropoint_from_header(self, ps1_ingestion_module, sample_ps1_fits):
        """Test that zeropoint is correctly read from FITS header."""
        exposure = ps1_ingestion_module.convert_ps1_to_lsst_exposure(
            sample_ps1_fits, nickel_band="r"
        )

        metadata = exposure.getMetadata()
        zp = metadata.getScalar("TEMPLATE_ZEROPOINT")
        assert zp == pytest.approx(25.5, abs=0.01)


class TestBandMapping:
    """Local-band -> PS1-band resolution is profile-driven (F-011).

    Exercised through the PRODUCTION path -- ``ingest._resolve_source_band()``
    -> ``PS1Source.band_map()`` -> ``core.pipeline.template_band_map()`` -- and
    not a PS1-only duplicate, which is how the duplicate came to read
    ``ps1_band_map`` directly and ignore ``template_band_maps`` entirely.
    """

    @staticmethod
    def _resolve(local_band):
        from stips.pipeline_tools.external_template.ingest import _resolve_source_band
        from stips.pipeline_tools.external_template.sources import get_source

        return _resolve_source_band(get_source("ps1"), local_band)

    @staticmethod
    def _use_profile(monkeypatch, **attrs):
        import stips.core.config as cfg

        prof = type(
            "_Prof", (), {"ps1_band_map": {}, "template_band_maps": {}, **attrs}
        )
        monkeypatch.setattr(cfg, "load_active_profile", lambda *a, **k: prof())

    def test_resolve_uses_profile_map(self, ps1_ingestion_module, monkeypatch):
        """The legacy ps1_band_map still drives PS1 when no explicit entry."""
        self._use_profile(monkeypatch, ps1_band_map={"r": "r", "i": "i"})

        assert self._resolve("r") == "r"
        assert self._resolve("i") == "i"
        # A band not in the map is not PS1-eligible.
        assert self._resolve("v") is None

    def test_template_band_maps_takes_precedence_for_ps1(
        self, ps1_ingestion_module, monkeypatch
    ):
        """An explicit template_band_maps["ps1"] entry wins over the legacy map.

        ps1_band_map stays dual-purpose (it also builds the PS1 refcat
        filterMap via STIPS_PS1_BAND_MAP), so a profile must be able to state a
        different TEMPLATE policy without disturbing refcats.
        """
        self._use_profile(
            monkeypatch,
            ps1_band_map={"r": "r"},
            template_band_maps={"ps1": {"i": "z"}},
        )

        assert self._resolve("i") == "z"
        assert self._resolve("r") is None

    def test_resolve_new_capability_g(self, ps1_ingestion_module, monkeypatch):
        """A Sloan-style profile makes g PS1-eligible without framework edits."""
        self._use_profile(monkeypatch, ps1_band_map={"g": "g"})
        assert self._resolve("g") == "g"

    def test_resolve_identity_when_no_profile(self, ps1_ingestion_module, monkeypatch):
        """Without a loadable profile, fall back to identity (parity)."""
        import stips.core.config as cfg

        def _boom(*a, **k):
            raise RuntimeError("INSTRUMENT_DIR not set")

        monkeypatch.setattr(cfg, "load_active_profile", _boom)
        assert self._resolve("r") == "r"


class TestMetadataTracking:
    """Test PS1 template metadata tracking."""

    def test_record_ps1_metadata(self, temp_dir):
        """Test recording PS1 template metadata."""
        from stips.pipeline_tools.template_metadata import (
            TemplateMetadata,
        )

        metadata_mgr = TemplateMetadata(temp_dir)

        metadata_mgr.record_template(
            collection="templates/ps1/r",
            start_date="PS1",
            end_date="PS1",
            tract="1825",
            band="r",
            description="Test PS1 template",
            source="ps1",
            ps1_filter="r",
            ps1_ra=TEST_RA,
            ps1_dec=TEST_DEC,
            ps1_cutout_size=TEST_SIZE,
        )

        # Verify metadata was saved
        assert metadata_mgr.metadata["templates"]["templates/ps1/r"]["source"] == "ps1"
        assert (
            metadata_mgr.metadata["templates"]["templates/ps1/r"]["ps1"]["filter"]
            == "r"
        )
        assert (
            metadata_mgr.metadata["templates"]["templates/ps1/r"]["ps1"]["ra"]
            == TEST_RA
        )

    def test_query_ps1_templates(self, temp_dir):
        """Test querying for PS1 templates."""
        from stips.pipeline_tools.template_metadata import (
            TemplateMetadata,
        )

        metadata_mgr = TemplateMetadata(temp_dir)

        # Add mix of templates
        metadata_mgr.record_template(
            collection="templates/ps1/r",
            start_date="PS1",
            end_date="PS1",
            band="r",
            source="ps1",
        )

        metadata_mgr.record_template(
            collection="templates/deep/r",
            start_date="20210101",
            end_date="20210131",
            band="r",
            source="nickel",
        )

        # Query should return both
        results = metadata_mgr.query_templates(required_band="r")
        assert len(results) == 2
        assert "templates/ps1/r" in results
        assert "templates/deep/r" in results


@pytest.mark.integration
class TestFullIngestionWorkflow:
    """Integration tests for full PS1 ingestion workflow.

    These require a Butler repository and are skipped by default.
    Run with: pytest tests/test_ps1_templates.py -m integration
    """

    @pytest.fixture
    def butler_repo(self):
        """Get Butler repository path from environment."""
        repo = os.environ.get("REPO")
        if not repo or not Path(repo).exists():
            pytest.skip("REPO environment variable not set or repo doesn't exist")
        return repo

    def test_full_ingestion(self, ps1_ingestion_module, butler_repo, temp_dir):
        """Test complete ingestion workflow (download + convert + ingest)."""
        pytest.skip("Skipping full ingestion test - requires Butler repo setup")

        # This would test the full main() workflow
        # Left as template for manual testing


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
