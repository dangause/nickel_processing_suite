#!/usr/bin/env python
"""Fit the template-vs-science colour term for an external-template DIA run.

WHAT THIS MEASURES
------------------
An external template (PS1 r) and the science frames (Nickel R) sit on different
photometric systems. ``subtractImages`` fits a PSF-matching kernel with a
*single* global flux scale, so it can null the mean offset but not any
colour-dependent part: red and blue stars end up systematically over- and
under-subtracted, producing repeatable residual DIA detections at real stars.

For every reference star we measure

    dm = -2.5 * log10(F_science / F_template_matched)

on the PSF-matched template (so the PSF difference is already removed) and fit

    dm = k0 + k1 * colour

with ``k0`` removed **per visit**, because that is exactly what the kernel's
global scale absorbs. ``k1`` -- the slope -- is the part the kernel cannot fix
and the quantity a colour-corrected template would have to remove.

SIGN CONVENTION
    k1 < 0  =>  red stars are BRIGHTER in science than the template predicts,
                so the template under-predicts red stars.

WHY THE SLOPE IS NOT SIMPLY THE CONFIGURED COLOUR TERM
    The science frames are already calibrated onto Nickel R using the
    ``*monster*`` entry in ``instruments/nickel/configs/colorterms.py``
    (R: primary=monster_ComCam_r, secondary=monster_ComCam_i, c1=-0.101),
    while the template carries a flat PS1 zeropoint and no colour term at all.
    What we fit here is therefore the NET residual mismatch between those two
    systems, which is the operationally relevant number for DIA -- not a
    from-scratch re-derivation of the PS1->Nickel transformation.

USAGE
    fit_template_colorterm.py REPO DIFF_COLLECTION [DIFF_COLLECTION ...]

Must run inside the LSST stack.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
from lsst.daf.butler import Butler
from lsst.geom import Point2D, SpherePoint, degrees, radians
from lsst.meas.algorithms import ReferenceObjectLoader

REFCAT = "the_monster_20250219_local"
REFCAT_COLL = "refcats"

# Selection thresholds. Deliberately conservative: this is a calibration fit,
# so purity of the star sample matters far more than sample size.
MIN_REF_SNR = 20.0  # per band, in the reference catalogue
ISOLATION_ARCSEC = 6.0  # nearest reference neighbour must be beyond this
EDGE_PIX = 25  # stay this far from the detector edge
MAX_CENTROID_SHIFT_PIX = 2.0  # recentroid must not wander further than this
MIN_MEASURED_SNR = 30.0  # per-star science aperture SNR
CLIP_SIGMA = 3.0
CLIP_ITERS = 5


def robust_line(x, y, clip=CLIP_SIGMA, iters=CLIP_ITERS):
    """Sigma-clipped least-squares line fit. Returns (slope, intercept, mask)."""
    keep = np.isfinite(x) & np.isfinite(y)
    for _ in range(iters):
        if keep.sum() < 5:
            break
        p = np.polyfit(x[keep], y[keep], 1)
        resid = y - np.polyval(p, x)
        sigma = 1.4826 * np.median(np.abs(resid[keep] - np.median(resid[keep])))
        if not np.isfinite(sigma) or sigma <= 0:
            break
        new = keep & (np.abs(resid - np.median(resid[keep])) < clip * sigma)
        if new.sum() == keep.sum():
            break
        keep = new
    p = np.polyfit(x[keep], y[keep], 1)
    return p[0], p[1], keep


def bootstrap_slope(x, y, n=2000, seed=0):
    rng = np.random.default_rng(seed)
    out = np.empty(n)
    idx = np.arange(len(x))
    for i in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        out[i] = robust_line(x[s], y[s])[0]
    return float(np.std(out))


def psf_fwhm_pix(exp):
    bb = exp.getBBox()
    c = Point2D(bb.getCenterX(), bb.getCenterY())
    return float(exp.getPsf().computeShape(c).getDeterminantRadius() * 2.3548)


def measure(image, x, y, r_ap, r_in, r_out):
    """Aperture flux with a local annulus background. None if unusable."""
    r = int(np.ceil(r_out)) + 1
    xi, yi = int(round(x)), int(round(y))
    if not (r <= xi < image.shape[1] - r and r <= yi < image.shape[0] - r):
        return None
    sub = image[yi - r : yi + r + 1, xi - r : xi + r + 1]
    yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
    rad = np.hypot(yy - (y - yi), xx - (x - xi))
    ann = (rad >= r_in) & (rad <= r_out) & np.isfinite(sub)
    if ann.sum() < 20:
        return None
    bg = np.median(sub[ann])
    ap = (rad <= r_ap) & np.isfinite(sub)
    if ap.sum() < 5:
        return None
    npix = int(ap.sum())
    return (
        float(np.sum(sub[ap] - bg)),
        npix,
        float(1.4826 * np.median(np.abs(sub[ann] - bg))),
    )


def centroid(image, x, y, box=3):
    xi, yi = int(round(x)), int(round(y))
    if not (box <= xi < image.shape[1] - box and box <= yi < image.shape[0] - box):
        return None
    sub = image[yi - box : yi + box + 1, xi - box : xi + box + 1].astype(float)
    sub = sub - np.median(sub)
    sub[sub < 0] = 0.0
    tot = sub.sum()
    if tot <= 0:
        return None
    yy, xx = np.mgrid[-box : box + 1, -box : box + 1]
    return float(xi + (sub * xx).sum() / tot), float(yi + (sub * yy).sum() / tot)


def collect(butler, collection, sn_coord, log=sys.stderr):
    refs = list(butler.registry.queryDatasets(REFCAT, collections=REFCAT_COLL))
    loader = ReferenceObjectLoader(
        dataIds=[butler.registry.expandDataId(r.dataId) for r in refs],
        refCats=[butler.getDeferred(r) for r in refs],
    )
    visits = sorted(
        {
            r.dataId["visit"]
            for r in butler.registry.queryDatasets(
                "difference_image", collections=collection
            )
        }
    )
    rows = []
    for v in visits:
        kw = dict(instrument="Nickel", visit=v, detector=0, band="r")
        diff = butler.get("difference_image", collections=collection, **kw)
        tmpl = butler.get("template_matched", collections=collection, **kw)
        dim = diff.image.array.astype(float)
        tim = tmpl.image.array.astype(float)
        sci = dim + tim  # exactly how subtractImages forms the difference
        wcs = diff.getWcs()
        bbox = diff.getBBox()

        fwhm = psf_fwhm_pix(diff)
        r_ap, r_in, r_out = 2.0 * fwhm, 3.0 * fwhm, 5.0 * fwhm

        centre = wcs.pixelToSky(Point2D(bbox.getCenterX(), bbox.getCenterY()))
        rc = loader.loadSkyCircle(centre, 0.12 * degrees, "monster_ComCam_r").refCat
        ra = np.asarray(rc["coord_ra"])
        dec = np.asarray(rc["coord_dec"])
        # Colour basis. MONSTER's synthetic ComCam griz covers only ~7% of rows
        # in this field, so Gaia BP-RP -- present for essentially every row --
        # is the primary colour here; ComCam (r-i)/(g-r) are carried along for
        # the subset that has them, as a cross-check.
        ok_phot = np.ones(len(rc), bool)
        with np.errstate(invalid="ignore", divide="ignore"):
            bp = np.asarray(rc["phot_bp_mean_flux"])
            rp = np.asarray(rc["phot_rp_mean_flux"])
            bpe = np.asarray(rc["phot_bp_mean_fluxErr"])
            rpe = np.asarray(rc["phot_rp_mean_fluxErr"])
            ok_phot &= (
                np.isfinite(bp)
                & (bp > 0)
                & np.isfinite(rp)
                & (rp > 0)
                & (bp / bpe > MIN_REF_SNR)
                & (rp / rpe > MIN_REF_SNR)
            )
            bp_rp = -2.5 * np.log10(bp / rp)

            bands = {}
            for bnd in ("g", "r", "i"):
                bands[bnd] = np.asarray(rc[f"monster_ComCam_{bnd}_flux"])
            g_r = -2.5 * np.log10(bands["g"] / bands["r"])
            r_i = -2.5 * np.log10(bands["r"] / bands["i"])

        # isolation, in the reference catalogue itself (coords are RADIANS)
        n = len(rc)
        isolated = np.ones(n, bool)
        for i in range(n):
            if not ok_phot[i]:
                continue
            d = (
                np.hypot(
                    (ra - ra[i]) * np.cos(dec[i]),
                    dec - dec[i],
                )
                * 206265.0
            )
            d[i] = np.inf
            if np.nanmin(d) < ISOLATION_ARCSEC:
                isolated[i] = False

        for i in range(n):
            if not (ok_phot[i] and isolated[i]):
                continue
            sp = SpherePoint(float(ra[i]) * radians, float(dec[i]) * radians)
            if sn_coord is not None and sp.separation(sn_coord).asArcseconds() < 10.0:
                continue
            p = wcs.skyToPixel(sp)
            x0, y0 = p.getX(), p.getY()
            if not (
                EDGE_PIX <= x0 < dim.shape[1] - EDGE_PIX
                and EDGE_PIX <= y0 < dim.shape[0] - EDGE_PIX
            ):
                continue
            c = centroid(sci, x0, y0)
            if c is None:
                continue
            x, y = c
            if np.hypot(x - x0, y - y0) > MAX_CENTROID_SHIFT_PIX:
                continue
            ms = measure(sci, x, y, r_ap, r_in, r_out)
            mt = measure(tim, x, y, r_ap, r_in, r_out)
            if ms is None or mt is None:
                continue
            f_s, npix, noise = ms
            f_t = mt[0]
            if f_s <= 0 or f_t <= 0:
                continue
            snr = f_s / (noise * np.sqrt(npix)) if noise > 0 else np.inf
            if snr < MIN_MEASURED_SNR:
                continue
            rows.append(
                dict(
                    visit=v,
                    star=int(rc["id"][i]),
                    bp_rp=float(bp_rp[i]),
                    g_r=float(g_r[i]),
                    r_i=float(r_i[i]),
                    dm=float(-2.5 * np.log10(f_s / f_t)),
                    snr=float(snr),
                )
            )
        print(f"  visit {v}: {sum(r['visit'] == v for r in rows)} stars", file=log)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo")
    ap.add_argument("collections", nargs="+")
    ap.add_argument("--sn-ra", type=float, default=210.910750)
    ap.add_argument("--sn-dec", type=float, default=54.311694)
    ap.add_argument("--ref-snr", type=float, default=MIN_REF_SNR)
    ap.add_argument("--meas-snr", type=float, default=MIN_MEASURED_SNR)
    ap.add_argument("--isolation", type=float, default=ISOLATION_ARCSEC)
    ap.add_argument(
        "--min-obs",
        type=int,
        default=3,
        help="require this many visits per star (guards against "
        "one-off measurements dominating the fit)",
    )
    args = ap.parse_args()

    globals()["MIN_REF_SNR"] = args.ref_snr
    globals()["MIN_MEASURED_SNR"] = args.meas_snr
    globals()["ISOLATION_ARCSEC"] = args.isolation
    print(
        f"cuts: ref SNR>{args.ref_snr:g}, measured SNR>{args.meas_snr:g}, "
        f'isolation>{args.isolation:g}"'
    )

    butler = Butler(args.repo)
    sn = SpherePoint(args.sn_ra * degrees, args.sn_dec * degrees)

    allrows = []
    for coll in args.collections:
        print(f"\n=== collecting {coll}")
        rows = collect(butler, coll, sn)
        for r in rows:
            r["collection"] = coll
        allrows += rows

    if not allrows:
        print("no usable stars")
        return

    visits = np.array([r["visit"] for r in allrows])
    stars = np.array([r["star"] for r in allrows])
    dm = np.array([r["dm"] for r in allrows])

    # Remove the per-visit median: that offset is what the kernel's single
    # global flux scale already absorbs, so it carries no information here.
    dm_res = dm.copy()
    for v in np.unique(visits):
        m = visits == v
        dm_res[m] -= np.median(dm[m])

    uniq = np.array(
        [s_ for s_ in np.unique(stars) if (stars == s_).sum() >= args.min_obs]
    )
    dropped = len(np.unique(stars)) - len(uniq)
    keep_rows = np.isin(stars, uniq)
    visits, stars, dm, dm_res = (
        visits[keep_rows],
        stars[keep_rows],
        dm[keep_rows],
        dm_res[keep_rows],
    )
    allrows = [r for r, k in zip(allrows, keep_rows) if k]
    if dropped:
        print(f"\ndropped {dropped} star(s) with < {args.min_obs} observations")
    print(f"\n{'='*70}")
    print(f"{len(allrows)} measurements over {len(np.unique(visits))} visits")
    print(f"{len(uniq)} DISTINCT stars  <-- this, not the measurement count,")
    print("    is the sample size for a colour-term fit: each star is measured")
    print("    many times, so per-measurement errors are NOT independent.")
    print(f"{'='*70}")

    # Aggregate to one point per star. Per-star scatter across visits tells us
    # the measurement floor; scatter BETWEEN stars is what a colour term must
    # explain.
    s_bprp, s_gr, s_ri, s_dm, s_sem, s_n, s_nights = [], [], [], [], [], [], []
    for sid in uniq:
        m = stars == sid
        vals = dm_res[m]
        s_bprp.append(np.median([r["bp_rp"] for r, k in zip(allrows, m) if k]))
        s_gr.append(np.median([r["g_r"] for r, k in zip(allrows, m) if k]))
        s_ri.append(np.median([r["r_i"] for r, k in zip(allrows, m) if k]))
        s_dm.append(float(np.median(vals)))
        s_sem.append(float(np.std(vals) / max(np.sqrt(len(vals)), 1)))
        s_n.append(int(m.sum()))
        s_nights.append(len({r["collection"] for r, k in zip(allrows, m) if k}))
    s_bprp = np.array(s_bprp)
    s_gr = np.array(s_gr)
    s_ri = np.array(s_ri)
    s_dm = np.array(s_dm)
    s_sem = np.array(s_sem)
    s_n = np.array(s_n)
    s_nights = np.array(s_nights)

    order = np.argsort(s_bprp)
    if len(uniq) <= 40:
        print(
            f"\n{'star':>20} {'n obs':>6} {'nights':>7} {'BP-RP':>7} {'r-i':>7} "
            f"{'median dm':>11} {'sem':>8}"
        )
        for j in order:
            print(
                f"{uniq[j]:>20} {s_n[j]:6d} {s_nights[j]:7d} {s_bprp[j]:7.3f} "
                f"{s_ri[j]:7.3f} {s_dm[j]:+11.4f} {s_sem[j]:8.4f}"
            )

    for name, colour in (
        ("Gaia (BP-RP)", s_bprp),
        ("ComCam (r-i)", s_ri),
        ("ComCam (g-r)", s_gr),
    ):
        finite = np.isfinite(colour) & np.isfinite(s_dm)
        if finite.sum() < 5:
            print(
                f"\n--- per-star fit: dm vs {name} --- only {finite.sum()} stars, skipped"
            )
            continue
        slope, icept, keep = robust_line(colour[finite], s_dm[finite])
        colour, s_dm_f = colour[finite], s_dm[finite]
        err = bootstrap_slope(colour[keep], s_dm_f[keep])
        resid = s_dm_f - np.polyval([slope, icept], colour)
        rms_before = float(np.std(s_dm_f))
        rms_after = float(np.std(resid[keep]))
        print(
            f"\n--- per-star fit: dm vs {name} ---  ({keep.sum()} of {len(colour)} stars)"
        )
        print(
            f"  colour range     : {colour[keep].min():.3f} .. {colour[keep].max():.3f}"
        )
        print(f"  slope k1         : {slope:+.4f} +/- {err:.4f} mag/mag")
        print(
            f"  scatter of dm    : {rms_before:.4f} -> {rms_after:.4f} mag after removing the trend"
        )
        print(f"  median per-star measurement sem: {np.median(s_sem):.4f} mag")

    print("\n--- per-collection consistency (per-star, dm vs Gaia BP-RP) ---")
    for coll in args.collections:
        cm = np.array([r["collection"] == coll for r in allrows])
        cs = np.unique(stars[cm])
        if len(cs) < 5:
            print(f"  {coll.split('/')[2]:>10}  only {len(cs)} stars - not fittable")
            continue
        cx, cy = [], []
        for sid in cs:
            m = cm & (stars == sid)
            cx.append(np.median([r["bp_rp"] for r, k in zip(allrows, m) if k]))
            cy.append(float(np.median(dm_res[m])))
        cx, cy = np.array(cx), np.array(cy)
        good = np.isfinite(cx) & np.isfinite(cy)
        cx, cy = cx[good], cy[good]
        s, _, k = robust_line(cx, cy)
        e = bootstrap_slope(cx[k], cy[k])
        print(
            f"  {coll.split('/')[2]:>10}  stars={len(cs):3d}  k1 = {s:+.4f} +/- {e:.4f}"
        )


if __name__ == "__main__":
    main()
