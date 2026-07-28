# Changelog

All notable changes to STIPS (the Small Telescope Image Processing Suite) are documented here.

## [Unreleased]

### Fixed
- **External templates were truncated to a single skymap patch.**
  `ingest_exposure_to_butler()` reprojected the survey cutout onto the one patch
  containing the target coordinate and discarded everything outside it, while
  the self-coadd template path has always ingested every patch the field
  overlaps and let `rewarpTemplate` gather them at DIA time. Same field, same
  tract, i band: the validated CTIO self-coadd covers 4 patches (142, 143, 156,
  157 of tract 444); the external ingest wrote 1 (156). On NGC2298 the assembled
  SkyMapper mosaic spans 17.0′ × 25.5′ but the ingested `template_coadd` kept
  only 10.0′ × 15.3′ of it — the target sits 6.2′, −8.7′ off the centre of patch
  156 — leaving 39.0% science-field coverage and a template edge running through
  the field. The ingest now traces the exposure's sky footprint from its WCS and
  bbox, asks the skymap (`findTractPatchList`) which patches that footprint
  overlaps, and writes one `template_coadd` per patch, skipping any whose
  reprojection carries no usable data (an all-`NO_DATA` template is worse than an
  absent one, since `rewarpTemplate` would still gather it). This is shared
  PS1-inherited code, so **PS1 templates are affected too**: any PS1 cutout wider
  than one patch was being clipped the same way. Re-ingest existing external
  templates and rerun the DIA that used them. `ingest_exposure_to_butler()` now
  returns a list of data IDs (target patch first) and `ExternalTemplateResult`
  gained a `patches` field.
- **`stips external-template` never reported which tract/patch it wrote.**
  `ingest.py` configures `logging` with the default handler, which writes to
  *stderr*, so under `capture_output=True` every `Data ID:` line lands in
  `result.stderr` while `result.stdout` is empty — and the parse only looked at
  stdout. `tract`/`patch` came back `None` on every real run and the CLI silently
  omitted the line. Both streams are scanned now, which is also what surfaces the
  new multi-patch `Patches: [...]` summary.
- **`stips dia` ignored the YAML's `configs.dia.*` overrides.**
  `dia.run()` has accepted `subtract_config_file`/`detect_config_file` since the
  YAML-driven `stips run` path started wiring them from
  `configs.dia.subtract_images`/`detect_and_measure`, but the `stips dia` CLI
  subcommand exposed neither flag and passed neither — so the documented
  `stips dia <night> -b i --template ...` workflow silently fell back to the
  instrument-dir default DIA config instead of the one the YAML specified.
  Caught on a SkyMapper DIA run: the ctio1m default (`mode='convolveTemplate'`,
  `spatialKernelOrder=2`) applied instead of `subtractImages_skymapper.py`'s
  `mode="auto"`, forcing a deconvolution that drove the spatial condition
  number to 2.4e10. `stips dia` now has `--subtract-config`/`--detect-config`
  (resolved instrument-dir-first via the same `config.resolve_config()` as
  `stips run`), falls back to the `-c` YAML's `configs.dia.*` when a flag is
  omitted, and always prints which config file is in effect so this failure
  mode is visible instead of silent.
- **External templates attached a PSF at the wrong pixel scale.**
  `reproject_to_patch()` warped a survey cutout onto the skymap patch grid but
  copied the `GaussianPsf` across unchanged — and `GaussianPsf` stores its width
  in *pixels*, so the attached PSF silently misrepresented the seeing by the
  ratio of the two pixel scales. For a SkyMapper frame (0.4976 ″/px → 0.2887
  ″/px) a real 1.68″ FWHM read as 0.98″, understating the seeing by 1.72×, which
  makes `subtractImages` `mode="auto"` pick the wrong convolution direction. The
  bug predates the SkyMapper work: for PS1 (0.25 ″/px) the factor is 0.87, a 13%
  understatement small enough to have gone unnoticed. The same fix restores the
  `TEMPLATE_*` provenance keys, which reprojection also dropped. **Migration:
  this changes the PSF attached to every PS1 template.** Re-ingest existing
  templates and rerun any DIA that used them. Only an end-to-end ingest
  exercises this code (it needs a real skymap and the stack), so a regression
  test now pins the rescaling.
