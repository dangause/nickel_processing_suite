# Architecture Overview

This document describes the architecture of STIPS — the Small Telescope Image Processing Suite. STIPS is a generic framework that brings the LSST Science Pipelines to 1-meter class telescopes, plus a per-telescope instrument *profile*. The Nickel 1-m at Lick is the reference instrument; a fork supports another telescope by copying the declarative `instruments/nickel/` directory and editing its profile — no new code package is needed.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        User Interface Layer                          │
│  ┌──────────────┐  ┌──────────────────────────────────────────┐    │
│  │  stips CLI   │  │  Config YAML  (-c: env: block + pipeline) │    │
│  └──────────────┘  └──────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Orchestration Layer                             │
│  ┌──────────────┐  ┌──────────────────┐  ┌────────────────────┐    │
│  │   run.py     │  │     bps.py       │  │ processing_log.py  │    │
│  │  (YAML run)  │  │  (HPC submit)    │  │ (failure tracking) │    │
│  └──────────────┘  └──────────────────┘  └────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Core Processing Modules                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │bootstrap │ │ calibs   │ │ science  │ │   dia    │ │  fphot   │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│  ┌──────────┐ ┌──────────┐                                          │
│  │lightcurve│ │ps1_templ │                                          │
│  └──────────┘ └──────────┘                                          │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Infrastructure Layer                            │
│  ┌──────────────┐  ┌──────────────────┐  ┌────────────────────┐    │
│  │  config.py   │  │    stack.py      │  │   pipeline.py      │    │
│  │ (env loading)│  │ (LSST activation)│  │ (collections,      │    │
│  │              │  │                  │  │  coord validation)  │    │
│  └──────────────┘  └──────────────────┘  └────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        External Systems                              │
│  ┌──────────────┐  ┌──────────────────┐  ┌────────────────────┐    │
│  │   Butler     │  │ LSST Pipelines   │  │    HPC Cluster     │    │
│  │ Repository   │  │ (pipetask, etc.) │  │ (Slurm/HTCondor)   │    │
│  └──────────────┘  └──────────────────┘  └────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

## Package Structure

STIPS is a monorepo organized around a two-package framework split — the CLI/tooling (`stips`) and the instrument-agnostic LSST glue (`obs_stips`) — plus a curated data package (`obs_nickel_data`). A telescope is **not** a code package: it is a declarative `instruments/<name>/` directory (a `profile.py` alongside `camera/`, `configs/`, and `pipelines/`). `instruments/nickel/` is the reference. The active instrument is chosen via the `INSTRUMENT_DIR` environment variable (e.g. `/path/to/instruments/nickel`); the tooling loads `<dir>/profile.py` by path and drives all collection names, Butler queries, and skymap behavior from it. `obs_stips` synthesizes the concrete LSST instrument/translator/formatter from that profile at import time, and Butler registers it under the fixed class name `lsst.obs.stips.active.Instrument`.

### 1. stips (Framework Core, CLI & Tooling)

The main Python package. It depends on the profile *types* exposed by `stips` itself (`InstrumentProfile`, `Site`, `Field`, `@hook`, `collections.CollectionNames`) and imports the active instrument's `profile` module at runtime:

```
packages/stips/src/stips/
├── cli.py                 # `stips` CLI entry point (console script: stips)
├── profile.py             # InstrumentProfile / Site / Field / @hook framework types
├── collections.py         # CollectionNames (driven by the active profile)
├── core/
│   ├── config.py          # Environment/config loading
│   ├── stack.py           # LSST stack activation
│   ├── pipeline.py        # Collection naming + coordinate validation
│   ├── bootstrap.py       # Repository initialization
│   ├── calibs.py          # Nightly calibrations
│   ├── science.py         # Science processing
│   ├── dia.py             # Difference imaging
│   ├── fphot.py           # Forced photometry
│   ├── lightcurve.py      # Light curve extraction
│   ├── ps1_template.py    # PS1 template ingestion
│   ├── run.py             # YAML pipeline orchestrator
│   ├── bps.py             # HPC batch submission
│   └── processing_log.py  # Failure tracking
├── pipeline_tools/        # stips-* console scripts (archive, dia, ...)
├── eda/                   # Butler/archive inspection tools
├── skymap/                # Skymap construction tools
└── dashboard/             # Browser monitoring (optional `stips[dashboard]` extra)
```

### 2. obs_stips (Instrument-Agnostic LSST Glue)

