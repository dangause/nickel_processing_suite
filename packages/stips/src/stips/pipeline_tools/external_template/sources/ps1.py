"""Pan-STARRS1 external-template adapter.

Holds the download/metadata helpers that used to live in
``stips.pipeline_tools.ingest_ps1_template`` (``download_ps1_cutout`` and its
Method 1/2/3 fallbacks, the FITS-cutout helpers, and the PS1 zeropoint table
and band resolver). The LSST-dependent conversion/Butler-ingest code stays in
``ingest_ps1_template.py`` — this module must stay importable in a plain venv.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import astropy.units as u
import numpy as np
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
from astropy.wcs import WCS

# Try importing astroquery with a helpful error message, mirroring the
# historical behavior of the legacy ingest script.
try:
    from astroquery.mast import Observations
except ImportError:  # pragma: no cover - exercised only when astroquery is absent
    Observations = None
    # `log` (module logger) is defined further below, after this try/except,
    # so get a logger by name directly here rather than reordering the
    # module just to move that assignment earlier.
    logging.getLogger(__name__).warning(
        "astroquery is not installed; the MAST download method for PS1 "
        "templates will be skipped (the fitscut fallback still works). "
        "Install astroquery to enable it."
    )

from .. import imaging
from .base import TemplateSourceError

log = logging.getLogger(__name__)


# PS1 zeropoints (AB mag for 1 DN/sec)
# From PS1 DR2: https://outerspace.stsci.edu/display/PANSTARRS/PS1+Stack+images
# These are typical values; actual zeropoints are in FITS headers (FPA.ZP or ZPT keywords)
PS1_ZEROPOINTS = {
    "g": 25.0,
    "r": 25.0,
    "i": 25.0,
    "z": 24.5,
    "y": 23.5,
}


def open_ps1_fits(source):
    source = str(source)
    if source.startswith("s3://"):
        return fits.open(source, fsspec_kwargs={"anon": True})
    return fits.open(source)


def collect_ps1_metadata(hdul, band=None):
    keys = [
        "FILTER",
        "FILTNAM",
        "FPA.ZP",
        "ZPT",
        "MAGZERO",
        "MAGZPT",
        "EXPTIME",
        "TEXPTIME",
    ]
    metadata = {}
    for hdu in hdul:
        hdr = hdu.header
        for key in keys:
            if key not in metadata and key in hdr:
                metadata[key] = hdr[key]
    if band and "FILTER" not in metadata and "FILTNAM" not in metadata:
        metadata["FILTER"] = band
    return metadata


def write_ps1_cutout(source, coord, size_deg, output_file, band=None):
    size = (size_deg * u.deg, size_deg * u.deg)
    with open_ps1_fits(source) as hdul:
        image_hdu = imaging.find_first_image_hdu(hdul)
        metadata = collect_ps1_metadata(hdul, band=band)
        wcs = WCS(image_hdu.header)
        cutout = Cutout2D(image_hdu.data, coord, size, wcs=wcs, mode="trim")
        header = image_hdu.header.copy()
        for key in ("NAXIS1", "NAXIS2"):
            if key in header:
                del header[key]
        header.update(cutout.wcs.to_header())
        for key, value in metadata.items():
            if key not in header:
                header[key] = value
        fits.PrimaryHDU(data=cutout.data, header=header).writeto(
            output_file, overwrite=True
        )
    return str(output_file)


def download_ps1_cutout(
    ra, dec, band, size_deg=0.2, output_dir=".", force_service=None
):
    """
    Download PS1 image cutout from STScI MAST archive or PS1 image service.

    Parameters
    ----------
    ra : float
        Right ascension in degrees
    dec : float
        Declination in degrees
    band : str
        PS1 band (g, r, i, z, y)
    size_deg : float
        Cutout width in degrees
    output_dir : str
        Directory to save downloaded FITS file
    force_service : str, optional
        Force specific download method: 'mast', 'fitscut', or 'ps1filenames'

    Returns
    -------
    str or None
        Path to downloaded FITS file, or None if download failed
    """
    log.info(f"Downloading PS1 {band}-band cutout for RA={ra:.4f}, Dec={dec:.4f}")
    log.info(f"  Cutout width: {size_deg:.3f} degrees ({size_deg*60:.1f} arcmin)")

    coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    output_file = output_path / f"ps1_{band}_ra{ra:.4f}_dec{dec:.4f}.fits"

    # Check if file already exists and is large enough
    if output_file.exists() and output_file.stat().st_size > 10000:
        redownload = False
        if not imaging.file_covers_target(output_file, ra, dec):
            log.warning(
                f"Existing PS1 file {output_file} does not cover target; re-downloading"
            )
            redownload = True
        elif not imaging.file_meets_requested_size(output_file, size_deg):
            log.info("Cached PS1 file is smaller than requested; re-downloading")
            redownload = True

        if redownload:
            try:
                output_file.unlink()
            except Exception as e:
                log.warning(f"  Could not remove stale PS1 file: {e}")
        else:
            log.info(f"Using existing file: {output_file}")
            return str(output_file)

    # Method 1: Try PS1 via MAST (cloud-first stack + cutout)
    if (force_service is None or force_service == "mast") and Observations is not None:
        try:
            log.info("Method 1: Trying MAST archive (cloud-first stack + cutout)...")
            try:
                Observations.enable_cloud_dataset(provider="AWS")
            except Exception as e:
                log.warning(f"  Could not enable cloud dataset: {e}")
            # Query PS1 stacked images with more specific criteria
            obs_table = Observations.query_criteria(
                coordinates=coord,
                radius=(size_deg / 2.0) * u.deg,
                obs_collection="PS1",
                filters=band,
                dataproduct_type="image",
            )

            if len(obs_table) > 0:
                log.info(f"  Found {len(obs_table)} PS1 observations")

                obs_coords = SkyCoord(obs_table["s_ra"], obs_table["s_dec"], unit="deg")
                seps = coord.separation(obs_coords)
                best_idx = int(np.argmin(seps))
                inside_idxs = None
                if "s_fov" in obs_table.colnames:
                    fov = np.asarray(obs_table["s_fov"], dtype=float)
                    inside = np.isfinite(fov) & (seps <= (fov * u.deg / 2))
                    inside_idxs = np.where(inside)[0]
                    if inside_idxs.size > 0:
                        best_idx = int(inside_idxs[np.argmin(seps[inside_idxs])])

                best_obs = obs_table[best_idx]
                log.info(
                    "  Closest obs index %d at separation %.3f arcmin",
                    best_idx,
                    seps[best_idx].to(u.arcmin).value,
                )
                if inside_idxs is not None:
                    log.info(
                        "  Observations containing target (by s_fov): %d",
                        inside_idxs.size,
                    )

                # Get data products for the closest observation
                products = Observations.get_product_list(best_obs)

                # Filter for stacked images (not warp or diff)
                desc_col = products["description"]
                desc_values = (
                    desc_col.filled("") if hasattr(desc_col, "filled") else desc_col
                )
                desc_text = np.asarray(desc_values, dtype=str)

                is_science = products["productType"] == "SCIENCE"
                has_stack = np.char.find(np.char.lower(desc_text), "stack") >= 0

                stack_products = products[is_science & has_stack]

                if len(stack_products) > 0:
                    log.info(f"  Found {len(stack_products)} stack products")

                    stack_desc = desc_text[is_science & has_stack]
                    prefer_unconv = (
                        np.char.find(np.char.lower(stack_desc), "unconv") >= 0
                    )
                    if np.any(prefer_unconv):
                        stack_products = stack_products[prefer_unconv]

                    cloud_uri = None
                    try:
                        cloud_uris = Observations.get_cloud_uris(stack_products[0:1])
                        if isinstance(cloud_uris, (list, tuple)):
                            cloud_uri = cloud_uris[0] if cloud_uris else None
                        elif isinstance(cloud_uris, str):
                            cloud_uri = cloud_uris
                        elif hasattr(cloud_uris, "colnames"):
                            for col in ("cloud_uri", "s3_uri", "uri"):
                                if (
                                    col in cloud_uris.colnames
                                    and len(cloud_uris[col]) > 0
                                ):
                                    cloud_uri = cloud_uris[col][0]
                                    break
                        if cloud_uri:
                            log.info(f"  Cloud URI: {cloud_uri}")
                    except Exception as e:
                        log.warning(f"  Failed to resolve cloud URI: {e}")

                    # Download to temporary location (cloud-first)
                    temp_dir = output_path / "temp_mast"
                    temp_dir.mkdir(exist_ok=True)
                    source = cloud_uri

                    manifest = None
                    try:
                        manifest = Observations.download_products(
                            stack_products[0:1],
                            download_dir=str(temp_dir),
                            cloud_only=True,
                        )
                    except Exception as e:
                        log.warning(f"  Cloud download failed: {e}")

                    if (
                        manifest is not None
                        and len(manifest) > 0
                        and "Local Path" in manifest.colnames
                    ):
                        local_path = Path(manifest["Local Path"][0])
                        if local_path.exists():
                            source = local_path
                            log.info(f"  Downloaded to {local_path}")

                    if source is None or source == cloud_uri:
                        try:
                            fallback_manifest = Observations.download_products(
                                stack_products[0:1], download_dir=str(temp_dir)
                            )
                            if (
                                len(fallback_manifest) > 0
                                and "Local Path" in fallback_manifest.colnames
                            ):
                                local_path = Path(fallback_manifest["Local Path"][0])
                                if local_path.exists():
                                    source = local_path
                                    log.info(f"  Downloaded via MAST to {local_path}")
                        except Exception as e:
                            log.warning(f"  MAST download failed: {e}")

                    if source:
                        try:
                            write_ps1_cutout(
                                source, coord, size_deg, output_file, band=band
                            )
                        except Exception as e:
                            log.warning(f"  Cutout failed: {e}")
                            source = None

                    if source and imaging.file_covers_target(output_file, ra, dec):
                        if imaging.file_meets_requested_size(output_file, size_deg):
                            return str(output_file)
                        log.warning(
                            "  MAST cutout is undersized for DIA margin; trying alternate service"
                        )

                    log.warning(
                        "  MAST stack cutout does not cover target; trying alternate service"
                    )
                    if output_file.exists():
                        try:
                            output_file.unlink()
                        except Exception:
                            pass
                else:
                    log.warning("  No stack products found in MAST results")
            else:
                log.warning(f"  No PS1 {band}-band observations found at this position")

        except Exception as e:
            log.warning(f"  MAST download failed: {e}")

    # Method 2: Try PS1 image service (fitscut) - more reliable for cutouts
    if force_service is None or force_service == "fitscut":
        result = download_ps1_via_fitscut(ra, dec, band, size_deg, output_file)
        if (
            result
            and imaging.file_covers_target(result, ra, dec)
            and imaging.file_meets_requested_size(result, size_deg)
        ):
            return result
        if result:
            try:
                Path(result).unlink()
            except Exception:
                pass

    # Method 3: Try ps1filenames service for full stack URLs
    if force_service is None or force_service == "ps1filenames":
        result = download_ps1_via_ps1filenames(ra, dec, band, size_deg, output_file)
        if (
            result
            and imaging.file_covers_target(result, ra, dec)
            and imaging.file_meets_requested_size(result, size_deg)
        ):
            return result
        if result:
            try:
                Path(result).unlink()
            except Exception:
                pass

    log.error("All PS1 download methods failed")
    log.info("You can manually download from:")
    size_pixels = int(size_deg * 3600 / 0.25)
    log.info(
        f"  https://ps1images.stsci.edu/cgi-bin/fitscut.cgi?ra={ra}&dec={dec}&size={size_pixels}&format=fits&filter={band}"
    )
    log.info(f"Save as: {output_file}")
    return None


def download_ps1_via_fitscut(ra, dec, band, size_deg, output_file):
    """
    Download PS1 cutout via fitscut.cgi service (on-the-fly cutouts).

    This is the most reliable method for custom-sized cutouts.
    """
    log.info("Method 2: Trying PS1 fitscut.cgi service...")

    # Convert size to pixels (PS1 is 0.25"/pixel)
    requested_pixels = int(size_deg * 3600 / 0.25)
    max_pixels = 10000  # fitscut returns 400 for very large cutouts; cap to keep requests successful

    sizes_to_try = []
    capped = min(requested_pixels, max_pixels)
    if capped < requested_pixels:
        log.warning(
            f"  Requested cutout {requested_pixels}px exceeds fitscut limit {max_pixels}px; using {capped}px instead"
        )
    sizes_to_try.append(capped)
    # Add a couple of fallback sizes in case the first request is still too big for the service
    for fallback in (8000, 6000):
        if fallback < sizes_to_try[-1]:
            sizes_to_try.append(fallback)

    url = "https://ps1images.stsci.edu/cgi-bin/fitscut.cgi"

    for idx, size_pixels in enumerate(sizes_to_try):
        params = {
            "ra": ra,
            "dec": dec,
            "size": size_pixels,
            "format": "fits",
            "filter": band,
        }

        try:
            log.info(f"  Requesting cutout: {size_pixels}x{size_pixels} pixels")
            response = requests.get(url, params=params, timeout=180)

            if response.status_code == 200 and len(response.content) > 10000:
                with open(output_file, "wb") as f:
                    f.write(response.content)
                log.info(
                    f"  Successfully downloaded via fitscut: {output_file} ({len(response.content)} bytes)"
                )
                return str(output_file)

            log.warning(
                f"  fitscut failed: status {response.status_code}, size {len(response.content)} bytes"
            )
        except Exception as e:
            log.warning(f"  fitscut method failed: {e}")

        if idx < len(sizes_to_try) - 1:
            log.info("  Retrying fitscut with a smaller cutout...")

    return None


def download_ps1_via_ps1filenames(ra, dec, band, size_deg, output_file):
    """
    Download PS1 full stack image via ps1filenames.py service.

    This gets the full stacked image URL and downloads it (no custom cutout).
    """
    log.info("Method 3: Trying PS1 ps1filenames.py service...")

    size_arcsec = int(size_deg * 3600)

    getim_url = "https://ps1images.stsci.edu/cgi-bin/ps1filenames.py"
    getim_params = {
        "ra": ra,
        "dec": dec,
        "size": size_arcsec,
        "format": "fits",
        "filters": band,
    }

    try:
        response = requests.get(getim_url, params=getim_params, timeout=60)

        if response.status_code == 200:
            # Parse response to get FITS URL
            lines = [line.strip() for line in response.text.split("\n") if line.strip()]
            if len(lines) <= 1:
                log.warning("  No stack FITS files found in ps1filenames response")
                return None

            header = lines[0].split()
            filename_idx = header.index("filename") if "filename" in header else 7
            type_idx = header.index("type") if "type" in header else None
            bad_idx = header.index("badflag") if "badflag" in header else None

            fits_path = None
            for line in lines[1:]:
                parts = line.split()
                if len(parts) <= filename_idx:
                    continue
                if type_idx is not None and parts[type_idx].lower() != "stack":
                    continue
                if bad_idx is not None and parts[bad_idx] != "0":
                    continue
                fits_path = parts[filename_idx]
                break

            if fits_path is None:
                log.warning("  No suitable stack entries in ps1filenames response")
                return None

            fits_url = f"https://ps1images.stsci.edu{fits_path}"
            log.info(f"  Found PS1 stack URL: {fits_url}")

            # Download the FITS file
            fits_response = requests.get(fits_url, timeout=180)

            if fits_response.status_code == 200 and len(fits_response.content) > 10000:
                with open(output_file, "wb") as f:
                    f.write(fits_response.content)
                log.info(
                    f"  Successfully downloaded via ps1filenames: {output_file} ({len(fits_response.content)} bytes)"
                )
                return str(output_file)
            else:
                log.warning(
                    f"  FITS download failed: status {fits_response.status_code}"
                )
        else:
            log.warning(f"  ps1filenames.py returned status {response.status_code}")
    except Exception as e:
        log.warning(f"  ps1filenames.py method failed: {e}")

    return None


class PS1Source:
    name = "ps1"
    max_cutout_deg = None
    zeropoint_keywords = ["ZPT", "FPA.ZP", "MAGZERO", "MAGZPT"]
    #: Empty deliberately. PS1 stacks coadd ~27 dithered 40 s exposures, and
    #: measured on the 2023ixf skycell their bright-star cores are clean,
    #: un-clipped PSFs while the PS1 ``stack.mask`` flags no pixels in the field
    #: at all. The header's ``CELL.SATURATION`` describes a single input cell,
    #: NOT the stack, so masking on it would use a threshold that does not apply.
    saturation_keywords: list[str] = []
    #: ``download_ps1_cutout`` validates coverage and size after EACH of its
    #: three download methods, because that result is what decides whether to
    #: fall through to the next one. Every return path is therefore already
    #: validated, and re-checking in ingest.py would just re-open the FITS.
    fetch_validates_cutout = True

    def band_map(self, config: Any) -> dict[str, str]:
        from stips.core.pipeline import template_band_map

        return template_band_map(config, self.name)

    def default_zeropoint(self, header: Any) -> float:
        ps1_filter = (
            str(header.get("FILTER", header.get("FILTNAM", "r"))).strip().lower()
        )
        return PS1_ZEROPOINTS.get(ps1_filter, 25.0)

    def native_fwhm(self, header: Any) -> float:
        """PS1 stacks have ~1.2" seeing; the header carries no per-stack value."""
        return 1.2

    def fetch(self, ra, dec, src_band, size_deg, out_dir, **kwargs) -> Path:
        result = download_ps1_cutout(ra, dec, src_band, size_deg, str(out_dir))
        if result is None:
            raise TemplateSourceError(
                f"All PS1 download methods failed for {src_band}-band at "
                f"RA={ra}, Dec={dec}. Manual fallback: "
                f"https://ps1images.stsci.edu/cgi-bin/fitscut.cgi"
                f"?ra={ra}&dec={dec}&size={int(size_deg * 3600 / 0.25)}"
                f"&format=fits&filter={src_band}"
            )
        return Path(result)


__all__ = [
    "PS1_ZEROPOINTS",
    "PS1Source",
    "TemplateSourceError",
    "collect_ps1_metadata",
    "download_ps1_cutout",
    "download_ps1_via_fitscut",
    "download_ps1_via_ps1filenames",
    "open_ps1_fits",
    "write_ps1_cutout",
]