- **A mistyped `template.type` silently built nothing.** The dispatch was an
  if/elif chain with no `else`, so `template: {type: skymappper}` ingested no
  template and then failed every band in DIA with "no template available" —
  after the run had already spent hours on calibs and science. `RunConfig` now
  rejects an unrecognised value when the YAML is parsed, naming the valid ones.
- **External-survey cutouts were only validated for PS1.** The coverage and
  angular-size checks ran inside PS1's downloader, so an edge-trimmed SkyMapper
  frame — a well-formed FITS that clears the response-size floor but misses the
  target or leaves no DIA overlap margin — was converted and ingested silently,
  surfacing much later as a `NoKernelCandidatesError`. Both checks now run
  post-fetch for every source.
- **ctio1m: `exposure_id`/`observation_id` collided across consecutive nights.**
  CTIO straddles UT midnight and Y4KCam seqnums reset each local night, so the
  UT-day-keyed id mapped night N's post-midnight frames and night N+1's afternoon
  calibs to the same value (real: `36730069` on SA98 20100120/20100121), failing
  Butler exposure-sync on ingest and yielding an empty calib qgraph. Both ids now
  key on the local night parsed from the `y{YYMMDD}.{seq}.fits` filename. This
  changes ingested ids for ctio1m — existing ctio1m repos must be re-ingested.
- **crosstalk:** certification is idempotent; re-certifying a static calib raised
  `ConflictingDefinitionError` and broke every night after the first.
- **ctio1m:** the U+CuSO4 near-UV filter is recognised; nights whose biases sat at
  that wheel slot had zero ingestable biases (real: 20100120).

### Added
- **SkyMapper templates are now mosaicked, lifting the 10.2′ ceiling.** The
  DR4 SIA's 0.17° cap is per REQUEST, not per frame: several offset requests
  against the same `image=` id come back as exact sub-arrays of one CCD pixel
  grid — identical `CRVAL`, identical `CD`, identical `PV` distortion terms,
  differing only in `CRPIX`. `fetch` now issues a small overlapping grid of
  requests against the single selected frame whenever `--size` exceeds the cap
  and pastes the tiles together at integer pixel offsets (`CRPIX_ref −
  CRPIX_tile`), so there is **no reprojection, no resampling, and hence no
  interpolation error, no PSF change and no photometric change** — the assembled
  header keeps the frame's `CRVAL`/`CD`/`PV` and only shifts `CRPIX` to the new
  origin. Tiles that disagree on `CRVAL`/`CD`, or that are offset by a
  fractional pixel, raise `TemplateSourceError` rather than being pasted
  misaligned. This was the binding constraint on the whole SkyMapper path: a
  single 10.2′ cutout covers ~16% of a ~20′ Y4KCam field, which is why 85% of
  every NGC2298 difference image came back flagged `NO_DATA`. Measured on that
  same field at `--size 0.4`: nine tiles assemble to 17.0′ × 25.5′ and the
  ingested `template_coadd` covers **37% of the science patch, up from 14%**.
  The remaining limit is the detector, not the service — a SkyMapper CCD is
  2048 × 4096 px at 0.4976″/px = 17′ × 34′, so a square request wider than 17′
  comes back truncated on the short axis and says so explicitly in the log
  (`max_assembled_deg` records the ceiling). A `--size` at or under 0.17° still
  issues exactly one request and is byte-for-byte unchanged.
- **`stips external-template --source {ps1,skymapper} --ra --dec -b <band>`** —
  one command for every external-survey DIA template, replacing the
  source-specific `stips ps1-template` (retained as a working alias). Adds
  `--mjd-start`/`--mjd-end` so a template frame can be chosen from epochs that
  exclude the transient. `--source` choices, the `template.type` dispatch, and
  DIA's explicit-collection lookup all read the adapter registry, so a new
  survey is one `sources/*.py` file — see `docs/architecture.md`.
- **`template.type: skymapper`** in the run YAML — SkyMapper DR4 as an external
  template source for southern fields (Dec ≲ −30°) with no PS1 coverage. It is
  deliberately **Tier-2 and explicit-only**: `template.type: auto` never selects
  it. DR4 serves single-epoch 100 s frames (5 s frames are rejected outright),
  capped at 0.17° (10.2′) per request — see the mosaicking entry above — at ~2″
  seeing. Validated against
  a CTIO self-coadd on NGC2298: DIA succeeds on every visit but recovers 36% of
  the difference-image sources and leaves an ~8× larger systematic residual, so
  prefer `template.type: coadd` whenever SN-free epochs exist. Full comparison
  in `docs/skymapper-template-validation.md`.