The generic LSST middleware layer (`lsst.obs.stips`). It provides the base instrument/translator/formatter and *synthesizes* a concrete, registerable instrument from the active profile, plus the shared PipelineTasks every instrument reuses:

```
obs_stips/
└── python/lsst/obs/stips/
    ├── instrument.py       # Generic StipsInstrument base
    ├── translator.py       # Generic StipsTranslator base
    ├── formatter.py        # Generic StipsRawFormatter base
    ├── profile_loader.py   # Load instruments/<name>/profile.py by path (INSTRUMENT_DIR)
    ├── active.py           # Synthesizes lsst.obs.stips.active.Instrument from the profile
    ├── plotting.py         # Shared plotting helpers
    └── tasks/              # Shared PipelineTasks (lsst.obs.stips.tasks.*)
        ├── forcedPhotRaDec.py
        ├── differentialPhot.py
        └── diaLightcurve*.py
```

`active.py` reads `INSTRUMENT_DIR`, loads the profile by path, and binds it onto the generic base classes to produce `Instrument` / `Translator` / `RawFormatter`. Butler stores the FQN `lsst.obs.stips.active.Instrument` and re-imports it whenever it needs to re-instantiate the instrument — each import re-resolves the profile from `INSTRUMENT_DIR`, so there is no per-telescope instrument class or package to register.

### 3. instruments/nickel (Reference Instrument)

The Nickel telescope is a declarative directory — no code package, no `lsst.obs.nickel`. It is a `profile.py` plus the camera geometry, pipelines, and configs that the generic `obs_stips` machinery loads by path (`INSTRUMENT_DIR`):

```
instruments/nickel/
├── profile.py             # InstrumentProfile + @hook quirks (the profile)
├── fetch.py               # Optional co-located hook (raw-data fetch)
├── camera/
│   └── nickel.yaml        # Camera geometry (1024×1024 CCD)
├── configs/               # Instrument-FITTED science calibration (colorterms.py,
│                          #   calibrateImage/tuned_configs/, refcats_gaia_ps1.py)
├── obs_nickel_data/       # Curated calibrations (defect maps) — EUPS data package
├── template_metadata.json # Coadd-template bookkeeping
├── README.md
└── tests/                 # Reference translation/camera golden tests
```

Nickel ships **no** `pipelines/` dir — it inherits the framework reference
pipelines from `packages/obs_stips/instrument_defaults/`. It *does* ship a
`configs/` dir, but only for its **instrument-fitted** photometric calibration
(Landolt color terms, `calibrateImage` tunings, the Nickel-band PS1 overlay),
which is deliberately excluded from the neutral framework tier. Everything else
(DIA/coadd/skymap tunings, the neutral empty colorterms) is inherited. A fork
adds its own `instruments/<x>/pipelines/` or `configs/` only to override
individual files (resolved instrument-dir-first, else framework default); see
`packages/obs_stips/instrument_defaults/README.md` for the tiering contract.

There are no instrument/translator/formatter subclasses here — `obs_stips` synthesizes those from `profile.py` at import time (see `active.py` above). The instrument and translator quirks that used to live in a `lsst.obs.nickel` package are expressed declaratively via the `InstrumentProfile` fields and `@hook`s in `profile.py`.

### 4. obs_nickel_data (Curated Calibrations)

A standalone EUPS data package co-located under the instrument tree at `instruments/nickel/obs_nickel_data` (`setup -r instruments/nickel/obs_nickel_data obs_nickel_data`), resolved at runtime via the profile's `obs_data_package` field. Pre-built calibration products following LSST conventions:

```
obs_nickel_data/
├── Nickel/
│   └── defects/
│       └── ccd0/
│           └── 19700101T000000.ecsv  # Defect mask
└── python/lsst/obs/nickel_data/
    └── __init__.py
```

## Data Flow

### Complete Pipeline Flow

