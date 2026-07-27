#!/usr/bin/env python3
"""DEPRECATED shim — use ``stips.pipeline_tools.external_template`` instead.

Kept so external scripts and the ``stips ps1-template`` CLI keep working. All
behavior now lives in the generic external-template framework:

* astropy-only helpers -> ``external_template.imaging``
* PS1 download/metadata -> ``external_template.sources.ps1``
* LSST Exposure/Butler work -> ``external_template.core``
* the argparse entry point -> ``external_template.ingest``

Every name re-exported below has a live caller (the ``stips ps1-template``
command, ``packages/stips/tests/test_ps1_templates.py``, or an out-of-tree
script), so none of them may be dropped without checking those first.
"""

from stips.pipeline_tools.external_template.core import (  # noqa: F401
    convert_astropy_wcs_to_lsst,
    degrade_exposure_psf,
    fits_to_lsst_exposure,
    ingest_exposure_to_butler,
    reproject_to_patch,
)
from stips.pipeline_tools.external_template.imaging import (
    file_covers_target as ps1_file_covers_target,  # noqa: F401
)
from stips.pipeline_tools.external_template.imaging import (
    file_meets_requested_size as ps1_file_meets_requested_size,  # noqa: F401
)
from stips.pipeline_tools.external_template.imaging import (  # noqa: F401
    find_first_image_hdu,
    zeropoint_to_calibration_mean,
)
from stips.pipeline_tools.external_template.ingest import main  # noqa: F401
from stips.pipeline_tools.external_template.sources import get_source
from stips.pipeline_tools.external_template.sources.ps1 import (  # noqa: F401
    PS1_ZEROPOINTS,
    _resolve_ps1_band,
    collect_ps1_metadata,
    download_ps1_cutout,
    download_ps1_via_fitscut,
    download_ps1_via_ps1filenames,
    open_ps1_fits,
    write_ps1_cutout,
)


def convert_ps1_to_lsst_exposure(
    ps1_fits_path, nickel_band, degrade_to_fwhm=None, force_unity_photoCalib=False
):
    """Back-compat wrapper over :func:`fits_to_lsst_exposure`."""
    return fits_to_lsst_exposure(
        ps1_fits_path,
        nickel_band,
        get_source("ps1"),
        degrade_to_fwhm=degrade_to_fwhm,
        force_unity_photocalib=force_unity_photoCalib,
    )


if __name__ == "__main__":
    import sys

    # ``--source`` defaults to "ps1", so ``python -m
    # stips.pipeline_tools.ingest_ps1_template`` keeps its historical meaning.
    sys.exit(main())