- profile `template_band_maps` — per-source external-template band policy
  (`SOURCE -> (LOCAL band -> that survey's band)`), additive alongside
  `ps1_band_map`, which stays because it also builds the PS1 refcat filterMap
  via `STIPS_PS1_BAND_MAP`. Band names are **not** interchangeable across
  surveys: SkyMapper's `v` is a ~384 nm violet filter, not Johnson V (~551 nm),
  so ctio1m maps only `{"r": "r", "i": "i"}` and excludes `v` rather than
  silently fetching a near-UV template for a green science image.
- profile `fov_arcmin` — approximate science field of view, the input to the
  template-coverage warning (a cutout narrower than the field leaves dithered
  pointings with no PSF-matching kernel candidates). Nickel `6.3`, ctio1m
  `20.0`; unset means no warning.
- `stips.pack_exposure_id(days_since_2000, seqnum)` — the low-level id packer, for
  profiles whose local night does not map 1:1 onto a UT day. `make_exposure_id`
  now delegates to it and is unchanged for callers.
- ctio1m Y4KCam DIA tuning (bleed masking, SAT-excluded detection, spatial kernel)
  and coadd visit-selection/warp configs; SA98 validation pipeline configs.
- refcat: synchronous Gaia TAP fallback for async result-storage outages.

### Changed
- **External-template exposure metadata keys are source-namespaced:**
  `PS1_FILTER`/`PS1_ZEROPOINT` are now `TEMPLATE_SOURCE`/`TEMPLATE_ZEROPOINT`
  (joined by `TEMPLATE_FWHM_ARCSEC`), since one converter now serves every
  survey. Templates ingested before this carry the old keys; anything reading
  them must handle both or re-ingest.
- **The default external-template download directory moved** from
  `<repo>/ps1_templates` to `<repo>/external_templates`. The old directory is
  not read, so the first run after upgrading re-downloads each cutout once;
  delete the stale directory afterwards, or pass `--output-dir`.
- ctio1m pipeline configs use the neutral `calibrateImage` default instead of
  Nickel's fitted `tuned_configs/` (which are fitted for Nickel's CCD and now live
  under `instruments/nickel/configs/`). A Y4KCam-fitted config is future work.

## [2.0.1] — 2026-07-14

### Fixed
- Singularity release publication works end-to-end: fuse build deps on current
  runners, v-less image tag, ORAS publication to `ghcr.io/<owner>/stips-sif`
  (the 3.2 GB .sif exceeds GitHub's 2 GiB release-asset cap), `registry login`
  for Singularity 4.x, and `packages: write` job permissions. A
  `test-singularity` PR label runs the publish end-to-end inside a PR.
- `make test` stack harness: `packages/refcats/src` was missing from
  PYTHONPATH (refcats tests could not import outside with-stack.sh).

### Changed
- Shared exec tooling (Makefile, with-stack.sh, bootstrap, docker images, BPS
  sites) discovers instrument data packages generically under
  `$INSTRUMENT_DIR` (any `ups/`-bearing subdir) instead of hardcoding
  `obs_nickel_data`/`testdata_nickel`; retired stale `obs_nickel` references
  (CI step name, fitter guidance strings, test mock paths).

## [2.0.0] — 2026-07-14

A large documentation-and-correctness audit campaign. The grouped summary below
is at user level; the finding-tagged subsections that follow it keep the detailed
per-area notes.

### Added
- **Generic calibration tooling** as console scripts, so a fork produces its own
  fitted assets instead of copying Nickel's: `stips-defects-build` (defect maps
  from master calibs), `stips-colorterms-fit` (Landolt-fit color terms), and
  `stips-tune-calibrate-image` (searches `calibrateImage` parameters to produce
  `tuned_configs/`). Recipes under `instruments/nickel/{defects,colorterms,tuning}/README.md`.
- **`stips measure-crosstalk`** and declarative `CrosstalkSpec` for multi-amp
  cameras (detailed below).
