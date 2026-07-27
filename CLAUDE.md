# STIPS — Small Telescope Image Processing Suite

## Git Commit Rules

- **NEVER** include `Co-Authored-By` lines in commit messages
- **NEVER** add Claude as a contributor, co-author, or collaborator in any form

## Project Overview

**STIPS** (Small Telescope Image Processing Suite) brings the **LSST Science
Pipelines** to 1-meter class telescopes. It wraps the Rubin/LSST reduction stack
with the per-telescope plumbing — a declarative instrument profile, prefab YAML
pipelines, and a unified `stips` CLI — needed to run survey-grade calibration,
difference imaging (DIA), forced photometry, and lightcurve extraction on
small-telescope data. The primary use case is **transient astronomy**
(supernova monitoring campaigns) at the **Nickel 1-m** at Lick Observatory.

Supported instruments today: **Nickel 1-m** (reference) and **CTIO 1.0m /
Y4KCam** (second instrument; 4-amp camera, on-chip binning, NOIRLab archive
fetch). A new telescope is added by dropping a declarative profile directory
under `instruments/<name>/` — no per-instrument code package. See
`docs/forking-stips.md`.

**Supported LSST stack:** release `v30_0_3` (what the Docker images build on and
the docs validate against). CI pins the weekly `w_2025_32`; a scheduled canary
tracks `w_latest`. Before bumping the stack, follow
`docs/stack-bump-runbook.md`.

## Architecture: framework + declarative instrument

The framework is **two packages** plus **declarative instrument directories**.
A telescope is *not* a code package — it is a directory loaded by path.

- **`packages/stips`** — the `stips` CLI, pipeline tooling, framework-core, and
  the profile *types* (`InstrumentProfile`, `Site`, `Field`, `hook`,
  `CollectionNames`). It loads the active instrument's `profile.py` **by path**
  from the `INSTRUMENT_DIR` env var and drives all collection names, Butler
  queries, and skymap behavior from that profile.
- **`packages/obs_stips`** — generic, instrument-neutral LSST glue
  (`lsst.obs.stips`). It *synthesizes* a concrete, registerable LSST
  instrument/translator/raw-formatter from the profile at import time. Butler
  registers the fixed class `lsst.obs.stips.active.Instrument` for **every**
  instrument (the instrument is re-resolved from `INSTRUMENT_DIR` on each
  import). Also ships the shared PipelineTasks (`lsst.obs.stips.tasks.*`) and
  the reference pipelines/configs (`instrument_defaults/`).
- **`instruments/<name>/`** — a declarative profile directory: a `profile.py`
  (one `InstrumentProfile(...)` plus a handful of `@hook` quirk functions), a
  camera geometry (a `camera/<name>.yaml` or an in-memory `CameraSpec`), an
  optional `fetch.py` data-fetch hook, and *optional* `pipelines/`/`configs/`
  override dirs. `instruments/nickel/` is the reference profile;
  `instruments/ctio1m/` is the second instrument.

**No `INSTRUMENT_PACKAGE`, no `obs_nickel` package, no `.env`/`-p` profiles.**
The old package-based instrument selection is gone. `INSTRUMENT_PACKAGE` in a
config's `env:` block is now **actively rejected** with an error
(`config.py`) — set `INSTRUMENT_DIR` instead.

### Pipeline/config resolution (instrument-dir-first)

Pipelines and config overrides resolve **instrument-dir-first, else framework
default** via `Config.resolve_pipeline()` / `resolve_config()`. A fork overrides
one file by dropping a same-named file into its own
`instruments/<x>/pipelines/` or `configs/`; everything else inherits the
framework defaults in `packages/obs_stips/instrument_defaults/`. The defaults
tier is neutral: Nickel-FITTED science calibration (Landolt `colorterms.py`,
`calibrateImage/tuned_configs/`, the Nickel-band `refcats_gaia_ps1.py`) lives in
`instruments/nickel/configs/`; `ctio1m` ships a `configs/` dir with its skymap
tweak. The tiering contract (what a fork inherits vs MUST review) is documented
in `packages/obs_stips/instrument_defaults/README.md`.

