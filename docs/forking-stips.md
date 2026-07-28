# Forking STIPS for a New Telescope

STIPS runs the LSST Science Pipelines on small (1-meter, single-CCD) telescopes.
The design splits cleanly into a **generic framework** and a per-telescope
**declarative instrument definition**. To adopt STIPS for your telescope you fork
the repo and add **one directory** — `instruments/<your_instrument>/` — that
describes your hardware and header conventions. **You write no LSST Python: no
instrument class, no translator/formatter bindings, no `pyproject.toml`, no EUPS
table.** The generic `obs_stips` package synthesizes all of that from your
profile at runtime.

This guide walks through that fork end to end, using the reference instrument
`instruments/nickel/` (Lick Observatory's Nickel 1-m) as the worked example.

---

## 1. Overview: framework vs. fork

**The framework — you do NOT touch these:**

| Package | Import root | What it provides |
|---------|-------------|------------------|
| `stips` | `stips` | The `stips` CLI + tooling. Exposes `stips.InstrumentProfile`, `stips.Site`, `stips.Field`, `stips.hook`. Loads the active instrument's `profile.py` **by path** from `INSTRUMENT_DIR`, and drives all collection names / Butler queries / skymap from the profile. |
| `obs_stips` | `lsst.obs.stips` | Generic LSST glue. The `lsst.obs.stips.active` submodule **synthesizes** a concrete, registerable instrument + translator + raw formatter from your profile (Butler registers `lsst.obs.stips.active.Instrument` — the same class for every fork). Also ships the shared/generic pipeline tasks (`StipsCalibCombineTask`, `lsst.obs.stips.tasks.*`). |

**The fork — you DO write this (data, not code):**

| Location | What it is |
|----------|-----------|
| `instruments/<x>/` | Your telescope definition: `profile.py` (one `InstrumentProfile(...)` + a few `@hook`s), a camera geometry YAML, and instrument-tuned pipelines/configs. **Loaded by path** — it is *not* an importable Python package. |

**Honest scope.** STIPS targets **1-meter, single-CCD** telescopes. The generic
translator handles the common FITS conventions; your telescope's header quirks
are absorbed by a handful of `@hook` functions in `profile.py`. Nickel needed
about seven hooks (filter fallback, observation typing, a coordinate-header bug,
an exposure-ID scheme, temperature units, and datetime/day_obs derivation). Plan
for a similar order of magnitude.

---

## 2. Step 1 — Fork & branch

```bash
# Fork the repo on your host (GitHub fork, or a clone you control), then:
git clone <your-fork-url> stips
cd stips
git checkout -b feature/instrument-<x>
```

---

## 3. Step 2 — Create your instrument directory

Copy `instruments/nickel/` as your template. There is no package to scaffold,
nothing to rename in Python, no metadata files to edit — it is pure data:

```bash
cp -r instruments/nickel instruments/<x>
rm -rf instruments/<x>/tests   # optional: keep & adapt, or start fresh

# Nickel's configs/ hold Nickel-FITTED photometric calibration (§6) — they must
# NOT carry over to another telescope. Clear them and start from the neutral
# framework defaults:
rm -rf instruments/<x>/configs instruments/<x>/obs_nickel_data \
       instruments/<x>/{colorterms,tuning,defects,testdata,vendor}
```

Resulting layout (of a minimal fork):

```
instruments/<x>/
├── profile.py                 # THE file you edit — your InstrumentProfile + @hooks
├── camera/
│   └── <x>.yaml               # LSST yamlCamera geometry (detectors, plate scale)
├── fetch.py                   # OPTIONAL: a data-fetch hook (delete if you place raws by hand)
├── template_metadata.json     # OPTIONAL: coadd-template bookkeeping
├── configs/                   # OPTIONAL: your instrument-fitted calibration (see §6) — add once you have it
├── README.md
└── tests/                     # OPTIONAL: reference tests, adapt to your golden values
```

`nickel` deliberately ships **no** `pipelines/` dir — it inherits every reference
pipeline from `packages/obs_stips/instrument_defaults/`. It *does* ship a
`configs/` dir, but only for its **instrument-fitted** photometric calibration
(Landolt color terms, `calibrateImage/tuned_configs/`, the Nickel-band PS1
overlay) — which is exactly what a fork must **not** inherit (§6). So a fresh fork
starts with **no** `pipelines/` and **no** `configs/`, inheriting the neutral
framework defaults, and adds its own `instruments/<x>/pipelines/` or `configs/`
only to override individual files.

That's the whole fork. No `python/lsst/obs/<x>/`, no bindings, no
`pyproject.toml`, no `ups/` table — `obs_stips` builds the LSST instrument
machinery from `profile.py` at runtime.

---

## 4. Step 3 — Write `profile.py` (the heart)

`profile.py` builds one `stips.InstrumentProfile(...)` object plus a few
`@hook(profile)` functions, and assigns it to a module-level `profile`. The
framework loads this file **by path** (so the imports below resolve against the
installed `stips`). The real Nickel constructor call looks like this:

```python
from stips import Field, InstrumentProfile, Site, hook

profile = InstrumentProfile(
    name="Nickel",
    policy_name="Nickel",                 # defaults to name if omitted
    site=Site(
        latitude=37.343333,
        longitude=-121.636667,
        elevation=1290.0,
        name="Lick Observatory",          # if set, uses EarthLocation.of_site(name)
    ),
    # physical_filter -> band
    filters={
        "B": "b", "V": "v", "R": "r", "I": "i",
        "clear": None, "gp": "gp", "rp": "rp",
        "Halpha": "halpha", "OIII": "oiii",
    },
    # raw FITS filter value (upper-cased on lookup) -> physical_filter
    filter_aliases={
        "B": "B", "V": "V", "R": "R", "I": "I",
        "OPEN": "clear", "C": "clear", "CLEAR": "clear",
        "GP": "gp", "G'": "gp", "RP": "rp", "R'": "rp",
        "HALPHA": "Halpha", "H-ALPHA": "Halpha", "6563/100": "Halpha",
        "OIII": "OIII", "[OIII]": "OIII", "5000/100": "OIII",
    },
    filter_key="FILTNAM",                 # FITS keyword holding the raw filter name
    header_map={
        "exposure_time":      Field("EXPTIME", unit="s", default=0.0),
        "dark_time":          Field("EXPTIME", unit="s", default=0.0),
        "boresight_airmass":  Field("AIRMASS", default=float("nan")),
        "object":             Field("OBJECT", default="UNKNOWN"),
        "science_program":    Field("PROGRAM", default="unknown"),
        "relative_humidity":  Field("HUMIDITY", default=0.0),
        "telescope":          Field("TELESCOP", default="Nickel 1m"),
    },
    const_map={"boresight_rotation_angle": 0.0, "boresight_rotation_coord": "sky"},
    camera="camera/nickel.yaml",
    instrument_class="lsst.obs.stips.active.Instrument",   # see note — same for every fork
    night_to_dayobs_offset_days=1,
    skymap_name="nickelRings-v1",
    skymap_collection="skymaps/nickelRings",
    obs_data_package="obs_nickel_data",
)
```

### Field-by-field

These are the real `InstrumentProfile` fields (from
`packages/stips/src/stips/profile.py`). Required fields have no default.

- **`name`** *(required)* — Instrument name, e.g. `"Nickel"`. Used for
  registration and as the default `collection_prefix`/`policy_name`.
- **`site`** *(required)* — A `stips.Site(latitude, longitude, elevation, name=None)`.
  If `name` is set, the translator resolves the location via
  `EarthLocation.of_site(name)` and the lat/lon/elev are informational
  fallbacks. If your observatory is not in astropy's site registry, leave
  `name=None` and the geodetic lat/lon/elev are used.
- **`filters`** *(required)* — `physical_filter -> band` map. This is the
  canonical filter registry that drives the LSST `FilterDefinitionCollection`.
  A band of `None` (Nickel's `"clear"`) means "no canonical band".
- **`filter_aliases`** — `raw FITS value -> physical_filter`. Lookup is
  case-insensitive. Map every spelling your headers actually emit (Nickel maps
  `OPEN`/`C`/`CLEAR` → `clear`, `G'` → `gp`, `6563/100` → `Halpha`, etc.).
- **`filter_key`** — FITS keyword holding the raw filter name. Default
  `"FILTNAM"`; Nickel uses the default.
- **`instrument_header_value`** — Substring matched (case-insensitive) against
  the FITS `INSTRUME` header to decide whether your translator handles a file.
  Defaults to `name`. Set it when the instrument **name differs from the camera
  in `INSTRUME`** — e.g. the CTIO 1.0m profile is `name="CTIO1m"` but its raws
  carry `INSTRUME="Y4KCam"`, so it sets `instrument_header_value="Y4KCam"`.
- **`header_map`** *(required)* — `metadata field -> stips.Field(key, unit=None,
  default=None)`. Each `Field` maps an LSST metadata slot to a FITS keyword,
  with an astropy unit name (e.g. `"s"`) and a default for missing keys. Map the
  fields your telescope's headers provide directly; anything that needs logic
  goes in a `@hook` instead.
- **`const_map`** — Constants for metadata that your headers don't carry (Nickel
  has no rotator, so `boresight_rotation_angle: 0.0`).
- **`camera`** *(required)* — Path to your camera geometry YAML **relative to
  `instruments/<x>/`**, e.g. `"camera/<x>.yaml"`. Loaded from `INSTRUMENT_DIR`.
- **`instrument_class`** — The fully-qualified instrument class
  `butler register-instrument` uses. **Keep the Nickel value
  `"lsst.obs.stips.active.Instrument"` unchanged** — this is the generic class
  `obs_stips` synthesizes from your profile; it is the *same string for every
  instrument*. You do not write or name an instrument class.
- **`night_to_dayobs_offset_days`** — Days to add to a local observing night to
  get its UTC `day_obs` (Nickel and CTIO: `1`, since evening obs at western
  longitudes roll into the next UTC day; an instrument that observes entirely
  before UTC midnight uses `0`). **The Butler `day_obs` dimension is the UT
  calendar day** (from `astro_metadata_translator.to_observing_day`); this offset
  is how STIPS maps the human-readable local night you pass on the CLI to the UT
  `day_obs` it queries. Pick the value by ingesting one frame and comparing the
  stored `day_obs` to the local night — do **not** assume `0`. (See the day_obs
  gotcha in §9.)
- **`collection_prefix`** — Butler collection prefix. Defaults to `name` if
  omitted, so Nickel collections begin with `Nickel/...`.
- **`skymap_name` / `skymap_collection`** — Skymap registry name and its
  collection (`"nickelRings-v1"`, `"skymaps/nickelRings"`). Bootstrap registers
  and chains the skymap under **these** names (profile-driven). The skymap
  *geometry* comes from `configs/makeSkyMap.py`, resolved instrument-dir-first:
  drop your own `instruments/<x>/configs/makeSkyMap.py` (set `config.name` to
  match `skymap_name`, and your native `pixelScale`) to get tract/patch geometry
  at your plate scale; otherwise the framework reference geometry is used. A
  distinct geometry also gets a distinct skymap hash, so each instrument
  registers cleanly under its own name.
- **`isr_overrides`** — Dict of ISR config overrides applied to the `isr` task at
  science qgraph-build time (as `pipetask -c isr:<key>=<value>`). Use it to
  toggle ISR steps whose curated calibs your instrument does **not** ship —
  without forking the shared `DRP.yaml`. E.g. an instrument with no defect maps
  sets `isr_overrides={"doDefect": False}` (CTIO does exactly this). Default
  `{}` (inherit the framework ISR config unchanged).
- **`crosstalk`** — Optional `stips.CrosstalkSpec` declaring intra-detector
  crosstalk coefficients for a **multi-amp** camera (an N×N matrix, N = amp
  count, zero diagonal). When set, STIPS builds a `CrosstalkCalib` from it,
  certifies it into the calib chain, and enables ISR crosstalk correction
  (`stips measure-crosstalk` can measure the matrix from data first). `None`
  (Nickel, single-amp) disables crosstalk entirely.
- **`obs_data_package`** — Optional companion EUPS data package with curated
  calibs / defects / crosstalk (`"obs_nickel_data"`). Co-locate it under your
  instrument dir (the reference layout is `instruments/nickel/obs_nickel_data`);
  `resolve_data_package_dir()` finds `<INSTRUMENT_DIR>/<obs_data_package>`
  automatically, and the stack activation sets it up by name. Omit if you have
  none — and disable the ISR steps that would need its products via
  `isr_overrides`. To build the defect maps this package ships, run the generic
  `stips-defects-build` tool against master calibs (recipe:
  `instruments/nickel/defects/README.md`).
- **`package_dir`** — Optional filesystem path to the instrument package root,
  for profiles that need to resolve their own bundled resources. Normally left
  unset (the loader already knows `INSTRUMENT_DIR`); Nickel omits it. When
  `obs_data_package` is set, `package_dir` is the explicit override for where it
  lives on disk (absolute path, or a name resolved under the instrument dir);
  when unset, STIPS looks for `<INSTRUMENT_DIR>/<obs_data_package>` first, then
  the reference `packages/<obs_data_package>` layout (see
  `stips.core.config.resolve_data_package_dir` for the full precedence).
- **`ps1_band_map`** — PS1-template policy: maps a **local science band → the PS1
  band** to download for it. The map's *keys* are the local bands eligible for
  external PS1 templates; in `template.type: auto`, every other band falls back
  to a coadd template. PS1 serves *grizy*, so a Johnson–Cousins instrument like
  Nickel maps only its `{"r": "r", "i": "i"}`; a Sloan fork could add
  `{"g": "g"}`. The default (empty dict) means **no PS1 templates** — the safe
  choice for an unknown fork, which then uses coadd templates for every band.
- **`template_band_maps`** — Per-source external-template band policy: `SOURCE
  NAME -> (LOCAL band -> that survey's band)`, e.g.
  `{"skymapper": {"r": "r", "i": "i"}}`. This is **separate** from
  `ps1_band_map` above and does not replace it: `ps1_band_map` is also exported
  as `STIPS_PS1_BAND_MAP` (JSON) by `run_with_stack()` and consumed by the
  in-stack `refcats_gaia_ps1*.py` pex_config overlays to build the PS1
  reference-catalog filterMap, so it cannot be renamed or folded into the
  generic map without breaking that consumer. For `source="ps1"`,
  `template_band_map(config, "ps1")` (`stips.core.pipeline`) checks
  `template_band_maps["ps1"]` first and falls back to `ps1_band_map` when no
  entry exists — so a profile written before `template_band_maps` existed keeps
  working untouched. Add a new survey by giving it its own key here; you do
  not need to touch `ps1_band_map` to do so.

  **Band maps are not identity maps — worked example.** The CTIO 1.0m profile
  (`instruments/ctio1m/profile.py`) is Johnson-Cousins (`U`/`B`/`V`/`R`/`I`).
  SkyMapper's `v` filter is a Strömgren-like **violet band at ~384 nm** — not
  Johnson V (~551 nm). An identity map (`"v": "v"`) would silently fetch a
  near-UV template for a green science image. CTIO's map is therefore
  `{"skymapper": {"r": "r", "i": "i"}}` — `v` is **deliberately excluded**
  rather than mismapped, because the honest match (SkyMapper `g` at ~510 nm)
  needs a colorterm nobody has fit yet. Treat every entry in a
  `template_band_maps` map as a claim that needs its own justification, not a
  name-matching exercise.
- **`fov_arcmin`** — Approximate science field of view in **arcmin** (the long
  dimension is fine; this is an order-of-magnitude figure, not geometry). Its
  only consumer is the external-template coverage warning: a survey cutout
  narrower than the science field leaves dithered pointings with no PSF-matching
  kernel candidates (`NoKernelCandidatesError`), and SkyMapper's hard 0.17°
  (10.2′) cap is under the FOV of most 1-m-class cameras. Nickel declares `6.3`,
  CTIO 1.0m / Y4KCam `20.0`. Camera geometry is deliberately not used as a
  substitute (binning and partial illumination make it unreliable). Leave it
  unset if you have not measured it — the warning then stays silent rather than
  asserting a field size STIPS guessed.
- **`fetch_data`** — Optional callable hook: `fetch_data(night, config, *,
  overwrite=False) -> "ok" | "not_found" | "failed"`, used by `stips download`.
  Wire it from a co-located module (Nickel's `profile.py` does `from fetch import
  fetch_data` — the loader puts `instruments/<x>/` on `sys.path` so a co-located
  `fetch.py` is importable). Your `fetch.py` implements only the archive backend
  `_fetch_night(night, raw_root, *, overwrite, **kwargs) -> int` (0 ok / 1 failed
  / 2 not-found) plus a `build_kwargs(env)` mapping your env schema, then
  `fetch_data = make_fetch_data(_fetch_night, build_kwargs)` from `stips.fetch`
  (use `stips.fetch.parse_night` for `YYYYMMDD` validation) — the wrapper, night
  validation, and status mapping are shared. Leave unset if you place raws by hand.
- **`policy_name`**, **`refcat_path`** — Optional; `policy_name` defaults to `name`.

### Quirk hooks (`@hook(profile)`)

Hooks override individual translation methods. Register one by decorating a
function whose name matches the method; the function takes `header` (and, for a
few, a `default`/`raw` argument). Write a hook only when a header field needs
logic beyond a flat `header_map` lookup. The hooks Nickel actually defines:

- **`observation_type(header)`** — Classify the frame (`science`, `flat`,
  `bias`, `dark`, `focus`) from your `OBSTYPE`/`OBJECT` strings.
- **`observation_reason(header)`** — `calibration` / `focus` / `test` /
  `pointing` / `science`.
- **`temperature(header)`** — Return an astropy `Quantity`. Nickel reads
  `TEMPDET` in Celsius and converts to Kelvin.
- **`exposure_id(header)` / `visit_id(header)`** — Build a unique ID that fits in
  31 bits. Use `stips.make_exposure_id(datetime_end, seqnum)` if your observing
  night maps 1:1 onto a UT day (Nickel does: `days_since_2000 * 10000 + OBSNUM`).
  **Verify that assumption before relying on it.** If your site's local night
  straddles UT midnight *and* your seqnum resets each local night, the UT day is
  NOT a unique key — night N's post-midnight frames and night N+1's afternoon
  calibs share a UT day and a reset seqnum, and collide (real case: CTIO ids
  `36730069` on both SA98 nights 20100120/20100121, failing Butler exposure-sync
  on ingest). Derive your own day term from the local night and call
  `stips.pack_exposure_id(days_since_2000, seqnum)` instead — see
  `instruments/ctio1m/profile.py`. This only bites on multi-night ingest, so
  single-night testing will not surface it.
- **`datetime_begin(header)` / `datetime_end(header)`** — Nickel prefers
  `DATE-BEG`/`DATE-END`, falls back to `DATE-OBS`, and synthesizes the end from
  `begin + EXPTIME` when `DATE-END` is missing or bad.
- **`day_obs(header)` / `observation_id(header)`** — Derive `YYYYMMDD` and a
  globally-unique string ID.
- **`unknown_filter(header, raw)`** — Fallback when `filter_aliases` has no
  match (Nickel logs a warning and returns `"clear"`).
- **`tracking_radec(header, default=None)`** — Coordinate read with quirk
  handling. Nickel's `CRVAL1/CRVAL2` (WCS) sometimes disagree with `RA`/`DEC`
  (telescope control); the hook calls `default()` for the generic CRVAL read,
  compares against the sexagesimal `RA`/`DEC`, and falls back to the control
  values when they diverge by more than 1°.

A short, realistic hook example:

```python
@hook(profile)
def observation_type(header):
    """Return one of: science | flat | bias | dark | focus."""
    obstype = header.get("OBSTYPE", "").strip().lower()
    obj = header.get("OBJECT", "").strip().lower()
    if obstype == "dark":
        return "bias" if "bias" in obj else "dark"
    if obstype == "flat" or "flat" in obj:
        return "flat"
    if any(w in obj for w in ("focus", "point", "test")):
        return "focus"
    return "science"


@hook(profile)
def temperature(header):
    import astropy.units as u
    return (header.get("TEMPDET", -999.0) + 273.15) * u.K
```

---

## 5. Step 4 — Camera geometry

There are two ways to give STIPS a camera.

**Simple path — `CameraSpec`.** For a single-CCD, geometry-only camera, set
`profile.camera` to a `CameraSpec` (imported from `stips`) instead of a YAML
path. You declare CCD size, pixel size, plate scale, and orientation, and STIPS
synthesizes a usable LSST camera in-memory with a default single-amp readout:

```python
from stips import CameraSpec

profile = InstrumentProfile(
    ...,
    camera=CameraSpec(
        nx=1024, ny=1024,
        pixel_size_um=30.0,
        plate_scale_arcsec_per_pixel=0.368,
        flip_x=False, flip_y=True,
    ),
)
```

`gain` / `read_noise` / `saturation` are optional (sensible defaults). This is
all most single-CCD telescopes need.

**Full-control escape hatch — `camera/<x>.yaml`.** A standard LSST
`yamlCamera`-format file describing detector layout, amps, gain, and read noise.
Set `profile.camera` to its path (loaded from `INSTRUMENT_DIR`, no EUPS lookup);
use `camera/nickel.yaml` as a template. Nickel deliberately uses the YAML to get
real multi-amp / gain / read-noise fidelity that the simple `CameraSpec` path
does not model.

**Multi-amp cameras.** A single CCD read out through several amplifiers is fully
supported via the YAML path — list each amp under the CCD's `amplifiers:` with
its own `rawBBox` / `rawDataBBox` / overscan bboxes / `readCorner` / `flipXY`,
and ISR does per-amp overscan and assembly automatically. `camera/y4kcam.yaml`
(CTIO 1.0m Y4KCam) is a worked **4-amp** example, including the trickier case of
amps that read toward the detector centre (overscan strips on the *inner* edges).
The single biggest source of master-bias seams is wrong overscan geometry, so
measure your amp boundaries from a real raw (per-column / per-row medians) rather
than trusting documentation.

---

## 6. Step 5 — Pipelines & configs

**You inherit the whole neutral reference set — no copying required for the
common case.** STIPS ships reference pipelines
(`packages/obs_stips/instrument_defaults/pipelines/`) and config overrides
(`packages/obs_stips/instrument_defaults/configs/`), and every fork inherits all
of them by default. A minimal fork carries **zero** pipeline/config files. The
structural thresholds in these defaults are a REFERENCE tuning from the Nickel
1-m (relaxed cuts for a small-aperture, sparse-field instrument) — a working
starting point that you tweak only where your telescope differs. See
`packages/obs_stips/instrument_defaults/README.md` for the full tiering
contract.

**What you do NOT inherit: photometric calibration.** Nickel's empirically-fit
color terms (`colorterms.py`), its `calibrateImage/tuned_configs/`, and its
Gaia+PS1 refcat overlay live in `instruments/nickel/configs/` — they are fit for
one telescope and are deliberately excluded from the neutral tier. **Your fork
still runs science out of the box**: with no tuned `calibrateImage` config,
`science.py` falls back to the neutral `calibrateImage/neutral_default.py`
(schema-compat only, no tuning), and until you fit your own color terms and drop
a `colorterms.py` into `instruments/<x>/configs/`, photometry is calibrated with
a plain per-visit zeropoint (color terms OFF). Fitting these is a fork's **#1
review item** for quality, not a prerequisite to process. (An
explicitly-configured-but-missing config path still errors — that is typo
protection, not the no-config fallback.) Two framework tools produce these
per-instrument files: `stips-colorterms-fit` fits your `colorterms.py` from
matched standard-star photometry (recipe:
`instruments/nickel/colorterms/README.md`) and `stips-tune-calibrate-image`
searches `calibrateImage` parameters to produce your `tuned_configs/` (recipe:
`instruments/nickel/tuning/README.md`). The rules:

- **Override one file by dropping a same-named file.** The CLI resolves each
  pipeline/config **instrument-dir-first, else framework default** (via
  `Config.resolve_pipeline` / `resolve_config`). To override `DIA.yaml`, place
  your own `instruments/<x>/pipelines/DIA.yaml`; everything else keeps coming
  from the defaults. You override files individually — there is no all-or-nothing
  copy.
- **Make overrides thin.** An override is usually a few tweaks layered on top of
  the framework version via LSST's `imports:`, e.g.

  ```yaml
  imports:
    - location: $STIPS_DEFAULTS/pipelines/DIA.yaml
  tasks:
    subtractImages:
      config:
        # ...your tweaks...
  ```

  `$STIPS_DEFAULTS` is the env var (exported by the stack activation) pointing at
  the framework defaults dir, so reference pipelines and your overrides can
  reference siblings as `$STIPS_DEFAULTS/pipelines/<name>.yaml` and
  `$STIPS_DEFAULTS/configs/<name>.py`. Use `$STIPS_DEFAULTS/...` to reference
  framework siblings and `$INSTRUMENT_DIR/...` to reference your fork's own
  sibling files — both are exported by stack activation.
- **Never import the profile inside a pex_config (`.py`) config override.**
  pex_config replays every module first-imported while a config file executes
  when a saved quantum graph is reloaded, and the path-loaded profile machinery
  is unimportable at replay time — it kills `pipetask run` at graph
  deserialization. If your override needs a profile value, read it from an env
  var instead: STIPS exports `STIPS_PS1_BAND_MAP` (the profile's `ps1_band_map`
  as JSON) from `run_with_stack` for exactly this reason (see the neutral
  `refcats_gaia_ps1*.py` overlays). `$STIPS_DEFAULTS` and `$INSTRUMENT_DIR` are
  exported the same way and are safe to reference by path.
- **Generic tasks stay generic.** Pipeline steps that reference
  `lsst.obs.stips.tasks.*` or `lsst.obs.stips.calibCombine.StipsCalibCombineTask`
  are framework tasks — keep those references as-is. (The robust calib-combine
  that Nickel used to ship is now a generic obs_stips task.)
- **`instrument:` is the generic class.** Pipelines with an `instrument:` field
  use `lsst.obs.stips.active.Instrument` (same as `profile.instrument_class`).
- **Genuinely instrument-specific tasks (rare).** If your telescope needs a
  custom PipelineTask that no generic one covers, ship a Python module in your
  instrument dir (namespaced, e.g. `instruments/<x>/<x>_tasks.py`, so it can't
  shadow a stack module), declare its FQN, and reference it from your pipeline.
  Nickel ships none — both of its old quirk tasks generalized into `obs_stips`.

---

## 7. Step 6 — Point STIPS at your instrument

There is **nothing to install** — the instrument is loaded by path. Just tell
STIPS where your instrument dir is, via the `env:` block of the config YAML you
pass with `-c`:

```yaml
env:
  INSTRUMENT_DIR: /path/to/stips/instruments/<x>
  # ...plus REPO, STACK_DIR, RAW_PARENT_DIR (and optional REFCAT_REPO, CP_PIPE_DIR)
```

`stips` loads `INSTRUMENT_DIR/profile.py` by path; `obs_stips` synthesizes the
LSST instrument from it and Butler registers `lsst.obs.stips.active.Instrument`
(which reports your `profile.name`). Every collection name, Butler query, and
skymap reference is driven by your profile — collections become
`<your collection_prefix>/...`.

> If `INSTRUMENT_DIR` is unset (or has no `profile.py`), the framework fails loud
> with a clear message rather than guessing. There is no `INSTRUMENT_PACKAGE` and
> no obs-package import — the old package-based selection is gone.

---

## 8. Step 7 — Run

Same CLI, your instrument. Pass your config once with the group-level `-c`:

```bash
CFG=scripts/config/<target>/pipeline.yaml
stips -c $CFG bootstrap                 # create repo, register your instrument, ingest refcats, skymap
stips -c $CFG calibs <night>            # build bias/flat, certify
stips -c $CFG science <night> --ra <RA> --dec <DEC>
stips -c $CFG dia <night> --auto
stips -c $CFG fphot <night> --ra <RA> --dec <DEC>
stips -c $CFG lightcurve --ra <RA> --dec <DEC> --collections <...>
# or drive the whole thing from the same YAML config:
stips -c $CFG run
```

---

## 9. Verifying & common gotchas

**Free contract coverage.** Ship `tests/contract_data.py` in your instrument
dir and the framework's auto-discovered contract suite tests your profile,
exposure-id scheme, translation, and fetch hook with no test code to write —
see `docs/instrument-contract.md`.

**Translation parity — test first.** Before running pipelines, point
`astro_metadata_translator` at a real raw FITS header from your telescope and
confirm the translator resolves `physical_filter`, `observation_type`,
`exposure_id`, `datetime_begin/end`, `day_obs`, and `tracking_radec` to sane
values. Most fork bugs are header-mapping bugs, and they surface here cheaply.
(`instruments/nickel/tests/test_translation_golden.py` shows the pattern: set
`INSTRUMENT_DIR`, import `lsst.obs.stips.active`, and assert on its `Translator`.)

**Checklist:**

- [ ] `name`, `site`, `filters`, `header_map`, `camera` set (the required fields).
- [ ] `filter_aliases` covers every spelling your headers actually emit (check
      real files, not the manual).
- [ ] `filter_key` matches your FITS filter keyword.
- [ ] `instrument_header_value` set if your `name` differs from FITS `INSTRUME`.
- [ ] `night_to_dayobs_offset_days` verified by ingesting one frame (not assumed).
- [ ] `isr_overrides` disables ISR steps whose curated calibs you don't ship.
- [ ] A `@hook` exists for every header quirk (observation typing, coordinate
      bugs, exposure-ID scheme, temperature units, datetime derivation).
- [ ] `camera/<x>.yaml` reflects your CCD dimensions, pixel scale, plate scale.
- [ ] `instrument_class` left as `"lsst.obs.stips.active.Instrument"` (the generic class).
- [ ] `INSTRUMENT_DIR: /path/to/instruments/<x>` set in the config YAML's `env:` block.
- [ ] Translator parity verified against a real header.

**Other gotchas:**

- **Single-CCD assumption.** STIPS targets single-detector cameras. A
  multi-detector mosaic needs more than a profile and is out of scope here.
- **Camera geometry matters.** A wrong plate scale or detector size silently
  corrupts WCS fitting and source matching. Get `camera/<x>.yaml` right early.
- **Don't name your in-instrument files after stdlib/stack modules.** The loader
  puts `instruments/<x>/` on `sys.path` (so `profile.py`'s co-located hooks like
  `fetch.py` import). The framework appends it (so stdlib/installed modules still
  win), but avoid generic names that could collide if another path entry is added.
- **`day_obs` is UT, derived from the datetime — not from a hook.** The Butler
  `day_obs` dimension comes from `astro_metadata_translator.to_observing_day`
  (the UT calendar day of the exposure). A profile `day_obs` hook *can* override
  it, but it must return the **UT** day, not a local-night keyword like
  `DTCALDAT`. Returning the local night makes the stored `day_obs` disagree with
  the offset convention, and `stips science` then silently selects **zero**
  exposures ("No target_name matches… Available: []") even though the data is
  ingested. To find your `night_to_dayobs_offset_days`: ingest one frame, read
  its stored `day_obs`, and compare to the local night you'd name it by. CTIO's
  2007-03-21 (local) frames store `day_obs=20070322` (UT) → offset `1`.
- **No defects/crosstalk/linearity calibs?** The framework ISR defaults assume
  Nickel's curated calibs exist (`doDefect: true`). If your instrument ships
  none, the qgraph build fails with *"Not enough datasets (0) found for
  non-optional connection isr.defects"*. Fix it with `isr_overrides={"doDefect":
  False}` in your profile — not by editing the shared `DRP.yaml`.
- **Skymap hash collisions.** The stack registers one skymap *name* per geometry
  *hash*. If you reuse the framework `makeSkyMap.py` geometry verbatim, a repo
  that already registered another instrument's skymap with the same geometry will
  refuse a second name. Ship your own `configs/makeSkyMap.py` at your native
  `pixelScale` (distinct geometry → distinct hash) — or just bootstrap a fresh
  repo, since it's one instrument per repo anyway.
- **`exposure_id` must fit 31 bits.** If your scheme can overflow, the hook
  should raise (Nickel's does) rather than silently wrap.
- **Hooks return the right types.** `temperature` returns an astropy
  `Quantity`; `datetime_*` return `astropy.time.Time`; `tracking_radec` returns
  a `SkyCoord`. Match the framework's expectations.
- **One instrument per repo.** A Butler repo holds one synthesized instrument
  (the fork-per-telescope model). Multi-instrument repos are unsupported.
