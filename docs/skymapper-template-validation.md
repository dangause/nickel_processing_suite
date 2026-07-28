# SkyMapper external-template validation — NGC2298

Date: 2026-07-27
Branch: `feature/skymapper-template`
Design: `docs/superpowers/specs/2026-07-27-skymapper-template-design.md`

## Verdict

**The machinery works. The science product does not.**

Cross-instrument DIA ran on every visit and the astrometry is excellent — zero
systematic offset against a 0.289″ pixel, so the registration failure mode seen
in the earlier PS1→Y4KCam attempt on SN 2009Y did not recur. But a positional
cross-match against the validated coadd run shows SkyMapper recovers only
**12% of the coadd's difference-image sources** (1 173 of 9 784), and **67% of
what it does detect has no coadd counterpart** and is most likely subtraction
artifact.

That is not "a third of the sensitivity" — it is one real source in eight,
inside a majority-spurious catalogue. **SkyMapper templates should not be used
to support a transient campaign on a field like this.** Use
`template.type: coadd`. Reach for `skymapper` only to get a pipeline running
end-to-end when no SN-free epochs exist, and treat its catalogue as
provisional.

## Setup

| | |
|---|---|
| Field | NGC2298 (globular cluster), RA 102.246542, Dec −36.005333 |
| Science night | 20061216 (held-out epoch), band `i`, 18 visits |
| Baseline | `ngc2298_repo`, `diff/20260726T150523Z/run` — the validated coadd-template run |
| Test | `ngc2298_skymapper_repo` (isolated 123 GB copy), `diff/20260728T001826Z/run` |
| Instrument | CTIO 1.0 m / Y4KCam, single CCD, ~20′ FOV |

Both runs use the **same** held-out science night and the **same** processed
science exposures (`processCcd/20260725T172648Z`). The only variable is the
template source.

### Template actually used

Selected automatically by the adapter from the DR4 SIA:

| Property | Value |
|---|---|
| Frame | `20160417083802-17` |
| `image_type` / `EXPTIME` | `main` / 100 s |
| `QAFWHM` | 1.68189″ |
| `ZPAPPROX` | 27.947 |
| Native scale | 0.4976 ″/px |
| WCS | `RA---TPV` |
| Cutout | 0.17 deg (service maximum) |

Ingested to `templates/skymapper/i` as `template_coadd`, tract 444 / patch 156,
`PhotoCalib = 1.0` with pixels pre-scaled to nJy, provenance recorded as
`TEMPLATE_SOURCE=skymapper`, `TEMPLATE_ZEROPOINT=27.947`,
`TEMPLATE_FWHM_ARCSEC=1.68189`.

## Results

### Difference-image source recovery

| | SkyMapper | Coadd (validated) |
|---|---:|---:|
| Visits with a difference image | **18 / 18** | 18 / 18 |
| Visits with zero sources | 0 | 0 |
| **Total DIA sources** | **3 553** | **9 784** |
| Per-visit min / median / max | 171 / 195 / 228 | 433 / 545 / 624 |

Raw counts alone suggest 36% recovery — **that reading is wrong.** Source
totals do not establish that the same objects were found. See the cross-match
below.

### Cross-match against the validated coadd run

Matching each SkyMapper detection to the nearest coadd detection on the same
visit:

| | |
|---|---:|
| SkyMapper detections | 3 553 |
| …with a coadd counterpart within 1″ | **1 173 (33.0 %)** |
| …with no counterpart | **2 380 (67.0 %)** |
| **Coadd sources actually recovered** | **1 173 / 9 784 = 12.0 %** |

**Registration is not the explanation.** Median offset among matched pairs is
**+0.000″ in RA and +0.000″ in Dec** (Y4KCam pixel = 0.289″). The separation
distribution is bimodal — p10 = 0.03″, p50 = 9.14″ — and the match fraction
plateaus with tolerance (27.1 % at 0.5″, 33.0 % at 1″, 41.3 % at 3″), so the
unmatched detections are not near-misses.

The unmatched 67 % cannot be proven artifact from this test alone, but three
lines of evidence point there: registration is clean, so they are not displaced
counterparts; the coadd is the deeper image and should find *more* real sources,
not fewer; and the plateau rules out a tolerance effect.

### Forced photometry at the cluster centre

| | SkyMapper | Coadd |
|---|---:|---:|
| Measurements | 18 | 18 |
| Median `diffFlux` | **−29 181** | −3 715 |
| Fraction negative | **94.4 %** | 61.1 % |

**Read this as subtraction quality, not transient photometry.** The forced
position is the NGC2298 core — a crowded, bright region containing no transient
— so a large negative residual is expected for *both* templates. The
informative quantity is the ratio: the SkyMapper subtraction leaves a residual
**7.9× larger** and far more systematically one-signed, consistent with poorer
PSF matching and a depth mismatch in a crowded field.