## Repository Structure

```
stips/
├── packages/                # Framework only
│   ├── stips/                # stips CLI + pipeline tooling + framework-core (main dev focus)
│   ├── obs_stips/            # Generic LSST glue (lsst.obs.stips) + instrument_defaults/ pipelines & configs
│   └── refcats/              # Reference-catalog tooling (Gaia DR3 / PS1 shard dump + ingest)
├── instruments/             # Declarative instrument profiles (loaded via INSTRUMENT_DIR)
│   ├── nickel/              # Reference profile (profile.py, camera/, fetch.py, tests/)
│   │   ├── configs/         # Nickel-fitted calibration (colorterms.py, calibrateImage/tuned_configs/, refcats_gaia_ps1.py)
│   │   ├── obs_nickel_data/ # Curated Nickel calibrations (defect maps) — a real EUPS data package
│   │   ├── testdata/        # Test fixtures (testdata_nickel EUPS product)
│   │   ├── defects/         # Defect-mask generation (stips-defects-build)
│   │   ├── colorterms/      # Color-term fitting (stips-colorterms-fit)
│   │   ├── tuning/          # Pipeline-tuning utilities (stips-tune-calibrate-image)
│   │   └── vendor/lick_searchable_archive/  # Vendored Lick archive (client used by fetch.py)
│   └── ctio1m/              # CTIO 1.0m / Y4KCam (4-amp camera, configs/, tests/)
├── scripts/
│   ├── config/             # Per-target YAML configs (2023ixf, 2020wnt, ctio1m, ...)
│   ├── pipeline/           # Bootstrap shell script (delegated to via run_with_stack)
│   └── utilities/          # Helper scripts
├── bps/                    # BPS configs (base.yaml, sites/, pipelines/)
├── docker/                 # Dockerfile(s), docker-compose, Singularity def
├── docs/                   # architecture.md, getting-started.md, forking-stips.md, audit/, ...
└── pyproject.toml          # uv workspace
```

## Key Packages

### packages/stips (primary development focus)

Provides the `stips` CLI (`stips = "stips.cli:main"`) and `stips-*` auxiliary
console scripts (`stips-dia-lightcurve`, `stips-eda-butler`, `stips-skymap-make`,
`stips-archive-ingest-ps1`, ...).

**Core modules (`stips.core.*`):**
- `config` — Loads config from a YAML `env:` block (the SOLE config source);
  `load_active_profile()` imports the active instrument profile by path from
  `INSTRUMENT_DIR`. Rejects the removed `INSTRUMENT_PACKAGE` key.
- `stack` — LSST stack activation and command execution (`run_with_stack()`).
- `calibs` — Nightly calibration processing (bias, flat, defects).
- `crosstalk` — Measure/certify intra-detector crosstalk (multi-amp cameras).
- `science` — Science processing (ISR, WCS, photometry); `target_ra`/`target_dec`
  enable pre-flight coordinate validation; fallback `calibrateImage` configs.
- `dia` — Difference imaging; detects template-overlap failures (diff_count==0).
- `ps1_template` — PS1 template ingestion (configurable cutout size).
- `coadd` — Coadd template building from multiple nights.
- `fphot` — Forced photometry at RA/Dec (per-band).
- `lightcurve` — Lightcurve extraction; `LightcurveConfig` dataclass for display
  options (y-axis/x-axis mode, distance modulus, explosion date, error filter).
- `refcat` — On-demand Gaia DR3 + PS1 refcat fetch/convert/ingest for a target cone.
- `run` — YAML-driven full-pipeline orchestration.
- `bps` — HPC batch submission (Slurm / HTCondor / local Parsl).
- `pipeline` — Shared utilities: re-exports `CollectionNames`; coordinate/data
  validation (`find_bad_coord_exposures()`).
- `dataset_types` — Central Butler dataset-type name constants (e.g.
  `difference_image`, `forced_phot_diffim_radec`), so a stack rename is one edit.