```
Raw FITS Files
     │
     ▼
┌─────────────────┐
│   Bootstrap     │ ──► Butler repo created
│   (one-time)    │     Instrument registered
└─────────────────┘     Refcats ingested
     │
     ▼
┌─────────────────┐
│   Calibrations  │ ──► Bias frames
│   (per night)   │     Flat fields
└─────────────────┘     Defect masks certified
     │
     ▼
┌─────────────────┐
│    Science      │ ──► ISR-corrected images
│   Processing    │     WCS solutions
│   (per night)   │     Photometric calibration
└─────────────────┘     Source catalogs
     │
     ▼
┌─────────────────┐
│    Template     │ ──► PS1 cutouts or
│   Preparation   │     Nickel coadd stacks
└─────────────────┘
     │
     ▼
┌─────────────────┐
│   Difference    │ ──► Difference images
│    Imaging      │     DIA source catalogs
│ (per night/band)│
└─────────────────┘
     │
     ▼
┌─────────────────┐
│    Forced       │ ──► Flux measurements
│   Photometry    │     at target coords
└─────────────────┘
     │
     ▼
┌─────────────────┐
│   Light Curve   │ ──► CSV files
│   Extraction    │     Plots
└─────────────────┘
```

## Key Design Patterns

### 1. Result Objects

Each core module returns a dataclass with status and outputs:

```python
@dataclass
class ScienceResult:
    success: bool
    error: str | None = None
    science_run: str | None = None
    coadd_run: str | None = None
```

### 2. Stack Wrapping

All LSST commands run through `stack.py`:

```python
def run_with_stack(cmd: list[str], config: Config, **kwargs) -> subprocess.CompletedProcess:
    """Execute command with LSST stack activated."""
    # Sources loadLSST.bash
    # Sets up lsst_distrib and obs_stips; exports INSTRUMENT_DIR,
    #   STIPS_DEFAULTS, and STIPS_PS1_BAND_MAP (profile ps1_band_map as JSON,
    #   read by in-stack pex_config files that must not import the profile)
    # Exports config as environment variables
    # Runs the command
```

### 3. Collection Naming & Coordinate Validation

`stips.collections` builds consistent collection names, parameterized by the active
instrument's `collection_prefix` (a fork's collections become `<prefix>/...`); `core/pipeline.py`
re-exports `CollectionNames` and provides the pre-flight coordinate/data-quality checks:

```python
class CollectionNames:
    # prefix = config.profile.collection_prefix ("Nickel" for the reference instrument)
    def __init__(self, night: str, run_ts: str | None = None, *, prefix: str):
        self.raw_run = f"{prefix}/raw/{night}/{run_ts}"
        self.calib_chain = f"{prefix}/calib/current"
        self.science_parent = f"{prefix}/runs/{night}/processCcd/{run_ts}"  # CHAINED
        self.science_run = f"{self.science_parent}/run"                     # Primary RUN
        # Fallback RUNs: {science_parent}/run_fb1, run_fb2, etc.
        self.diff_run = f"{prefix}/runs/{night}/diff/{run_ts}/run"
        # ...

def find_bad_coord_exposures(config, night, target_ra, target_dec, ...):
    """Query Butler for exposures with coordinates far from the target.
    Returns exposure IDs to exclude from processing."""
```

Downstream modules (DIA, coadd, fphot) use the CHAINED parent (`science_parent`) rather than `science_run` to include results from both the primary config and any fallback configs.

The coordinate validation catches the Nickel telescope's known issue where DEC headers can freeze at a previous pointing's value, which would otherwise crash the qgraph builder due to missing refcat coverage.

### 4. Processing Logs

`processing_log.py` tracks fallback attempts:

```python
@dataclass
class ProcessingLog:
    night: str
    step: str
    timestamp: str
    configs_tried: list[ConfigAttempt]
    final_status: str
    output_collection: str | None
```

### 5. YAML-Driven Orchestration

`run.py` parses YAML and calls other modules:

```python
def run(config_file: Path, config: Config, dry_run: bool) -> RunResult:
    yaml_config = load_yaml(config_file)

    # Step 0: Bootstrap if needed
    if bootstrap.needs_bootstrap(config):
        bootstrap.run(config)

    # Step 1: Templates
    for band in yaml_config['bands']:
        ps1_template.run(band=band, ...)

    # Step 2-4: Per night
    for night in yaml_config['nights']:
        calibs.run(night, config)
        science.run(night, config, ...)
        dia.run(night, config, ...)
        fphot.run(night, config, ...)

    # Step 5: Light curve
    lightcurve.run(...)
```

### 6. External-Template Source Framework

DIA templates can come from an external sky survey (PS1, SkyMapper) instead of
a same-instrument coadd. The framework splits into three layers so that
survey-specific fetch logic stays out of the LSST-dependent conversion code,
and both stay out of the CLI/orchestration layer:

```
packages/stips/src/stips/pipeline_tools/external_template/
├── imaging.py            # astropy-only helpers; importable in the plain venv
├── core.py               # the ONE module here allowed to import lsst at
│                          #   module scope — executes only inside the stack,
│                          #   via ingest.py
├── ingest.py              # in-stack CLI entry: --source <name> --ra --dec ...
└── sources/
    ├── base.py            # the TemplateSource protocol
    ├── ps1.py             # PS1 adapter
    └── skymapper.py       # SkyMapper adapter
```

- **`imaging.py`** — zeropoint-to-calibration-mean conversion, cutout-size
  clamping. No `lsst` import; used by adapters and unit tests alike.
- **`core.py`** — the FITS → LSST `ExposureF` conversion, WCS handling
  (`makeSkyWcs`), reprojection onto the skymap, and Butler ingest. This is the
  one module in the package that imports `lsst` at module scope, and it only
  ever runs inside the activated stack (invoked via `ingest.py`, mirroring the
  `run_with_stack()` / snippet-returns-JSON pattern used elsewhere in STIPS).
- **`sources/`** — one thin adapter per survey. **Adapters must not import
  `lsst`** — an AST scan (`test_adapters_do_not_import_lsst` in
  `packages/stips/tests/test_skymapper_source.py`) enforces this at the source
  level (not just at import time), so a function-scoped `lsst` import inside an
  adapter fails the test suite too.

Each adapter implements the `TemplateSource` protocol
(`sources/base.py`):

| Member | Purpose | PS1 | SkyMapper |
|---|---|---|---|
| `name` | Registry key and collection namespace (`templates/<name>/<band>`) | `"ps1"` | `"skymapper"` |
| `max_cutout_deg` | Hard service limit on cutout size, or `None` if unlimited | `None` | `0.17` |
| `zeropoint_keywords` | FITS header cards to try, in order, for the zeropoint | `["ZPT", "FPA.ZP", "MAGZERO", "MAGZPT"]` | `["ZPAPPROX"]` |
| `band_map(config)` | LOCAL science band → this survey's band name | `profile.ps1_band_map` | `profile.template_band_maps["skymapper"]` |
| `default_zeropoint(header)` | Fallback AB zeropoint when no header card is present | per-filter table | per `image_type` (`main` ≈ 28.75, `short` ≈ 25.4) |
| `native_fwhm(header)` | Seeing FWHM in arcsec for this specific frame | constant 1.2″ | reads `QAFWHM` from the frame |
| `fetch(ra, dec, src_band, size_deg, out_dir, **kw)` | Download a cutout; return its `Path` | MAST → fitscut → ps1filenames (3 fallbacks) | SIA `query` → frame selection → `get_image` |

`fetch` **raises** `TemplateSourceError` rather than returning `None` on
failure — a falsy return is how a "no usable frames" condition would otherwise
degrade into an opaque downstream Butler error instead of an actionable
message.

**Adding a new survey** (e.g. a future DECam Legacy Surveys / DES adapter):

1. Implement the `TemplateSource` protocol in a new `sources/<name>.py` (no
   `lsst` imports).
2. Register it in `sources/__init__.py`'s `SOURCES` dict.
3. Add a `template_band_maps["<name>"]` entry to any instrument profile that
   should offer it (see `docs/forking-stips.md`).

Nothing else changes — `core.py`, `ingest.py`, the `stips external-template`
CLI, and `dia.find_template()`'s explicit-collection lookup are all
source-agnostic.