## What contradicted the design's predictions

**The FOV concern did not materialise as a failure.** The design predicted that
a 10.2′ template against a ~20′ Y4KCam field would leave dithered pointings with
no PSF-matching kernel candidates (`NoKernelCandidatesError`). In practice all
18 visits produced difference images and non-zero source counts. The NGC2298
pointings evidently sit close enough to the field centre that the template
covers the science footprint. **This does not generalise** — a campaign with a
wider dither pattern should still expect the failure, and the warning emitted by
the adapter remains appropriate. That warning is driven by the active profile's
`fov_arcmin` (`20.0` for Y4KCam), passed into `fetch()` by `ingest.py`; a
profile that does not declare a FOV gets no warning.

**`mode = "auto"` was the right call.** The template's 1.68″ seeing versus CTIO
science seeing makes the PS1 config's hardcoded `convolveTemplate` unsafe;
`AlardLuptonSubtractConfig.mode.allowed` was confirmed on the installed stack as
`['auto', 'convolveScience', 'convolveTemplate']`.

## Bug found and fixed during validation

Phase B verification exposed a **real defect in code inherited from the PS1
path**, fixed in commit `4185dc1`:

`reproject_to_patch()` warped the cutout onto the skymap patch grid but copied
the `GaussianPsf` across **unchanged in pixel units**. Since `GaussianPsf`
stores width in pixels and the patch grid has a different pixel scale, the
attached PSF silently misrepresented the seeing.

| | |
|---|---|
| Source | `QAFWHM` 1.68189″ at 0.4976 ″/px → σ = 1.44 px (correct) |
| Patch grid | 0.2887 ″/px — the same 1.44 px reads as **FWHM 0.98″** |
| Correct | σ = 2.47 px at patch scale |
| **Error** | **PSF understated by 1.72×** |

Consequence: DIA would believe the template far sharper than it is, and
`mode="auto"` would choose the wrong convolution direction — the exact failure
this design exists to avoid.

**This bug predates the SkyMapper work.** For PS1 (0.25 ″/px → 0.2887 ″/px) the
factor is 0.87 — a 13 % understatement, small enough to have gone unnoticed.
SkyMapper's coarser pixels amplified it to 72 %, which is why a second source
surfaced it. The same commit also fixed loss of the `TEMPLATE_*` provenance keys
during reprojection.

After the fix and re-ingest, the attached PSF reads **FWHM 1.682″** — an exact
match to the source frame.

**Unit tests could not have caught this**: `reproject_to_patch` needs a real
skymap and the LSST stack, so only an end-to-end ingest exercises it. A
regression test now pins the rescaling.

## Recommendation

1. **Keep `template.type: coadd` as the southern default.** It recovers ~8× more
   real sources and leaves an ~8× smaller systematic residual on the same field.
2. **`template.type: skymapper` is a plumbing fallback, not a science fallback.**
   It will get a southern field through the pipeline end-to-end when no SN-free
   epochs exist to self-coadd, and the difference images are real. But at 12 %
   recovery with a majority-spurious catalogue, its detections must not be
   treated as a transient list without independent confirmation.
3. **Do not enable it in `auto`.** The current explicit-only policy is correct;
   nothing here justifies loosening it.
4. **The deeper southern-template question is now urgent, not merely open.**
   The 12 % recovery figure means SkyMapper does not close the southern gap for
   fields without SN-free epochs. SkyMapper's ceiling is
   set by being a single 100 s frame. A deep coadd survey (DECam Legacy Surveys
   DR10, DES DR2) is the natural next adapter, and the framework built here makes
   that one `sources/*.py` file plus a `template_band_maps` entry. Coverage at
   Dec −36 was never verified — `legacysurvey.org` was unreachable throughout —
   and should be confirmed before that work is scoped.

## Caveats

- One field, one night, one band. NGC2298 is a dense globular cluster; results
  in a sparse field may differ substantially in both directions.
- Only `i` was testable. Y4KCam's `v` is Johnson V (~551 nm) while SkyMapper's
  `v` is a Strömgren-like violet band (~384 nm), so `v` is deliberately unmapped.
- The forced-photometry comparison is at the cluster core, not a transient. It
  characterises subtraction residuals, not transient photometric accuracy.
- "No counterpart" is evidence of, not proof of, a spurious detection. Confirming
  the 2 380 unmatched sources as artifacts would need visual inspection of the
  difference stamps or a shape/SNR cut.
- The DR4 holdings at this position are thin — 2 `main` frames in total — so
  frame selection had little to choose from.