- `stips.collections` — `CollectionNames(night, run_ts, *, prefix)` builds
  standard collection names parameterized by the profile's `collection_prefix`.
- `stips.profile` — `InstrumentProfile` dataclass and `Site`/`Field`/`CameraSpec`/
  `CrosstalkSpec`/`hook` framework types.

### packages/obs_stips

Generic, instrument-neutral LSST glue shared by all instruments (translator/
formatter/instrument base classes, the `active` synthesizer that binds a profile
onto them, the shared PipelineTasks, and the reference `instrument_defaults/`
pipelines & configs). Instrument profiles build on top of this.

**Key framework-default pipeline files** (`packages/obs_stips/instrument_defaults/pipelines/`):
- `DRP.yaml` — Data-release pipeline (ISR, calibration, coaddition); includes the
  relaxed thresholds for `makeDirectWarp`, `BestSeeingQuantileSelectVisitsTask`, etc.
- `DIA.yaml` — Difference imaging.
- `ForcedPhotRaDec.yaml` — Forced photometry at RA/Dec.
- `CpBias.yaml` / `CpFlat.yaml` — Calibration builds.
- `analysis-dia-lightcurve.yaml` — Lightcurve extraction.

### instruments/nickel/obs_nickel_data

A real EUPS data package (co-located under the instrument tree; set up by name
via the stack activation). Ships curated Nickel defect maps under
`Nickel/defects/`. Resolved at runtime by `resolve_data_package_dir()` via the
profile's `obs_data_package` field (co-located precedence).

## Configuration System

There is **one** config source: the `env:` block of the YAML file you pass with
the group-level `-c/--config` flag. No `.env` files, no `-p/--profile` flag, no
`os.environ` fallback for config values.

```bash
# The -c YAML supplies REPO/STACK_DIR/INSTRUMENT_DIR/RAW_PARENT_DIR
stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml env
stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml calibs 20230519
```

Self-contained config `env:` block:

```yaml
env:
  REPO: /path/to/butler/repo
  STACK_DIR: /path/to/lsst_stack
  INSTRUMENT_DIR: /path/to/stips/instruments/nickel   # the declarative instrument dir
  RAW_PARENT_DIR: /path/to/raw/data                    # contains YYYYMMDD/raw/
  REFCAT_REPO: /path/to/refcats                        # optional
  CP_PIPE_DIR: "${STACK_DIR}/cp_pipe"                  # optional; ${VAR} expands within env:
  # CCD_BINNING: 2                                     # optional; scale camera for 2x2 raws (default 1)
```

### Required config keys

| Key | Description |
|-----|-------------|
| `REPO` | Path to Butler repository |
| `STACK_DIR` | Path to LSST stack installation |
| `INSTRUMENT_DIR` | Path to the active declarative instrument dir (e.g. `instruments/nickel`, `instruments/ctio1m`) — must contain `profile.py` |
| `RAW_PARENT_DIR` | Parent directory for raw data (contains `YYYYMMDD/raw/`) |

### Optional config keys

| Key | Description |
|-----|-------------|
| `REFCAT_REPO` | Path to reference catalog repository |
| `CP_PIPE_DIR` | Path to cp_pipe (auto-discovered from the stack if unset) |
| `CCD_BINNING` | On-chip binning factor; scales camera geometry (default 1 = unbinned) |
| `LICK_ARCHIVE_DIR` | Path to the Lick archive client (Nickel `download`) |
| `NOIRLAB_PROPOSAL` | Optional proposal-id filter for the CTIO NOIRLab `download` |

> `INSTRUMENT_PACKAGE`, `OBS_NICKEL`, and `.env`/`-p` profiles are **removed**.
> A lingering `INSTRUMENT_PACKAGE` in `env:` raises an error telling you to set
> `INSTRUMENT_DIR` instead.

## CLI Commands

All commands take the config via the group-level `stips -c <config.yaml>
<command>`. Verify options with `stips <command> --help`.