- **Shared framework modules** a fork builds on: `stips.fetch`
  (`make_fetch_data` wrapper + `parse_night`; a fork's `fetch.py` implements only
  `_fetch_night` + `build_kwargs`), `stips.make_exposure_id` (the reference
  31-bit-safe exposure-id scheme), `stips.testing.instrument_contract` (the
  auto-discovered contract-test harness — see `docs/instrument-contract.md`),
  `core/dataset_types.py` (central Butler dataset-type constants, pinned by a
  contract test), `core/download.py` (download orchestration), `core/query.py`
  (a Butler string-literal sanitizer), and `core/pipeline.PipetaskStage` (shared
  pipetask/butler choreography).
- **Profile `ps1_band_map`** — declares which local science bands are
  PS1-template eligible and the PS1 band each maps to (drives `template.type: auto`).
- **New docs**: `docs/stack-bump-runbook.md`, `docs/instrument-contract.md`,
  `docs/migrations.md`, and `packages/obs_stips/instrument_defaults/README.md`
  (the tiering contract).

### Changed
- **Packaging is framework-only.** `packages/` now holds just `stips`,
  `obs_stips`, and `refcats`; **all** Nickel assets moved under
  `instruments/nickel/` (`obs_nickel_data`, `testdata`, `defects/`,
  `colorterms/`, `tuning/`, and the vendored `lick_searchable_archive`, marked
  with a `VENDORED.md`).
- **`refcats` is now the `stips-refcats` distribution** (import `stips_refcats`);
  the old `nickel_refcats` import remains as a thin re-export shim.
- **`obs_data_package` resolution precedence** clarified: `package_dir` overrides
  the location; otherwise STIPS looks under `<INSTRUMENT_DIR>/<obs_data_package>`
  first, then the reference `packages/<name>` layout.
- **CLI handlers thinned** — they delegate to core modules: `download`
  orchestration lives in `core/download.py`, `clean` is a plan/execute flow,
  lightcurve display options flow only through `LightcurveConfig`, and
  `dashboard` requires and threads the `-c` config.
- **Ops**: the scheduled stack canary runs the pipeline graph-build tests so
  config-field breakage surfaces before the pin moves; CI validates pushes on the
  active development branches. Supported release `v30_0_3`, CI weekly pinned at
  `w_2025_32` (see `docs/stack-bump-runbook.md`).

### Fixed
- **Calibs success is verified against products**, not just the pipetask exit
  code — a run that exits 0 but writes no bias/flat is now reported as a failure
  (or partial), not a success.
- **DIA and forced photometry query both UT `day_obs` values** a local observing
  night can span (pre-/post-midnight), so exposures near UT midnight are no
  longer silently dropped.
- **Coadd template rebuild is build-then-swap** (F-009): a rebuild writes to a
  fresh RUN and the parent chain is repointed only on success, so a failed
  rebuild can't leave a half-built template in place.
- **Provenance records the true LSST *pipelines* (EUPS) version and the profile's
  instrument**, distinct from the conda/rubin-env name.
- **`filter_map.py` covers CTIO 1.0m's uppercase `U`** physical filter
  (previously a live KeyError for analysis tasks on U-band data).

### Deprecated
- The `nickel_refcats` import path — use `stips_refcats`; importing it emits a
  `DeprecationWarning`.
- The `nights: {YYYYMMDD: {band: [...]}}` mapping form in run configs — only its
  night keys are read now; use the `science: nights: [...]` list.

### Migration notes
- **QA task labels renamed `...Nickel` → `...Visit`** (F-013): task labels become
  Butler dataset-type names, so dashboards/queries referencing the old
  `...Nickel_metadata`/`_log`/`_config` names must update. No data loss and
  nothing to migrate on disk. See `docs/migrations.md`.

### E2E validation fixes
End-to-end runs on real Nickel and CTIO/Y4KCam data hardened the venv/stack
boundary and the forking path:
- **Refcat fetch runs from a plain venv.** The HTM cone-coverage math and
  `convertReferenceCatalog` now fall back to in-stack execution automatically, so
  `stips run` with `refcat.mode: gaia_ps1` works without a stack-activated shell
  (previously it only ran from inside the stack).
- **`stips-refcats` declares its fetch dependencies** (`astroquery`, `astropy`,
  `numpy`, `pandas`), so a clean `uv sync --group dev` can fetch Gaia/PS1.
- **A failed refcat ensure aborts `stips run` early** with the root cause, instead
  of warning and limping into science where every night died with an opaque
  `MissingDatasetTypeError('panstarrs1_dr2')`.
