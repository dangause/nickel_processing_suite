# SkyMapper external-template validation — NGC2298

Date: 2026-07-27
Branch: `feature/skymapper-template`
Design: `docs/superpowers/specs/2026-07-27-skymapper-template-design.md`

## Verdict

**SkyMapper templates work, and are meaningfully worse than a CTIO self-coadd.**
Cross-instrument DIA succeeded on every visit — better than predicted — but
recovers only **36% of the difference-image sources** and leaves a **~8× larger
systematic residual** in the crowded cluster core. The design's Tier-2,
explicit-only positioning is confirmed empirically: use `template.type: coadd`
whenever SN-free epochs exist; reach for `skymapper` only when they do not.

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

**SkyMapper recovers 36.3% of the coadd-template sources.**

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
the adapter remains appropriate.

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

1. **Keep `template.type: coadd` as the southern default.** It recovers ~2.8× the
   sources and leaves an ~8× smaller systematic residual on the same field.
2. **`template.type: skymapper` is a usable fallback** when no SN-free epochs
   exist to self-coadd — it produces real difference images and real detections,
   not garbage. Expect roughly a third of the sensitivity.
3. **Do not enable it in `auto`.** The current explicit-only policy is correct;
   nothing here justifies loosening it.
4. **The deeper southern-template question remains open.** SkyMapper's ceiling is
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
- The DR4 holdings at this position are thin — 2 `main` frames in total — so
  frame selection had little to choose from.