- `stips env` — Show/validate configuration; check the LSST stack.
- `stips bootstrap` — Initialize the Butler repo (create repo, register the
  instrument, ingest refcats, register the skymap).
- `stips download <night>...` — Fetch raw data via the profile's `fetch_data`
  hook (Nickel → Lick archive; CTIO → NOIRLab). Nights default to the config's
  `science:`/coadd-`template:` night lists.
- `stips calibs <night>` — Nightly calibrations (bias, flat, defects). `-j/--jobs`.
- `stips measure-crosstalk <nights>...` — Measure & certify intra-detector
  crosstalk (multi-amp cameras; requires a `CrosstalkSpec` in the profile).
- `stips science <night>` — Science processing. `--object`, `--ra`/`--dec`
  (enables coordinate validation), `--skip-coadds`, `--bad`, `--calibrate-config`.
- `stips dia <night>` — Difference imaging. `--auto` (auto-discover template) or
  `--template`, `--prefer-ps1`, `-b/--band`, `--object`.
- `stips ps1-template --ra --dec --band {r,i}` — Download/ingest a PS1 template.
  `--size` (deg, default 0.2), `--degrade-seeing`, `--tract`, `--overwrite`.
- `stips fphot <night> --ra --dec` — Forced photometry at RA/Dec. `-b/--band`,
  `--image-type {visit,diffim,both}` (default `diffim`).
- `stips lightcurve --ra --dec --collections <glob>` — Extract a lightcurve.
  `--dataset-type`, `--y-axis`, `--x-axis`, `--explosion-mjd`,
  `--distance-modulus`, `--max-mag-err`, `--min-snr`, `--band`. Can run
  standalone with `--repo`/`--stack-dir` instead of `-c`.
- `stips calib-metrics -o <csv>` — Dump per-visit astrometric/photometric
  calibration metrics to CSV.
- `stips landolt-validate --catalog <csv> -o <csv>` — Validate photometric
  calibration against Landolt standards.
- `stips clean` — Remove processing runs for re-runs. `--night`, `--step`,
  `--dry-run`, `-y`.
- `stips run` — YAML-driven full-pipeline orchestration. `--dry-run`,
  `--site {local,slurm,htcondor}`, `--concurrent`.
- `stips refcat fetch|status --ra --dec` — On-demand Gaia/PS1 refcat coverage.
- `stips bps submit|status|cancel|list` — BPS batch submission and management.
- `stips dashboard` — Browser-based pipeline monitoring (needs the
  `stips[dashboard]` extra).
- `stips provenance sync|mark-deleted` — Maintain the run-provenance document.

## LSST Stack Integration

`stips` runs in its own venv and does not `import lsst`. LSST commands (pipetask,
butler) are wrapped by `run_with_stack()` in `core/stack.py`, which:
1. Sources the LSST stack loader (`loadLSST.bash`).
2. Sets up `lsst_distrib` and `obs_stips`; exports `INSTRUMENT_DIR`,
   `STIPS_DEFAULTS` (framework defaults dir), `STIPS_PS1_BAND_MAP` (the profile's
   `ps1_band_map` as JSON, read by in-stack pex_config files that must not import
   the profile — see Common Issues), and config values as env vars.
3. Runs the command in the activated environment.

Where STIPS needs data *out* of the stack (Butler queries), it runs a small
snippet inside the stack that returns JSON, keeping the venv import-free.

## Data Flow

### Collection Naming Convention

Built by `stips.collections.CollectionNames`, parameterized by the profile's
`collection_prefix` (`Nickel` for the reference profile — a fork's prefix
replaces `Nickel/`). Downstream consumers (DIA, coadd, fphot) should use the
**CHAINED parent** (`processCcd/{ts}`), not individual RUN collections, since the
parent includes both the primary config and any successful fallback configs.