**SkyMapper is deliberately Tier-2 and explicit-only.** Its DR4 cutouts are
single-epoch (100 s `main` frames only; 5 s `short` frames rejected), capped at
0.17° (10.2′, smaller than the Y4KCam ~20′ FOV), and not reliably sharper than
the science (`subtractImages_skymapper.py` uses `mode = "auto"` rather than the
PS1 config's hardcoded `convolveTemplate`). `template.type: auto` never selects
it — a fork must set `template.type: skymapper` explicitly, and only after
confirming no SN-free epochs exist for a same-instrument coadd. See the
"Southern fields have no PS1 coverage" gotcha in `CLAUDE.md` for the full
verified-limits list.

## Butler Collection Structure

```
Nickel/
├── raw/YYYYMMDD/timestamp/        # RUN: Ingested raw data
├── calib/
│   ├── current                    # CHAIN: Points to latest calibs
│   ├── curated                    # CHAIN: Camera + defects
│   ├── YYYYMMDD/                  # RUN: Certified calibs
│   └── cp/YYYYMMDD/bias/          # RUN: Constructed calibs
├── runs/YYYYMMDD/
│   ├── processCcd/timestamp/      # CHAIN: Unified science (primary + fallbacks)
│   │   ├── run                    # RUN: Primary config outputs
│   │   ├── run_fb1                # RUN: Fallback 1 outputs (if used)
│   │   └── run_fb2                # RUN: Fallback 2 outputs (if used)
│   ├── diff/timestamp/run         # RUN: DIA outputs
│   └── forcedPhotRaDec/.../run    # RUN: Forced phot outputs
├── templates/
│   ├── ps1/{band}                 # RUN: External templates
│   └── deep/tract{N}/{band}       # RUN: Nickel coadds
└── refcats                        # CHAIN: Reference catalogs
```

The `processCcd/timestamp/` CHAINED collection is what downstream steps (DIA, coadd, fphot) use as input. It chains together the primary RUN and any fallback RUNs, providing a unified view of all successfully processed quanta.

## Extension Points

### Adding New Pipeline Steps

1. Create module in `core/`:
   ```python
   # core/my_step.py
   @dataclass
   class MyStepResult:
       success: bool
       error: str | None = None

   def run(night: str, config: Config, **options) -> MyStepResult:
       ...
   ```

2. Add CLI command in `cli.py`:
   ```python
   @cli.command()
   def my_step(ctx, night, ...):
       result = my_step_module.run(night, ...)
   ```

3. Integrate in `run.py` if needed

### Adding New Instruments

Copy the reference `instruments/nickel/` directory to `instruments/<instrument>/` and edit it — no new code package is required:
1. Edit `profile.py` (an `InstrumentProfile` plus any `@hook` quirks)
2. Replace `camera/<instrument>.yaml` with your camera geometry
3. Add any instrument-specific pipeline configs under `pipelines/` and `configs/`
4. Point `INSTRUMENT_DIR` at the new directory (via the config `env:` block) so the tooling — and `obs_stips`'s synthesis — load the new profile

The instrument/translator/formatter are synthesized from the profile by `obs_stips`, so there is nothing to subclass or register. The shared PipelineTasks in `obs_stips` and the `stips` tooling work unchanged. See the [forking guide](forking-stips.md) for the full walkthrough.

## Dependencies

The framework is two packages plus declarative instrument directories. `stips` (CLI/tooling) defines the profile types and loads the active instrument's `profile.py` by path at runtime. `obs_stips` is the generic LSST glue, and it *synthesizes* the registerable instrument from that profile. A telescope is a declarative `instruments/<name>/` directory (the reference is `instruments/nickel/`), selected via `INSTRUMENT_DIR`; a fork is just another such directory.

```
                      ┌──────────────────────────────────┐
                      │              stips               │
                      │  CLI · tooling · profile types   │
                      │  (InstrumentProfile, @hook,      │
                      │   CollectionNames)               │
                      └──────────────────────────────────┘
                       │ defines profile API      │ loads profile.py
                       │ (imported by profiles)   │ by path at runtime
                       ▼                          ▼ via INSTRUMENT_DIR
   ┌───────────────────────────┐      ┌──────────────────────────────────────┐
   │   instruments/nickel/     │      │   instruments/<name>/   (a fork)      │
   │  reference instrument     │ ...  │  another telescope's directory        │
   │  profile.py + camera/     │      │  profile.py + camera/                 │
   │  + configs/ + pipelines/  │      │  + configs/ + pipelines/              │
   └───────────────────────────┘      └──────────────────────────────────────┘
                       │  each profile loaded by   │
                       ▼   & synthesized in ───────▼
                      ┌──────────────────────────────────┐
                      │             obs_stips            │
                      │  generic LSST glue + synthesis   │
                      │  (instrument, translator,        │
                      │  formatter, active.Instrument;   │
                      │  PipelineTasks: lsst.obs.stips.*)│
                      └──────────────────────────────────┘
                                       │
                                       ▼
                      ┌──────────────────────────────────┐
                      │       LSST Science Pipelines      │
                      │  daf.butler · obs.base ·          │
                      │  pipe.tasks · ip.diffim · ...     │
                      └──────────────────────────────────┘
```

## See Also

- [Pipeline Flow Diagram](diagrams/pipeline-flow.mmd)
- [CLI Commands Diagram](diagrams/cli-commands.mmd)
- [Butler Collections Diagram](diagrams/butler-collections.mmd)