- **Instruments with no tuned `calibrateImage` config now run science** on a
  neutral schema-compatibility default (measurement plugins/radii/slots only, no
  instrument tuning) — a fork no longer needs to fit `calibrateImage` before
  processing. An explicitly-configured-but-missing config path still errors (typo
  protection).
- **gaia_ps1 mode now covers the stage-1 QA ref-match tasks** via two neutral
  overlays (`refcats_gaia_ps1_qa_astrom.py`, `refcats_gaia_ps1_qa_photom.py`), so
  fields outside local MONSTER shard coverage no longer fail quantum-graph
  construction.
- **Instrument config overrides no longer import the profile.** The PS1 band map
  reaches the `refcats_gaia_ps1*.py` overlays through the new `STIPS_PS1_BAND_MAP`
  env var (exported by `run_with_stack`), fixing saved-quantum-graph replay, which
  re-imports every module a pex_config file touched during config exec.
- **PS1 templates must exceed the camera FOV plus dither margin.** On a large
  (~20′) FOV like Y4KCam, too small a `template.size` left dithered pointings with
  no PSF-matching kernel candidates (`NoKernelCandidatesError`).
- **Dashboard requires `fastapi>=0.110`** (request-first `TemplateResponse`).

### Defaults tiering: Nickel-fitted science calibration moved out of the framework tier (F-012)
- **Moved to `instruments/nickel/configs/`** (behavior for Nickel unchanged —
  instrument-dir-first resolution finds them there): the Landolt-fit
  `colorterms.py`, all `calibrateImage/tuned_configs/*.py`, and the Nickel-band
  `refcats_gaia_ps1.py`.
- **Neutral framework defaults** in `obs_stips/instrument_defaults/configs/`:
  `colorterms.py` is now an **empty** library; `apply_colorterms.py`,
  `analysisToolsPhotometricCatalogMatchVisit.py`, and `refcats_gaia_ps1.py` are
  instrument-aware — they load the active instrument's `configs/colorterms.py`
  / `configs/filter_map.py` via `$INSTRUMENT_DIR` when present and enable color
  terms **only when the resolved library is non-empty** (an empty library with
  `applyColorTerms=True` fails LSST config validation). The neutral
  `refcats_gaia_ps1.py` derives its PS1 filterMap from the profile's
  `ps1_band_map`.
- `filter_map.py` now covers CTIO 1.0m's uppercase `U` physical filter
  (previously a live KeyError for analysis tasks on U-band data).
- DRP.yaml/dia/coadd/skymap threshold comments relabeled honestly as
  "reference tuning from the Nickel 1-m"; new
  `packages/obs_stips/instrument_defaults/README.md` documents the tiering
  contract (what a fork inherits vs MUST review — photometric calibration!).

### QA task-label rename: `...Nickel` → `...Visit` (F-013)
- Renamed 5 analysis/QA task labels in DRP.yaml /
  analysis-visit-single-visit.yaml / visit-quality-detector.yaml
  (`analyzeCalibrateImageMetadataNickel` → `...MetadataVisit`,
  `*SingleVisitStar{Astrometric,Photometric}RefMatchNickel` → `...RefMatchVisit`).
  Task labels become dataset-type names in every fork's Butler repo.
- **Migration:** no data loss; reruns write under the new label-derived dataset
  names; dashboards/queries referencing the old `..Nickel_metadata/_log/_config`
  names must update. See `docs/migrations.md`.

### Crosstalk for multi-amplifier instruments
- **Declarative crosstalk**: instrument profiles can carry a `CrosstalkSpec`
  (N×N coefficient matrix + units). STIPS builds a `CrosstalkCalib`, certifies it
  into `{prefix}/calib/crosstalk` (chained into the curated calib chain), and
  auto-enables ISR `doCrosstalk` — no forked pipelines.
- **Measurement** (`stips measure-crosstalk <nights…>`): derives coefficients from
  exposures via cp_pipe's `cpCrosstalk` pipeline (reusing the profile's
  `isr_overrides` on the measurement ISR), certifies the result, and exports the
  matrix (ECSV) for inspection. Run once when no coefficients are known.