```
Nickel/raw/{night}/{ts}                               # RUN: ingested raw data
Nickel/cp/{night}/{bias,flat}/{ts}/run                # RUN: constructed calibs
Nickel/calib/{night}                                  # Certified calibrations
Nickel/calib/current                                  # CHAIN: unified calibration chain
Nickel/calib/curated                                  # CHAIN: camera geometry + defects
Nickel/calib/crosstalk                                # CALIBRATION: certified crosstalk
Nickel/runs/{night}/processCcd/{ts}                   # CHAINED: unified science outputs (use this)
Nickel/runs/{night}/processCcd/{ts}/run               # RUN: primary calibrateImage config
Nickel/runs/{night}/processCcd/{ts}/run_fb1           # RUN: fallback 1 (if used)
Nickel/runs/{night}/processCcd/{ts}/run_fb2           # RUN: fallback 2 (if used)
Nickel/runs/{night}/coadd/{ts}/run                    # RUN: per-night coadd outputs
Nickel/runs/{night}/diff/{ts}/run                     # RUN: difference imaging outputs
Nickel/runs/{night}/forcedPhotRaDec/{ts}/diffim_{band}  # RUN: forced phot on diff images
Nickel/runs/{night}/forcedPhotRaDec/{ts}/visit_{band}   # RUN: forced phot on visit images
templates/ps1/{band}                                  # RUN: PS1 external templates
templates/deep/tract{N}/{band}                        # RUN: Nickel coadd templates
```

### Pipeline Workflow

1. **Bootstrap** — Create repo, register instrument, ingest refcats (Gaia DR3,
   PS1), register skymap.
2. **Templates** — PS1 template ingestion (r/i) or Nickel coadd template building
   (b/v/r/i).
3. **Calibs** — Ingest raws, build bias/flat, certify calibrations.
4. **Science** — ISR, WCS fitting, photometric calibration (optional coordinate
   validation).
5. **DIA** — Image subtraction per night per band, source detection.
6. **Forced Photometry** — Per night per band at target RA/Dec on diff images.
7. **Lightcurve** — Combined multi-band lightcurve from forced-phot results.

## Observing Night vs UT Day

Lick observations use **local date** (Pacific time) for the observing night, but
FITS headers carry **UT dates**. The convention:
- Observing night `20230519` (local) → UT `day_obs` `20230520`.
- Pipeline collections use the observing night for human readability.
- Butler queries use the UT `day_obs` for data selection.
- The mapping is the profile's `night_to_dayobs_offset_days` (Nickel and CTIO: `1`).

## Target Campaigns

### 2023ixf (SN 2023ixf in M101)
- RA: 210.910750°, Dec: 54.311694° (14:03:38.580, +54:18:42.10)
- Distance: 6.7 Mpc (distance_modulus: 29.05)
- Explosion MJD: 60082.75 (2023-05-19)
- Nights: 20230519, 20230521, 20230523, ... through 20231211
- Config: `scripts/config/2023ixf/`

### 2020wnt (SN 2020wnt, SLSN-I at z=0.032)
- RA: 56.658125°, Dec: 43.229250° (03:46:37.950, +43:13:45.30)
- Explosion MJD: 59180.0 (2020-11-27)
- Nights: 20201207, 20210228, 20210324, ... through 20211111
- Config: `scripts/config/2020wnt/`

## Development Setup

```bash
cd stips
uv sync --group dev            # installs framework packages (stips, obs_stips, ...)
stips --help                   # verify the CLI
stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml env
```

Instrument profiles under `instruments/` are loaded by path at runtime — there is
no per-instrument package to install.

## YAML-Driven Pipeline Orchestration

`stips -c <config.yaml> run` orchestrates the full pipeline from one
self-contained YAML. Key sections:

```yaml
env: { REPO, STACK_DIR, INSTRUMENT_DIR, RAW_PARENT_DIR, REFCAT_REPO, ... }
object: "2023ixf"       # matched against FITS OBJECT (partial, case-insensitive)
ra: 210.910750          # full TNS precision — see Coordinate Precision below
dec: 54.311694
bands: ["r", "i"]

template:
  type: ps1             # "ps1" (r/i) or "coadd" (b/v/r/i)
  size: 0.3             # PS1 cutout size in degrees
  degrade_seeing: 2.0   # optional: convolve PS1 to match Nickel seeing
  nights: [...]         # for coadd type: SN-free template nights

science:
  nights: [20230519, 20230521, ...]

configs:                # optional overrides (paths resolve instrument-dir-first, else framework)
  science:
    calibrate_image: calibrateImage/tuned_configs/dense_strict.py
    calibrate_image_fallbacks: [...]
    colorterms: apply_colorterms.py
  dia:
    subtract_images: dia/subtractImages.py
    detect_and_measure: dia/detectAndMeasure.py

options:
  jobs: 6
  concurrent_nights: 3
  forced_phot: true
  forced_phot_image_type: diffim   # visit, diffim, or both
  continue_on_error: true
  use_fallbacks: true              # try fallback calibrateImage configs on failure

lightcurve:
  enabled: true
  dataset_type: forced_phot_diffim_radec
  min_snr: 2
  max_mag_err: 1.0
  y_axis: apparent_mag             # apparent_mag | absolute_mag | flux_nJy | flux_adu
  x_axis: days_since_explosion     # mjd | days_since_explosion
  explosion_mjd: 60082.75          # required when x_axis=days_since_explosion
  # distance_modulus: 29.05        # required when y_axis=absolute_mag
```

Target configs live in `scripts/config/{target}/`:
- `pipeline_ps1_template.yaml` — PS1-based DIA (r/i bands only)
- `pipeline_nickel_template.yaml` — Nickel coadd-based DIA (all bands)

## Logging

Pipeline runs create a unified log directory at `logs/{RUN_ID}/` with
subdirectories per step (`bootstrap/`, `calibs/`, `science/`, `dia/`, `fphot/`,
`lightcurve/`, `templates/{band}/`, ...), plus `pipeline.log` (Python
orchestration) and `summary.txt` (final success/failure counts). Per-night logs
are split by exposure for easier debugging.

## Testing

```bash
# Framework + instrument-profile tests (plain venv)
uv sync --group dev
python -m pytest -q

# Individual modules
pytest packages/obs_stips/tests/            # glue + camera builder + graph-build validation
pytest instruments/ctio1m/tests/            # an instrument profile (translator, camera, fetch)

# Graph-build validation: every pipeline YAML must build a qgraph
pytest packages/obs_stips/tests/test_pipeline_graphs.py

# Full pipeline orchestration (needs a real repo + stack)
stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml run --dry-run
stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml run
```

## Key Design Decisions

1. **Python CLI over Makefile** — All pipeline operations go through the `stips`
   CLI; the Makefile is dev-tasks only (lint, test).
2. **Declarative instrument profiles** — A telescope is a `instruments/<name>/`
   directory loaded by path (`INSTRUMENT_DIR`), not a code package. `obs_stips`
   synthesizes the LSST instrument from the profile at runtime.
3. **Single-YAML config** — The `-c` YAML's `env:` block is the sole config
   source; the same file drives `stips run`.
4. **Instrument-dir-first resolution** — Pipelines/configs resolve from the
   instrument dir first, else the framework defaults; a fork overrides one file
   at a time.
5. **Per-band DIA and fphot** — Run per night per band so partial band failures
   don't block other bands.
6. **Fallback config strategy** — Science tries a primary `calibrateImage`
   config, then relaxed fallbacks, each in its own RUN collection (`/run_fb1`,
   ...) under the shared CHAINED parent (see Common Issues).
7. **CP_PIPE_DIR auto-discovery** — Queried from eups if unset.

## Common Issues

### CP_PIPE_DIR not found
Auto-discovered from the LSST stack (`config.py:_discover_cp_pipe_dir`). If it
fails, the stack may not be installed, or set `CP_PIPE_DIR` explicitly in `env:`.

### Raw data not found
Ensure `RAW_PARENT_DIR/{night}/raw/` exists with FITS files. Use `stips download
<night>` to fetch from the archive (Nickel → Lick; CTIO → NOIRLab).

