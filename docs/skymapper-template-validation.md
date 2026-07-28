# SkyMapper external-template validation — NGC2298

Date: 2026-07-27
Branch: `feature/skymapper-template`
Design: `docs/superpowers/specs/2026-07-27-skymapper-template-design.md`

## Verdict

**The framework works. SkyMapper's yield is limited by template depth, not by
configuration — but two real bugs were masking that, and both are now fixed.**

Cross-instrument DIA runs on every visit and the astrometry is excellent (zero
systematic offset against a 0.289″ pixel), so the PS1→Y4KCam registration failure
seen on SN 2009Y did not recur.

The headline numbers depend critically on **comparing over the same sky area**.
The SkyMapper cutout covers only **15.9%** of the Y4KCam field (85.2% of each
difference image is `NO_DATA`), so whole-field source totals are not a valid
comparison. Restricted to the template footprint:

| within template footprint | SkyMapper | Coadd |
|---|---:|---:|
| detections | 3 563 | 2 132 |
| matched to the other | 1 081 | — |
| recall of coadd sources | **50.7 %** | — |
| purity (matched / all) | **30.3 %** | — |

SkyMapper recovers about **half** the coadd's sources in the region it covers,
while producing roughly as many again that the coadd does not see. Those extras
are **not random noise** — they recur in a median of 10 of 18 visits, *more*
repeatably than the coadd's own detections — which points at structure imprinted
by the single-epoch template rather than transient noise.

**Use `template.type: coadd` when SN-free epochs exist.** SkyMapper is a
usable-but-shallow fallback whose catalogue needs independent confirmation, and
whose 16% field coverage is a hard operational limit on this camera.

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

### Template coverage — read this before any source count

The SkyMapper cutout is capped at 0.17° (10.2′) against a ~20′ Y4KCam field.
Measured on the warped template: **15.9 % of each science image has template
data; 85.2 % is `NO_DATA`.** Detection correctly respects that mask — **zero**
SkyMapper detections fall outside the footprint — but it means whole-field
totals compare different sky areas and must not be used.

### Source recovery, restricted to the template footprint

| | SkyMapper (tuned) | Coadd (validated) |
|---|---:|---:|
| detections in footprint | 3 563 | 2 132 |
| matched within 1″ | 1 081 | — |
| unmatched | 2 482 | — |
| **recall of coadd sources** | **50.7 %** | — |
| **purity** | **30.3 %** | — |

**Registration is not a factor.** Median offset among matched pairs is +0.000″ in
both RA and Dec (pixel = 0.289″).

### Are the unmatched detections real?

Not settled, but they are **not random noise**. Repeatability across the 18
visits, matching detections within 1″:

| seen in ≥ N visits | SkyMapper | Coadd |
|---|---:|---:|
| ≥ 2 | 64.1 % | 69.1 % |
| ≥ 10 | **50.1 %** | 42.0 % |
| median visits per position | **10** | 6 |

SkyMapper's detections are *more* repeatable than the coadd's. Random noise
appears in one visit; these persist. That rules out shot noise but does **not**
distinguish a real variable from structure baked into the single-epoch template,
which would also imprint identically on every difference image. The latter is
the more parsimonious explanation for detections the deeper coadd does not see.

### Forced photometry at the cluster centre

| | SkyMapper | Coadd |
|---|---:|---:|
| Median `diffFlux` | −29 181 | −3 715 |
| Fraction negative | 94.4 % | 61.1 % |

Read as subtraction quality, not transient photometry — the position is the
crowded cluster core, which contains no transient.

## Does configuration tuning help?

Investigated directly, because the first DIA run turned out to be **misconfigured**.

**Bug 1 — the tuned config was never applied.** `stips dia` did not read
`configs.dia.subtract_images` from the YAML and exposed no override flag, so the
run silently used the ctio1m defaults: `mode='convolveTemplate'`,
`spatialKernelOrder=2`, `nStarPerCell=4`, `sizeCellX/Y=512`. Fixed by adding
`--subtract-config` / `--detect-config` and honouring the YAML keys.

**Bug 2 — the wrong convolution direction.** Forced `convolveTemplate` on a
template (1.68″) blurrier than the science (median ~1.40″) makes the kernel
deconvolve.

With `subtractImages_skymapper.py` actually applied:

| | untuned | tuned |
|---|---|---|
| `mode` | `convolveTemplate` | `auto` |
| exposure convolved | Template ×18 | **Science ×17**, Template ×1 |
| spatial condition number | 2.41e10 | **7.3e8** (33× better) |
| kernel sum | 1.072 | 0.918 |
| recall / purity | 55.0 % / 33.0 % | 50.7 % / 30.3 % |

**The kernel physics improved dramatically; the source yield did not.** `auto`
correctly flipped to convolving the science, confirming the template really is
blurrier, and conditioning improved 33×. Recall and purity moved slightly the
*wrong* way. Kernel conditioning was therefore never the limiting factor.

**Detection thresholds trade purity for recall with no sweet spot** (tuned run,
within footprint):

| cut | matched | unmatched | purity | recall |
|---|---:|---:|---:|---:|
| none | 1 081 | 2 482 | 30.3 % | 50.7 % |
| SNR ≥ 10 | 427 | 494 | 46.4 % | 20.0 % |
| SNR ≥ 20 | 298 | 105 | 73.9 % | 14.0 % |
| not-dipole | 716 | 2 197 | 24.6 % | 33.6 % |

The dipole cut makes purity **worse**: matched detections are dipole-classified
more often (33.8 %) than unmatched (11.5 %), so the classifier is tracking real
structure here, not artifacts.

**Conclusion: configuration tuning fixes real defects but does not raise yield.**
The binding constraint is that the template is a single 100 s exposure.

## The actionable lever: stack multiple SkyMapper frames

The adapter selects the single best-seeing `main` frame. DR4 holds **4 main
i-band frames** at NGC2298:

| frame | FWHM | ZP | MJD |
|---|---:|---:|---:|
| `20160417083802-17` | 1.68″ | 27.95 | 57495.4 |
| `20160417084203-17` | 1.76″ | 27.94 | 57495.4 |
| `20191208132400-31` | 2.07″ | 27.89 | 58825.6 |
| `20191208131957-17` | 2.11″ | 27.88 | 58825.6 |

Stacking all four would improve template SNR by ~**2×** and, more importantly,
average down the per-frame structure that the repeatability test suggests is
imprinting on every difference image. Multi-epoch coaddition was explicitly
scoped **out** of this design; this validation is the evidence that it is the
change most likely to move the numbers.

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

1. **Keep `template.type: coadd` as the southern default.** It achieves ~2× the
   purity over the same sky area and covers the whole field rather than 16 % of it.
2. **`template.type: skymapper` is a shallow fallback covering ~16 % of a Y4KCam
   field.** It recovers about half the coadd's sources in that region at ~30 %
   purity. Usable to get a southern field through the pipeline; its catalogue
   needs independent confirmation.
3. **Do not enable it in `auto`.** The current explicit-only policy is correct;
   nothing here justifies loosening it.
4. **Add multi-frame stacking to the SkyMapper adapter before anything else.**
   It is the one change the evidence points at (see above). After that, a deep
   coadd survey (DECam Legacy Surveys DR10, DES DR2) remains the real answer for
   southern external templates, and the framework makes it one `sources/*.py`
   file plus a `template_band_maps` entry. SkyMapper's ceiling is
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
