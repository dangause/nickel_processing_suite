# Template-vs-science colour term for PS1 external templates

Measured 2026-07-31 on `nickel_smoketest_repo`, SN 2023ixf field (M101), Nickel
r-band, after the PS1 asinh decode fix.

**Bottom line:** a colour trend is detected at ~5σ
(`k1 = −0.070 ± 0.015` mag per mag of Gaia BP−RP), in the physically expected
direction, and it explains roughly two thirds of the per-star residual scatter.
It is **not calibration-grade**: the fit rests on 18 stars, the two nights
disagree by ~2σ, and colour does not explain the scatter at the blue end. Do
**not** build a colour-corrected template from these coefficients. Refit on a
dense standard field first.

## Why this matters

`subtractImages` fits a PSF-matching kernel with a **single global flux scale**.
It can null the mean template-to-science offset but nothing colour-dependent, so
red and blue stars are systematically over- and under-subtracted. Those show up
as repeatable DIA detections at real, non-variable stars — the residual ~4
sources/visit left after the asinh fix (see `CHANGELOG.md`).

The science frames are already calibrated onto Nickel R using the `*monster*`
entry in `instruments/nickel/configs/colorterms.py`
(R: `primary=monster_ComCam_r`, `secondary=monster_ComCam_i`, `c1=−0.101`).
The template carries a flat PS1 zeropoint of 25.0 and **no** colour term —
nothing in the DIA or external-template path references `colorterms.py` at all.
What is fitted here is therefore the *net residual mismatch* between those two
systems, which is the operationally relevant number for DIA, not a from-scratch
re-derivation of PS1 → Nickel.

## Method

`scripts/analysis/fit_template_colorterm.py`. For each reference star in each
visit:

1. Reconstruct the science frame as `difference_image + template_matched` —
   exactly how `subtractImages` forms the difference, so both sides share a
   pixel grid, a PSF (the template is already matched) and a kernel scale.
2. Aperture photometry at the reference position on both images: radius
   `2×FWHM`, local background from a `3–5×FWHM` annulus (the annulus matters —
   M101 fills this field).
3. `dm = −2.5·log10(F_science / F_template_matched)`.
4. Subtract the **per-visit median** `dm`. That offset is precisely what the
   kernel's global scale already absorbs, so it carries no information.
5. Aggregate to **one point per star** (median across visits), then fit
   `dm = k0 + k1·colour` with 3σ clipping and a bootstrap error.

Cuts: reference SNR > 10 per band, isolation > 6″, ≥ 25 px from the detector
edge, recentroid shift < 2 px, measured science aperture SNR > 15, ≥ 3 visits
per star, and > 10″ from the SN.

### The step that changes the answer

Aggregating per star is not cosmetic. The 25 visits observe the *same* handful
of stars, so a per-measurement fit sees ~250 points that are really ~18
independent ones. Fitting per measurement gives `k1` errors ~5× too small and
produced a spurious 20σ disagreement between the two nights. **Sample size here
is the number of distinct stars, not the number of measurements.**

### Colour basis

Gaia BP−RP, not ComCam g−r/r−i. MONSTER returns 683 rows in this 6.3′ field but
only **48 (7%)** carry synthetic ComCam *griz*; Gaia BP/RP is present for
essentially all of them. ComCam colours are still reported as a cross-check on
the subset that has them.

## Results

18 distinct stars, 250 measurements, 25 visits across both nights.

| colour basis | stars used | `k1` (mag/mag) | significance |
|---|---|---|---|
| **Gaia BP−RP** | 12 of 18 | **−0.0704 ± 0.0145** | 4.9σ |
| ComCam (g−r) | 7 of 8 | −0.0920 ± 0.0521 | 1.8σ |
| ComCam (r−i) | 7 of 8 | −0.1128 ± 0.0751 | 1.5σ |

Sign convention: `k1 < 0` means red stars are **brighter** in the science frame
than the template predicts — i.e. the PS1-r template under-predicts red stars.
That is the expected direction, Nickel R being redder and broader than PS1 r.

Scatter in `dm` across stars: **0.104 mag → 0.034 mag** after removing the
trend, against a median per-star measurement error of 0.019 mag.

### Per-night consistency — the main weakness

| night | stars | `k1` (BP−RP) |
|---|---|---|
| 20230521 | 9 | −0.141 ± 0.052 |
| 20230519 | 18 | −0.032 ± 0.019 |

Same sign, but a factor ~4 apart and inconsistent at ~2σ. A pure bandpass
colour term is a fixed property of the filters and must reproduce night to
night. Something else is contributing — plausibly differential atmospheric
extinction with airmass, or aperture effects from the differing seeing
distributions (1.27–1.89″ on 20230521).

### What colour does not explain

Six of 18 stars are 3σ-clipped. The blue end is the problem: stars at
BP−RP ≈ −0.09 to +0.06 scatter over `dm` = −0.087 to +0.213, a 0.3 mag spread at
essentially fixed colour and far above the ~0.02 mag measurement error. The red
end (BP−RP 1.78–2.50) is tight, −0.062 to −0.095. So a real per-star effect
exists that a single colour cannot describe. Candidates, not separable with 18
stars: genuine variability between the PS1 stack epoch (2010–2014) and 2023,
PS1 stack photometry errors on individual stars, residual blending, and BP−RP
being a poor proxy for the r-band-relevant colour for some spectral types.

## What a perfect colour correction would buy

Per-star residuals would fall from ~10% to ~3% in flux (0.104 → 0.034 mag). A
star of science SNR `S` leaves a residual of roughly `0.10·S` now versus
`0.032·S` corrected, so the 5σ detection threshold would move from stars above
S ≈ 50 to stars above S ≈ 156 — most of the residual ~4 detections/visit. This
is an extrapolation from the fit, not a measured result.

## Recommendations

1. **Do not** apply these coefficients. The per-night disagreement and the blue-end
   scatter mean they would correct 20230521 and mis-correct 20230519.
2. **Refit on a dense standard field** before building anything on this — SA98 or
   the Landolt fields the repo already uses (`stips landolt-validate`,
   `instruments/nickel/colorterms/`) give hundreds of well-measured stars over a
   wide colour baseline, instead of 18. Include airmass as a second term to test
   the atmospheric hypothesis.
3. **Cheaper mitigations that do not need the colour term at all:** raise
   `detection.thresholdValue` from 3.0 to 5.0 in
   `instrument_defaults/configs/dia/detectAndMeasure.py` (this removes a separate
   ~160/visit noise population), and filter on positional repeatability — these
   residuals recur at fixed sky positions, so they are cheap to reject downstream.
4. If a corrected template is eventually built, the mechanism is to synthesise it
   from PS1 **r + i** stacks rather than ingesting PS1 r alone; a single-band
   template has one flux scale and cannot match blue and red stars at once.

## Reproducing

```bash
source $STACK_DIR/loadLSST.bash && setup lsst_distrib
cd <worktree> && setup -k -r packages/obs_stips
export INSTRUMENT_DIR=$PWD/instruments/nickel

python scripts/analysis/fit_template_colorterm.py \
    /Users/dangause/Developer/lick/data/nickel_smoketest_repo \
    Nickel/runs/20230521/diff/20260731T145256Z \
    Nickel/runs/20230519/diff/20260731T145450Z \
    --meas-snr 15 --ref-snr 10
```

Tunable: `--ref-snr`, `--meas-snr`, `--isolation`, `--min-obs`. Loosening the
first three does not grow the sample much — the binding constraint is how few
stars in a 6.3′ high-latitude field are bright and isolated enough to measure.