### `INSTRUMENT_PACKAGE is removed` error
A stale config still sets `INSTRUMENT_PACKAGE` in its `env:` block. Replace it
with `INSTRUMENT_DIR: /path/to/instruments/<name>` (the dir containing
`profile.py`). Likewise, `OBS_NICKEL` and `-p/--profile` no longer exist.

### Stale DEC headers / "FileNotFoundError: astrometry_ref_cat"
The Nickel DEC keyword can freeze at a previous pointing's value (both CRVAL2 and
DEC agree on the wrong coordinate, defeating the translator's fallback).
Pre-flight coordinate validation in `core/pipeline.py:find_bad_coord_exposures()`
catches these by comparing exposure coordinates against the expected target
RA/Dec (5° tolerance, RA wrap-around handled). Automatic under `stips run` (the
YAML has `ra`/`dec`); for standalone `stips science`, pass `--ra` and `--dec`.

A different root cause for a downstream `astrometry_ref_cat`/`panstarrs1_dr2`
`MissingDatasetTypeError` is missing reference catalogs. In `gaia_ps1` mode
`stips run` now **aborts early** if the on-demand refcat ensure fails (e.g. no
network, missing `astroquery`), reporting the root cause — rather than warning
and limping into science where every night dies with an opaque missing-dataset
error. Fix the reported cause, or use `refcat.mode: monster` if the repo already
holds refcats.

### DIA reports success but no difference images
`core/dia.py` checks `diff_image_count` after execution. If the pipeline exits 0
but produces zero difference images (typically because `rewarpTemplate` found no
template overlap), it correctly reports failure. This template-doesn't-overlap
cascade is the most common DIA failure mode.

### PS1 template overlap failures
The PS1 cutout size defaults to 0.2° (`--size`; YAML `template.size`). The Nickel
FOV is ~6.3 arcmin, so increase `template.size` if overlap failures persist. The
ingest cache validates target coverage and file size, so a larger size triggers a
re-download. **Size the cutout to comfortably exceed the camera FOV plus the
dither range** — on a large-FOV camera (e.g. CTIO Y4KCam, ~20 arcmin) the default
leaves dithered pointings with no PSF-matching kernel candidates
(`NoKernelCandidatesError` in DIA). The 2023ixf configs use `template.size: 0.4`
(24 arcmin) for exactly this margin.

### Southern fields have no PS1 coverage
PS1 (Pan-STARRS1) covers dec ≳ −30° only. For a southern target the `gaia_ps1`
refcat mode has no PS1 photometry: astrometry (Gaia DR3) still works, but
photometric calibration needs `refcat.mode: gaia` (Gaia-only) or MONSTER shards
(`refcat.mode: monster`).

For **templates**, southern fields have two options, in order of preference:

1. **`template.type: coadd`** — a CTIO self-coadd from SN-free epochs. This is
   the validated path (NGC2298, Dec −36) and is same-instrument, which avoids
   the cross-instrument registration and PSF-matching problems entirely.
2. **`template.type: skymapper`** — a SkyMapper DR4 external cutout. **Use only
   when no SN-free epochs exist to self-coadd.** Verified limits (2026-07-27):
   - the DR4 SIA serves **single-epoch frames, not stacks** (only 100 s `main`
     frames are usable; 5 s `short` frames are rejected)
   - cutouts are **hard-capped at 0.17° (10.2′)**, smaller than the Y4KCam
     ~20′ FOV — dithered pointings will hit `NoKernelCandidatesError`
   - main-frame seeing is ~1.8–2.3″, i.e. **not sharper than the science**, so
     `subtractImages_skymapper.py` does not assume `convolveTemplate`
   - **SkyMapper `v` is a ~384 nm violet filter, NOT Johnson V (~551 nm).** Only
     `r` and `i` are mapped for Y4KCam; see `template_band_maps` in the profile.

   SkyMapper is **explicit-only** — `template.type: auto` never selects it.

