"""Spike: does makeSkyWcs preserve SkyMapper's TPV distortion terms?

Run INSIDE the LSST stack:
    python scripts/analysis/spike_tpv_wcs.py /tmp/sm_spike/sm_i.fits
"""

import sys

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

import lsst.geom as geom
from lsst.afw.geom import makeSkyWcs
from lsst.daf.base import PropertyList


def main(path):
    hdu = fits.open(path)[0]
    awcs = WCS(hdu.header)
    print("CTYPE:", awcs.wcs.ctype)
    print("has PV terms:", len(awcs.wcs.get_pv()) > 0)

    header = awcs.to_header(relax=True)
    md = PropertyList()
    for key, value in header.items():
        if key and value is not None:
            if isinstance(value, (int, float, bool)):
                md.set(key, value)
            elif isinstance(value, str):
                md.set(key, str(value))
    lwcs = makeSkyWcs(md)

    ny, nx = hdu.data.shape
    xs = np.linspace(10, nx - 10, 7)
    ys = np.linspace(10, ny - 10, 7)
    worst = 0.0
    for x in xs:
        for y in ys:
            ra_a, dec_a = awcs.all_pix2world(x, y, 0)
            sp = lwcs.pixelToSky(geom.Point2D(float(x), float(y)))
            sep = (
                geom.SpherePoint(
                    float(ra_a) * geom.degrees, float(dec_a) * geom.degrees
                )
                .separation(sp)
                .asArcseconds()
            )
            worst = max(worst, sep)

    px = 0.4976
    print(f"worst astropy-vs-LSST separation: {worst:.4f} arcsec = {worst/px:.3f} px")
    print("VERDICT:", "TPV_OK" if worst < 0.1 * px else "TPV_NEEDS_RESAMPLE")


if __name__ == "__main__":
    main(sys.argv[1])