- **CTIO1m / Y4KCam** ships a **measured** 4×4 matrix (derived with
  `measure-crosstalk` on the E2 standard field, night 20111113; ~0.1–0.4%,
  largest between adjacent quadrants). Re-measure on a denser field to tighten.
- See `docs/crosstalk.md`.

## [1.0.0] — 2026-06-24

### Framework refactor (instrument-neutral)
- Renamed the suite to **STIPS**; split into `stips` (CLI + core) and `obs_stips` (generic LSST glue)
- **Declarative instrument profiles** under `instruments/<name>/` (`profile.py` + camera + hooks), loaded by path via `INSTRUMENT_DIR` — no per-instrument `obs_` package or EUPS product
- Single `-c <config.yaml>` is the sole config source (removed `.env`, `-p/--profile`, and `os.environ` fallbacks)
- Profile-driven collection prefix, skymap name/geometry, filters, header translation, ISR overrides, `boresight_rotation_angle`, and `fetch_data`
- Generic reference pipelines/configs shipped from `obs_stips/instrument_defaults/` with an instrument-dir-first resolver

### CTIO 1.0m / Y4KCam — second instrument
- First **multi-amplifier** camera (4-amp, central-cross overscan); measured per-amp gains; amp-flip + parallel-overscan ISR fixes (seam-free assembly)
- **On-chip binning** support (`CCD_BINNING`): one profile reduces unbinned 4064² and 2×2-binned 2072² raws (imaging scales, overscan fixed)
- **NOIRLab Astro Data Archive** `fetch_data` hook (funpack + integer-`FILTER` normalization)
- Astrometry fix: profile `boresight_rotation_angle=180°` (Y4KCam mounted rotated) — median residual 13.5″ → 0.12″
- Validated end-to-end: unbinned 2007 PG1047 (sub-arcsec astrometry, ~46 mmag photometry) and 2×2-binned 2011 B/V/R/I standard fields (0.578″/px, sub-arcsec V/R/I)

### Extended Objects & Narrowband Filters

- Per-filter narrowband isolation for Halpha, [OIII], g', r' filters
- Per-band-group processing (broadband and narrowband processed separately)
- 12-night extended objects survey configuration (2023B-2025B)
- 9 supported filters: B, V, R, I, g', r', Halpha, [OIII], clear

## [0.1.0] — 2026-03-03

### Exoplanet Transit Detection
- First exoplanet transit detection with the Nickel 1-meter telescope
- HD 189733 b detected at 13-sigma from 400 B-band exposures (4s cadence)
- LSST-native `DifferentialPhotTask` for ensemble differential aperture photometry
- BLS transit search module with configurable period/duration grids

### Variable Star Period Recovery
- Lomb-Scargle period analysis module for pulsating variables
- CY Aqr, DY Peg, AC And periods recovered from single-night V-band observations
- Example variable star campaign templates

### BPS / HPC Integration
- Full pipeline validated end-to-end through BPS, Parsl, and Slurm
- Docker Slurm test cluster (AlmaLinux 9, Slurm 22.05)
- Singularity `.def` for HPC deployment
- Conditional `--qgraph-datastore-records` for BPS vs. local execution

### Supernova Lightcurves
- SN 2023ixf (Type IIP): 22-night monitoring campaign, classic plateau lightcurve
- SN 2020wnt (SLSN-I): multi-epoch detections at z=0.032
- PS1 and Nickel coadd template strategies for DIA
- Configurable lightcurve display: apparent/absolute mag, flux, days-since-explosion

### Pipeline Architecture
- YAML-driven full pipeline orchestration (`nickel run`)
- Four-tier calibrateImage fallback chain (99.4% science processing success)
- Per-band DIA and forced photometry for partial-failure resilience
- Degenerate WCS detection and exclusion for coadd templates
- FastAPI real-time monitoring dashboard

### Infrastructure
- `nickel` CLI with 16 commands covering the full pipeline lifecycle
- Profile-based configuration system for multi-target campaigns
- CI with LSST Science Pipelines container, pre-commit, and ruff/black
- Docker images published to GHCR (`stips`, `stips-slurm`, `stips-hpc`)

## [0.0.1] — 2025-06-08

- Initial commit: obs_nickel instrument package (camera geometry, translator, ISR)
- NickelTranslator for FITS header metadata extraction
- Single-CCD detector layout, visit_system ONE_TO_ONE
- Basic test suite for instrument registration and raw ingestion