### Coordinate precision for forced photometry
Target RA/Dec must use full TNS precision (sexagesimal → decimal, 6+ decimal
places). Rounding to 2 decimal places in degrees causes 5–17″ offsets — enough to
completely miss a point source on Nickel's 0.37″/pixel scale. Always convert from
TNS sexagesimal (e.g. `14:03:38.580, +54:18:42.10` → `210.910750, 54.311694`)
rather than rounding. Symptom of wrong coordinates: 100% negative forced-photometry
flux (measuring galaxy background instead of the SN).

### PS1 template pixel units
PS1 templates must be pre-calibrated to nJy during ingestion
(`ingest_ps1_template.py` does this). If they stay in raw ADU (~363 nJy/ADU), the
DIA kernel must absorb a ~363× flux ratio on top of PSF matching, causing
numerical instability and unreliable kernel sums. After re-ingesting templates,
rerun existing DIA results.

### Nickel coadd template contamination
Building Nickel coadd templates from epochs where the SN is still active bakes SN
flux into the template, producing negative/underestimated difference flux. Use
PS1 templates for early epochs, or build Nickel templates only from SN-free data.

### Science fallback configs and collection naming
Each fallback `calibrateImage` config writes to its own RUN collection
(`/run_fb1`, `/run_fb2`, ...) under the same CHAINED parent as the primary
`/run`. This is required because Butler enforces config consistency per task label
within a single RUN collection. Downstream steps (DIA, coadd, fphot) should always
use the CHAINED parent (`processCcd/{ts}`), never individual `/run` or `/run_fb*`.

### DRP.yaml config field names
The installed stack uses specific field names that may differ from docs:
- `BestSeeingQuantileSelectVisitsTask`: uses `qMin`/`qMax` (NOT `quantile`).
- `BestSeeingSelectVisitsConfig`: uses `maxPsfFwhm`/`nVisitsMax`.
- `makeDirectWarp` selection: `select.maxEllipResidual`, `select.maxScaledSizeScatter`.

### Instrument fork gotchas
See `docs/forking-stips.md`. Common ones: `night_to_dayobs_offset_days` must be
verified by ingesting a frame (not assumed); disable ISR steps whose curated
calibs you don't ship via `isr_overrides` (e.g. `{"doDefect": False}`); keep
`instrument_class="lsst.obs.stips.active.Instrument"` (the generic synthesized
class, same for every fork).

## File Locations

- CLI entry point: `packages/stips/src/stips/cli.py`
- Core modules: `packages/stips/src/stips/core/`
- Config loader / env contract: `packages/stips/src/stips/core/config.py`
- Collection-name builder: `packages/stips/src/stips/collections.py`
- Dataset-type constants: `packages/stips/src/stips/core/dataset_types.py`
- Instrument profile dataclass: `packages/stips/src/stips/profile.py`
- Reference profile object: `instruments/nickel/profile.py`
- Second instrument profile: `instruments/ctio1m/profile.py`
- Pipeline tools (`stips-*`): `packages/stips/src/stips/pipeline_tools/`
- PS1 ingestion: `packages/stips/src/stips/pipeline_tools/ingest_ps1_template.py`
- Generic LSST glue: `packages/obs_stips/python/lsst/obs/stips/`
- Framework default pipelines: `packages/obs_stips/instrument_defaults/pipelines/` (DRP.yaml, DIA.yaml, ForcedPhotRaDec.yaml, ...)
- Framework default configs: `packages/obs_stips/instrument_defaults/configs/` (dia/, coadds/, neutral colorterms/filter_map; tiering contract in `instrument_defaults/README.md`)
- Nickel-fitted science configs: `instruments/nickel/configs/` (colorterms.py, calibrateImage/tuned_configs/, refcats_gaia_ps1.py)
- Bootstrap shell script: `scripts/pipeline/`
- Target configs: `scripts/config/{target}/`
- Adding a new instrument: `docs/forking-stips.md`
- Stack-upgrade runbook: `docs/stack-bump-runbook.md`
- Architecture / getting-started: `docs/architecture.md`, `docs/getting-started.md`
- Audit reports: `docs/audit/`
