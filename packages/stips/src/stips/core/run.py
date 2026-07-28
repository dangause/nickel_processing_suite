"""YAML-driven pipeline orchestrator.

This module reads a YAML configuration file and orchestrates the full pipeline:
calibs → science → DIA → forced photometry → lightcurve → period/transit analysis.

Example YAML format:
    # Environment configuration: the self-contained env: block is the sole
    # config source (supplied to `stips -c <this file>`).
    env:
      REPO: "/path/to/butler/repo"
      STACK_DIR: "/path/to/lsst_stack"
      INSTRUMENT_DIR: "/path/to/instruments/nickel"
      RAW_PARENT_DIR: "/path/to/raw/data"

    object: "SN2023ixf"   # Must match target_name in FITS (case-insensitive partial match)
    ra: 210.910750
    dec: 54.311694
    bands: ["r", "i"]

    template:
      type: ps1           # or "coadd"
      degrade_seeing: 2.0  # optional
      nights:             # for coadd type: template nights (SN faded)
        - 20230625
        - 20230629

    science:
      nights:
        - 20230519
        - 20230521

    # Pipeline configuration files (paths relative to the instrument's configs/)
    configs:
      science:
        calibrate_image: calibrateImage/tuned_configs/2023ixf_relaxed.py
        calibrate_image_fallbacks:
          - calibrateImage/tuned_configs/2023ixf_relaxed_psfex_sparse.py
        colorterms: apply_colorterms.py
      dia:
        subtract_images: dia/subtractImages.py
        detect_and_measure: dia/detectAndMeasure.py

    options:
      jobs: 8
      skip_calibs: false
      skip_science: false
      skip_dia: false
      forced_phot: true
      forced_phot_image_type: diffim  # visit, diffim, or both
      lightcurve: true
      lightcurve_dataset_type: dia_source_unfiltered  # or forced_phot_diffim_radec
      lightcurve_min_snr: 3.0
      use_fallbacks: true    # Try fallback configs on failure
      pipeline_type: supernova   # supernova | variable | transit
      period_search: false       # Enable Lomb-Scargle period search
      period_min: 0.1            # Minimum search period (days)
      period_max: 100.0          # Maximum search period (days)
      period_samples: 10000      # Frequency grid density
      transit_search: false       # Enable BLS transit search
      transit_duration_min: 0.5   # Min transit duration (hours)
      transit_duration_max: 6.0   # Max transit duration (hours)
      search_method: lomb_scargle # lomb_scargle | bls | both
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from stips.core.refcat import ensure_refcats
from stips.core.run_logging import (
    generate_run_id as _generate_run_id,
)
from stips.core.run_logging import (
    get_step_log_file as _get_step_log_file,
)
from stips.core.run_logging import (
    maybe_split_log as _maybe_split_log,
)
from stips.core.run_logging import (
    setup_run_logging as _setup_run_logging,
)
from stips.core.run_logging import (
    split_step_logs as _split_step_logs,
)

if TYPE_CHECKING:
    from stips.core.config import Config
    from stips.core.lightcurve import LightcurveConfig
    from stips.core.science import ScienceConfig

log = logging.getLogger(__name__)

# Staging default: "monster" preserves legacy behavior until Gaia/PS1 is
# validated (see docs/refcat-validation-runbook.md). Single source of truth for
# both the RunConfig field default and the from_yaml fallback.
DEFAULT_REFCAT_MODE = "monster"


@dataclass
class ScienceConfigs:
    """Science processing configuration paths."""

    calibrate_image: str | None = None
    calibrate_image_fallbacks: list[str] = field(default_factory=list)
    colorterms: str | None = None


@dataclass
class DIAConfigs:
    """DIA processing configuration paths."""

    subtract_images: str | None = None
    detect_and_measure: str | None = None


@dataclass
class CoaddConfigs:
    """Coadd template building configuration paths."""

    make_direct_warp: str | None = None
    select_template_coadd_visits: str | None = None
    select_deep_coadd_visits: str | None = None


#: ``template.type`` values that are not an external-survey adapter name.
INTERNAL_TEMPLATE_TYPES = ("coadd", "auto")


def valid_template_types() -> list[str]:
    """Every accepted ``template.type``: the internal ones plus every adapter."""
    from stips.pipeline_tools.external_template.sources import SOURCES

    return [*INTERNAL_TEMPLATE_TYPES, *sorted(SOURCES)]


def validate_template_type(template_type: str) -> str:
    """Reject an unrecognised ``template.type`` as early as it can be seen.

    The dispatch is an if/elif chain with no else, so a typo
    (``type: skymappper``) used to ingest nothing, and then every band failed
    DIA with "no template available" -- after the run had already spent hours on
    calibs and science. Failing here costs nothing and names the valid values.
    """
    valid = valid_template_types()
    if template_type not in valid:
        raise ValueError(
            f"Unknown template.type {template_type!r}; valid values are: "
            f"{', '.join(valid)}"
        )
    return template_type


@dataclass
class RunConfig:
    """Configuration parsed from YAML."""

    object_name: str
    ra: float | None
    dec: float | None
    bands: list[str]
    nights: list[str] = field(default_factory=list)

    # Template configuration
    template_type: str = "ps1"  # "ps1" | "skymapper" | "coadd" | "auto"
    template_degrade_seeing: float | None = None
    template_size: float = 0.3  # PS1 cutout size in degrees (default: 0.3)
    template_unity_photocalib: bool = False  # Force PhotoCalib=1.0 for PS1 templates
    template_nights: list[str] = field(default_factory=list)
    template_mjd_start: float | None = (
        None  # optional epoch filter (external templates)
    )
    template_mjd_end: float | None = None

    # Reference catalog configuration.
    # STAGING DEFAULT is "monster" so default runs behave exactly as before
    # (no on-demand fetch; science uses the MONSTER refcat baked into DRP.yaml).
    # Opt into the new path with `refcat: {mode: gaia_ps1}`. After validation on
    # real data, flip this default to "gaia_ps1" (see docs/refcat-validation-runbook).
    refcat_mode: str = DEFAULT_REFCAT_MODE  # "monster" | "gaia_ps1"
    refcat_radius_deg: float = 0.3  # cone radius for on-demand fetch
    refcat_gaia_quality: dict | None = None  # optional Gaia quality cuts

    # Pipeline config files
    science_configs: ScienceConfigs = field(default_factory=ScienceConfigs)
    dia_configs: DIAConfigs = field(default_factory=DIAConfigs)
    coadd_configs: CoaddConfigs = field(default_factory=CoaddConfigs)

    # Processing options
    jobs: int = 8
    skip_calibs: bool = False
    skip_science: bool = False
    skip_dia: bool = False
    forced_phot: bool = True
    forced_phot_image_type: str = "diffim"  # visit, diffim, or both
    lc_config: "LightcurveConfig" = field(
        default_factory=lambda: __import__(
            "stips.core.lightcurve", fromlist=["LightcurveConfig"]
        ).LightcurveConfig()
    )
    rebuild_templates: bool = False
    continue_on_error: bool = True
    use_fallbacks: bool = True

    # Variable star options
    pipeline_type: str = "supernova"  # "supernova" or "variable" or "transit"
    period_search: bool = False
    period_min: float = 0.1
    period_max: float = 100.0
    period_samples: int = 10_000

    # Transit search options
    search_method: str = "lomb_scargle"  # "lomb_scargle" | "bls" | "both"
    transit_search: bool = False
    transit_duration_min: float = 0.5  # hours
    transit_duration_max: float = 6.0  # hours

    # Execution backend
    execution: str = "local"  # "local" | "bps"
    site: str = "local"  # "local" | "slurm" | "htcondor" | "singularity-slurm"
    concurrent_nights: int = 0  # 0 = sequential (default)
    bps_poll_interval: float = 5.0  # Seconds between BPS status checks
    bps_timeout: float = 7200.0  # Per-stage BPS timeout in seconds

    # HPC container options
    container_image: str | None = None  # Path to Singularity/Apptainer SIF image

    def __post_init__(self) -> None:
        validate_template_type(self.template_type)

    @classmethod
    def from_yaml(cls, path: Path) -> RunConfig:
        """Load configuration from YAML file."""
        from stips.core.lightcurve import LightcurveConfig

        with open(path) as f:
            data = yaml.safe_load(f)

        # Extract template config
        template = data.get("template", {})
        template_type = template.get("type", "ps1")
        template_degrade_seeing = template.get("degrade_seeing")
        template_size = float(template.get("size", 0.3))
        template_unity_photocalib = template.get("unity_photocalib", False)
        # Convert template nights to strings (YAML parses 20230519 as int)
        template_nights = [str(n) for n in template.get("nights", [])]
        template_mjd_start = template.get("mjd_start")
        template_mjd_end = template.get("mjd_end")

        # Extract refcat config (on-demand Gaia/PS1; absent section => defaults).
        # Staging default "monster" keeps behavior unchanged until validated.
        refcat = data.get("refcat", {})
        refcat_mode = refcat.get("mode", DEFAULT_REFCAT_MODE)
        refcat_radius_deg = float(refcat.get("radius_deg", 0.3))
        refcat_gaia_quality = refcat.get("gaia_quality")

        # Extract options
        options = data.get("options", {})

        # Parse science nights from science.nights (preferred) or legacy nights key.
        # Preferred:  science: { nights: [20230519, ...] }
        # Legacy:     nights: [20230519, ...]  or  nights: {20230519: {r: [], ...}, ...}
        science_section = data.get("science", {})
        raw_nights = science_section.get("nights") if science_section else None
        if raw_nights is None:
            raw_nights = data.get("nights", [])

        if isinstance(raw_nights, list):
            nights = [str(n) for n in raw_nights]
        elif isinstance(raw_nights, dict):
            # Legacy dict format: keys are nights
            nights = [str(k) for k in raw_nights.keys()]
            log.debug(
                "Using legacy dict format for nights (consider migrating to list)"
            )
        else:
            nights = []

        # Extract config file paths
        configs = data.get("configs", {})
        science_cfg_data = configs.get("science", {})
        dia_cfg_data = configs.get("dia", {})

        science_configs = ScienceConfigs(
            calibrate_image=science_cfg_data.get("calibrate_image"),
            calibrate_image_fallbacks=science_cfg_data.get(
                "calibrate_image_fallbacks", []
            ),
            colorterms=science_cfg_data.get("colorterms"),
        )

        dia_configs = DIAConfigs(
            subtract_images=dia_cfg_data.get("subtract_images"),
            detect_and_measure=dia_cfg_data.get("detect_and_measure"),
        )

        coadd_cfg_data = configs.get("coadd", {})
        coadd_configs = CoaddConfigs(
            make_direct_warp=coadd_cfg_data.get("make_direct_warp"),
            select_template_coadd_visits=coadd_cfg_data.get(
                "select_template_coadd_visits"
            ),
            select_deep_coadd_visits=coadd_cfg_data.get("select_deep_coadd_visits"),
        )

        # Apply pipeline_type defaults
        pipeline_type = options.get("pipeline_type", "supernova")

        # Variable star default: forced phot on both visit + diffim
        if pipeline_type == "variable" and "forced_phot_image_type" not in options:
            default_fphot_type = "both"
        # Transit default: forced phot on visit images (total flux for depth)
        elif pipeline_type == "transit" and "forced_phot_image_type" not in options:
            default_fphot_type = "visit"
        else:
            default_fphot_type = "diffim"

        # Transit default: enable transit search with BLS
        search_method = options.get("search_method", "lomb_scargle")
        if pipeline_type == "transit":
            default_transit_search = True
            if "search_method" not in options:
                search_method = "bls"
        else:
            default_transit_search = False

        return cls(
            object_name=data.get("object", ""),
            ra=data.get("ra"),
            dec=data.get("dec"),
            bands=data.get("bands", ["r"]),
            nights=nights,
            template_type=template_type,
            template_degrade_seeing=template_degrade_seeing,
            template_size=template_size,
            template_unity_photocalib=template_unity_photocalib,
            template_nights=template_nights,
            template_mjd_start=template_mjd_start,
            template_mjd_end=template_mjd_end,
            refcat_mode=refcat_mode,
            refcat_radius_deg=refcat_radius_deg,
            refcat_gaia_quality=refcat_gaia_quality,
            science_configs=science_configs,
            dia_configs=dia_configs,
            coadd_configs=coadd_configs,
            jobs=options.get("jobs", 8),
            skip_calibs=options.get("skip_calibs", False),
            skip_science=options.get("skip_science", False),
            skip_dia=options.get("skip_dia", False),
            forced_phot=options.get("forced_phot", True),
            forced_phot_image_type=options.get(
                "forced_phot_image_type", default_fphot_type
            ),
            lc_config=LightcurveConfig.from_yaml(data.get("lightcurve"), options),
            rebuild_templates=options.get("rebuild_templates", False),
            continue_on_error=options.get("continue_on_error", True),
            use_fallbacks=options.get("use_fallbacks", True),
            pipeline_type=pipeline_type,
            period_search=options.get("period_search", False),
            period_min=float(options.get("period_min", 0.1)),
            period_max=float(options.get("period_max", 100.0)),
            period_samples=int(options.get("period_samples", 10_000)),
            search_method=search_method,
            transit_search=options.get("transit_search", default_transit_search),
            transit_duration_min=float(options.get("transit_duration_min", 0.5)),
            transit_duration_max=float(options.get("transit_duration_max", 6.0)),
            execution=options.get("execution", "local"),
            site=options.get("site", "local"),
            concurrent_nights=int(options.get("concurrent_nights", 0)),
            bps_poll_interval=float(options.get("bps_poll_interval", 5.0)),
            bps_timeout=float(options.get("bps_timeout", 7200.0)),
            container_image=options.get("container_image"),
        )


@dataclass
class RunResult:
    """Result of pipeline run."""

    success: bool
    failed_calibs: list[str] = field(default_factory=list)
    failed_science: list[str] = field(default_factory=list)
    failed_dia: list[str] = field(default_factory=list)
    failed_fphot: list[str] = field(default_factory=list)
    template_collections: dict[str, str] = field(default_factory=dict)
    forced_phot_collections: dict[str, list[str]] = field(default_factory=dict)
    differential_phot_success: bool | None = None
    lightcurve_path: str | None = None
    period_result_path: str | None = None
    transit_result_path: str | None = None
    log_dir: str | None = None
    error: str | None = None


def _create_executor(run_cfg: RunConfig):
    """Create the appropriate executor from RunConfig.

    Args:
        run_cfg: Pipeline run configuration

    Returns:
        LocalExecutor for local execution, BPSExecutor for BPS execution
    """
    from stips.core.executor import BPSExecutor, LocalExecutor

    if run_cfg.execution == "bps":
        return BPSExecutor(
            site=run_cfg.site,
            poll_interval=run_cfg.bps_poll_interval,
            timeout=run_cfg.bps_timeout,
            container_image=run_cfg.container_image,
        )
    return LocalExecutor()


def _dispatch_concurrent(
    fn,
    items: list,
    *,
    max_workers: int = 4,
    item_label: str = "item",
):
    """Run fn(item) concurrently for each item.

    Args:
        fn: Callable that takes a single item and returns a result
        items: List of items to process
        max_workers: Maximum concurrent workers
        item_label: Label for log messages

    Returns:
        Dict mapping each item to its result (None if fn raised an exception)
    """
    import concurrent.futures

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_item = {pool.submit(fn, item): item for item in items}
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                results[item] = future.result()
            except Exception as e:
                log.error(f"  {item_label} {item} raised: {e}")
                results[item] = None
    return results


BROADBAND = {"b", "v", "r", "i"}


def _split_band_groups(bands: list[str]) -> list[list[str]]:
    """Split bands into broadband group + individual narrowband filters.

    Broadband filters (b, v, r, i) go in one quantum graph;
    each narrowband/Sloan filter (halpha, oiii, gp, rp, ...) gets its
    own group.  This prevents a missing calibration for one filter
    (e.g. no OIII flat) from killing unrelated filters.

    Returns:
        List of band groups. Falls back to [bands] if all bands
        belong to one group.
    """
    bb = [b for b in bands if b in BROADBAND]
    other = [b for b in bands if b not in BROADBAND]
    groups: list[list[str]] = []
    if bb:
        groups.append(bb)
    for nb in other:
        groups.append([nb])
    return groups or [bands]


def _run_external_templates(
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
    *,
    source: str,
    bands: list[str] | None = None,
) -> None:
    """Ingest an external-survey template for each eligible band."""
    from stips.core import external_template
    from stips.core.pipeline import template_band_map

    eligible = template_band_map(config, source)
    for band in bands if bands is not None else run_cfg.bands:
        if band not in eligible:
            log.warning(
                "%s templates not available for band %s (eligible: %s), skipping",
                source,
                band,
                ", ".join(sorted(eligible)) or "none",
            )
            continue

        log.info("Ingesting %s template for %s-band...", source, band)

        if not dry_run:
            step_log = _get_step_log_file(f"{source}_template", band=band)
            tmpl_result = external_template.run(
                source,
                run_cfg.ra,
                run_cfg.dec,
                band,
                config,
                size=run_cfg.template_size,
                degrade_seeing=run_cfg.template_degrade_seeing,
                unity_photocalib=run_cfg.template_unity_photocalib,
                overwrite=run_cfg.rebuild_templates,
                mjd_start=getattr(run_cfg, "template_mjd_start", None),
                mjd_end=getattr(run_cfg, "template_mjd_end", None),
                log_file=step_log,
            )
            if tmpl_result is not None and tmpl_result.success:
                result.template_collections[band] = tmpl_result.collection
            elif tmpl_result is not None:
                log.warning(
                    "%s template failed for %s: %s", source, band, tmpl_result.error
                )
        else:
            log.info(
                "  [DRY RUN] external_template.run(%s, ra=%s, dec=%s, band=%s)",
                source,
                run_cfg.ra,
                run_cfg.dec,
                band,
            )
            result.template_collections[band] = f"templates/{source}/{band}"


def _run_ps1_templates(run_cfg, config, result, dry_run, bands=None) -> None:
    """Back-compat wrapper; PS1 is one external-template source among several."""
    _run_external_templates(run_cfg, config, result, dry_run, source="ps1", bands=bands)


def _run_template_step(
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    science_cfg,
    dry_run: bool,
    *,
    executor=None,
):
    """Build the templates ``run_cfg.template_type`` asks for.

    Every registered external-survey adapter is dispatchable by name, so adding
    a source needs no edit here (``docs/architecture.md``'s extension contract).
    ``RunConfig`` has already rejected any other value, so there is no silent
    fall-through: an unrecognised ``template.type`` never reaches this point.

    Returns a failing ``RunResult`` to abort the run, or None to continue.
    """
    from stips.pipeline_tools.external_template.sources import SOURCES

    if run_cfg.template_type in SOURCES:
        _run_external_templates(
            run_cfg, config, result, dry_run, source=run_cfg.template_type
        )
    elif run_cfg.template_type == "coadd":
        early_exit = _run_coadd_templates(
            run_cfg, config, result, science_cfg, dry_run, executor=executor
        )
        if early_exit is not None:
            log.error(f"Coadd template build failed: {early_exit.error}")
            return early_exit
    elif run_cfg.template_type == "auto":
        early_exit = _run_auto_templates(
            run_cfg, config, result, science_cfg, dry_run, executor=executor
        )
        if early_exit is not None:
            log.error(f"Auto template build failed: {early_exit.error}")
            return early_exit

    _log_template_summary(run_cfg, result)
    return None


def _build_coadd_config_files(run_cfg: "RunConfig", config: "Config") -> list[str]:
    """Map configured coadd config files to ``<taskLabel>:<resolved-path>`` entries.

    Resolution is instrument-dir-first (see Config.resolve_config), so a fork can
    override any of these by dropping the file in instruments/<name>/configs/.
    """
    mapping = [
        ("makeDirectWarp", run_cfg.coadd_configs.make_direct_warp),
        (
            "selectTemplateCoaddVisits",
            run_cfg.coadd_configs.select_template_coadd_visits,
        ),
        ("selectDeepCoaddVisits", run_cfg.coadd_configs.select_deep_coadd_visits),
    ]
    files: list[str] = []
    for label, rel in mapping:
        if rel:
            files.append(f"{label}:{config.resolve_config(rel)}")
    return files


def _run_coadd_templates(
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    science_cfg: "ScienceConfig",
    dry_run: bool,
    bands: list[str] | None = None,
    *,
    executor=None,
) -> RunResult | None:
    """Build coadd templates: process template nights, then coadd per band.

    ``bands`` restricts which bands get coadded (defaults to all configured).
    Returns a RunResult early-exit if continue_on_error is False and a step fails,
    or None to continue normally. ``executor`` is threaded to the template
    science and coadd steps so they run under the same backend (BPS/local) as the
    main pipeline; None keeps them local.
    """
    coadd_bands = bands if bands is not None else run_cfg.bands
    from stips.core import calibs, coadd, science
    from stips.core.pipeline import template_deep

    if not run_cfg.template_nights:
        log.error("Coadd template type requires template.nights in YAML")
        return RunResult(
            success=False,
            error="Coadd template type requires template.nights configuration",
        )

    # Process template nights through calibs
    if not run_cfg.skip_calibs:
        for night in run_cfg.template_nights:
            log.info(f"Running calibrations for template night {night}...")
            if not dry_run:
                calib_log = _get_step_log_file("calibs_template", night=night)
                calib_result = calibs.run(
                    night, config, jobs=run_cfg.jobs, log_file=calib_log
                )
                _maybe_split_log(calib_log)
                if not calib_result.success:
                    result.failed_calibs.append(f"template:{night}")
                    log.warning(
                        f"Calibrations failed for template night {night}: {calib_result.error}"
                    )
                    if not run_cfg.continue_on_error:
                        return RunResult(
                            success=False,
                            error=f"Template night calibrations failed for {night}",
                        )
            else:
                log.info(f"  [DRY RUN] calibs.run({night})")

    # Process template nights through science
    template_science_ran = False
    if not run_cfg.skip_science:
        for night in run_cfg.template_nights:
            bands_for_night = list(run_cfg.bands)
            if not bands_for_night:
                log.info(
                    f"Skipping template-night science for {night} "
                    "(no bands configured)"
                )
                continue

            log.info(f"Running science for template night {night}...")
            if not dry_run:
                sci_log = _get_step_log_file("science_template", night=night)
                sci_result = science.run(
                    night,
                    config,
                    jobs=run_cfg.jobs,
                    object_filter=run_cfg.object_name,
                    skip_coadds=True,
                    science_cfg=science_cfg,
                    use_fallbacks=run_cfg.use_fallbacks,
                    bands=bands_for_night,
                    target_ra=run_cfg.ra,
                    target_dec=run_cfg.dec,
                    log_file=sci_log,
                    executor=executor,
                )
                _maybe_split_log(sci_log)
                template_science_ran = True
                if not sci_result.success:
                    result.failed_science.append(f"template:{night}")
                    log.warning(
                        f"Science failed for template night {night}: {sci_result.error}"
                    )
                    if not run_cfg.continue_on_error:
                        return RunResult(
                            success=False,
                            error=f"Template night science failed for {night}",
                        )
            else:
                template_science_ran = True
                log.info(f"  [DRY RUN] science.run({night}, bands={bands_for_night})")

    # Build coadd templates per band
    force_rebuild_templates = template_science_ran or run_cfg.rebuild_templates
    if force_rebuild_templates and template_science_ran:
        log.info("Science was (re-)processed — forcing template rebuild")
    elif force_rebuild_templates:
        log.info("rebuild_templates=true — forcing template rebuild")

    for band in coadd_bands:
        log.info(f"Building coadd template for {band}-band...")

        if not dry_run:
            coadd_config_files = _build_coadd_config_files(run_cfg, config)

            coadd_log = _get_step_log_file("coadd_template", band=band)
            coadd_result = coadd.run(
                nights=run_cfg.template_nights,
                band=band,
                config=config,
                ra=run_cfg.ra,
                dec=run_cfg.dec,
                jobs=run_cfg.jobs,
                overwrite=force_rebuild_templates,
                config_files=coadd_config_files or None,
                log_file=coadd_log,
                executor=executor,
            )
            _maybe_split_log(coadd_log)
            if coadd_result.success:
                result.template_collections[band] = coadd_result.collection
                log.info(f"  Coadd template for {band}: {coadd_result.collection}")
            else:
                log.warning(f"Coadd template failed for {band}: {coadd_result.error}")
        else:
            log.info(
                f"  [DRY RUN] coadd.run(nights={run_cfg.template_nights}, band={band})"
            )
            # The real tract is computed inside coadd.run() from the skymap; it
            # is not known during a dry run, so report an honest placeholder
            # rather than a hardcoded (and usually wrong) "tract0".
            result.template_collections[band] = template_deep("<TBD>", band)

    return None


def _run_auto_templates(
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    science_cfg: "ScienceConfig",
    dry_run: bool,
    *,
    executor=None,
) -> RunResult | None:
    """Auto template strategy: PS1 for the profile's PS1-eligible bands, coadd
    for the rest.

    Returns a RunResult early-exit on coadd failure (when continue_on_error is
    False), else None.
    """
    from stips.core.pipeline import ps1_band_map

    eligible = ps1_band_map(config)
    ri = [b for b in run_cfg.bands if b in eligible]
    bv = [b for b in run_cfg.bands if b not in eligible]
    if ri:
        _run_ps1_templates(run_cfg, config, result, dry_run, bands=ri)
    if bv:
        if not run_cfg.template_nights:
            log.warning(
                "Auto templates: bands %s need coadd templates but no "
                "template.nights configured — skipping their templates.",
                ", ".join(bv),
            )
        else:
            early_exit = _run_coadd_templates(
                run_cfg,
                config,
                result,
                science_cfg,
                dry_run,
                bands=bv,
                executor=executor,
            )
            if early_exit is not None:
                return early_exit
    return None


def _log_template_summary(run_cfg: RunConfig, result: RunResult) -> None:
    """Log which bands have templates and which don't."""
    built = [b for b in run_cfg.bands if b in result.template_collections]
    failed = [b for b in run_cfg.bands if b not in result.template_collections]
    if built:
        log.info(f"Templates built for bands: {', '.join(built)}")
    if failed:
        log.warning(
            f"No templates for bands: {', '.join(failed)} (DIA will be skipped for these)"
        )


def _run_calibs_step(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
    *,
    executor=None,
) -> RunResult | None:
    """Run calibrations for each science night.

    Returns a RunResult for early exit if continue_on_error is False, else None.
    """
    from stips.core import calibs

    if dry_run:
        for night in all_nights:
            log.info(f"Running calibrations for {night}...")
            log.info(f"  [DRY RUN] calibs.run({night})")
        return None

    # Write curated calibrations (defects + crosstalk) once before per-night
    # calibs, in both the concurrent and sequential paths. These are repo-level
    # (timeless, cross-night) products, so building them per night would
    # redundantly re-run the curated write and the crosstalk build/certify; doing
    # it once also avoids SQLite write conflicts on the shared curated chain. The
    # per-night calibs then run with skip_curated=True.
    first_night = all_nights[0]
    log.info("Writing curated calibrations (one-time)...")
    curated_log = _get_step_log_file("calibs", night="curated")
    try:
        calibs.write_curated_calibrations(first_night, config, log_file=curated_log)
    except Exception as e:
        log.warning(f"Curated calibrations write failed: {e}")

    # Shared per-night body (used by both the concurrent and sequential paths,
    # so the calibs invocation lives in exactly one place).
    def _calibs_one(night: str):
        log.info(f"Running calibrations for {night}...")
        calib_log = _get_step_log_file("calibs", night=night)
        calib_result = calibs.run(
            night,
            config,
            jobs=run_cfg.jobs,
            log_file=calib_log,
            executor=executor,
            skip_curated=True,
        )
        _maybe_split_log(calib_log)
        return calib_result

    def _handle(night: str, calib_result) -> bool:
        """Record a calibs failure; return True to signal early exit."""
        if calib_result is not None and calib_result.success:
            return False
        result.failed_calibs.append(night)
        log.warning(
            f"Calibrations failed for {night}: "
            f"{getattr(calib_result, 'error', 'calibs raised')}"
        )
        if not run_cfg.continue_on_error:
            result.success = False
            result.error = f"Calibrations failed for {night}"
            return True
        return False

    if run_cfg.concurrent_nights > 1:
        log.info(
            f"Concurrent calibs: {len(all_nights)} nights, "
            f"max_workers={run_cfg.concurrent_nights}"
        )
        outcomes = _dispatch_concurrent(
            _calibs_one,
            all_nights,
            max_workers=run_cfg.concurrent_nights,
            item_label="calibs",
        )
        for night in all_nights:
            if _handle(night, outcomes.get(night)):
                return result
    else:
        # Sequential: run + handle night-by-night so a non-continue_on_error
        # failure stops before launching the remaining nights.
        for night in all_nights:
            if _handle(night, _calibs_one(night)):
                return result

    return None


def _run_science_step(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    science_cfg: "ScienceConfig",
    dry_run: bool,
    *,
    executor=None,
) -> RunResult | None:
    """Run science processing for each night.

    Returns a RunResult for early exit if continue_on_error is False, else None.
    """
    from stips.core import science

    if dry_run:
        for night in all_nights:
            if night in result.failed_calibs:
                log.info(f"Skipping science for {night} (calibrations failed)")
                result.failed_science.append(night)
                continue
            bands_for_night = list(run_cfg.bands)
            if not bands_for_night:
                log.info(f"Skipping science for {night} (no bands configured)")
                continue
            band_groups = _split_band_groups(bands_for_night)
            for group in band_groups:
                log.info(f"Running science for {night}...")
                log.info(f"  [DRY RUN] science.run({night}, bands={group})")
        return None

    # Shared per-night body: process band groups independently so one group's
    # failure (e.g. a missing narrowband flat) doesn't block the others; the
    # night succeeds if ANY group succeeds.
    def _science_one(night: str) -> bool:
        any_success = False
        for group in _split_band_groups(list(run_cfg.bands)):
            group_tag = "_".join(group)
            log.info(f"Running science for {night} bands={group}...")
            sci_log = _get_step_log_file("science", night=night, band=group_tag)
            sci_result = science.run(
                night,
                config,
                jobs=run_cfg.jobs,
                object_filter=run_cfg.object_name,
                skip_coadds=True,
                science_cfg=science_cfg,
                use_fallbacks=run_cfg.use_fallbacks,
                bands=group,
                target_ra=run_cfg.ra,
                target_dec=run_cfg.dec,
                log_file=sci_log,
                executor=executor,
            )
            _maybe_split_log(sci_log)
            if sci_result.success:
                any_success = True
                if sci_result.fallback_used:
                    log.info(
                        f"  Note: {night} bands={group} used fallback config: "
                        f"{sci_result.config_used}"
                    )
            else:
                log.warning(
                    f"Science failed for {night} bands={group}: {sci_result.error}"
                )
        return any_success

    def _skip(night: str) -> bool:
        """Skip predicate (records the skip); True => don't process this night."""
        if night in result.failed_calibs:
            log.info(f"Skipping science for {night} (calibrations failed)")
            result.failed_science.append(night)
            return True
        if not list(run_cfg.bands):
            log.info(f"Skipping science for {night} (no bands configured)")
            return True
        return False

    def _handle(night: str, ok: bool) -> bool:
        """Record a science failure; return True to signal early exit."""
        if ok:
            return False
        result.failed_science.append(night)
        if not run_cfg.continue_on_error:
            result.success = False
            result.error = f"Science failed for {night}"
            return True
        return False

    if run_cfg.concurrent_nights > 1:
        eligible = [n for n in all_nights if not _skip(n)]
        log.info(
            f"Concurrent science: {len(eligible)} nights, "
            f"max_workers={run_cfg.concurrent_nights}"
        )
        outcomes = _dispatch_concurrent(
            _science_one,
            eligible,
            max_workers=run_cfg.concurrent_nights,
            item_label="science",
        )
        for night in eligible:
            if _handle(night, bool(outcomes.get(night))):
                return result
    else:
        # Sequential: skip + run + handle night-by-night so a
        # non-continue_on_error failure stops before later nights.
        for night in all_nights:
            if _skip(night):
                continue
            if _handle(night, _science_one(night)):
                return result

    return None


def _run_dia_step(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
    *,
    executor=None,
) -> RunResult | None:
    """Run DIA per night per band.

    Returns a RunResult for early exit if continue_on_error is False, else None.
    """
    from stips.core import dia

    if dry_run:
        for night in all_nights:
            if night in result.failed_science:
                log.info(f"Skipping DIA for {night} (science processing failed)")
                bands_for_night = list(run_cfg.bands)
                for band in bands_for_night:
                    result.failed_dia.append(f"{night}/{band}")
                continue
            bands_for_night = list(run_cfg.bands)
            if not bands_for_night:
                log.info(f"Skipping DIA for {night} (no bands configured)")
                continue
            for band in bands_for_night:
                log.info(f"Running DIA for {night}/{band}...")
                log.info(f"  [DRY RUN] dia.run({night}, band={band})")
        return None

    # Resolve DIA config file paths from YAML (shared across all nights)
    subtract_cfg = None
    if run_cfg.dia_configs.subtract_images:
        subtract_cfg = config.resolve_config(run_cfg.dia_configs.subtract_images)
    detect_cfg = None
    if run_cfg.dia_configs.detect_and_measure:
        detect_cfg = config.resolve_config(run_cfg.dia_configs.detect_and_measure)

    # Shared per-(night, band) body: template lookup + dia.run + logging.
    def _dia_one_band(night: str, band: str) -> bool:
        """Run DIA for one night/band. Returns success."""
        template_coll = result.template_collections.get(band)
        if template_coll is None:
            log.warning(f"Skipping DIA for {night}/{band} (no template available)")
            return False
        log.info(f"Running DIA for {night}/{band}...")
        dia_log = _get_step_log_file("dia", night=night, band=band)
        dia_result = dia.run(
            night,
            config,
            jobs=run_cfg.jobs,
            template=template_coll,
            auto_template=False,
            prefer_ps1=run_cfg.template_type == "ps1",
            band=band,
            object_filter=run_cfg.object_name,
            subtract_config_file=subtract_cfg,
            detect_config_file=detect_cfg,
            log_file=dia_log,
            executor=executor,
        )
        _maybe_split_log(dia_log)
        if not dia_result.success:
            log.warning(f"DIA failed for {night}/{band}: {dia_result.error}")
            return False
        return True

    def _skip(night: str) -> bool:
        """Skip predicate; marks all bands failed when science failed."""
        if night in result.failed_science:
            log.info(f"Skipping DIA for {night} (science processing failed)")
            for band in list(run_cfg.bands):
                result.failed_dia.append(f"{night}/{band}")
            return True
        if not list(run_cfg.bands):
            log.info(f"Skipping DIA for {night} (no bands configured)")
            return True
        return False

    if run_cfg.concurrent_nights > 1:

        def _dia_one(night: str) -> list[str]:
            """All bands for one night. Returns failed night/band keys."""
            return [
                f"{night}/{band}"
                for band in list(run_cfg.bands)
                if not _dia_one_band(night, band)
            ]

        eligible = [n for n in all_nights if not _skip(n)]
        log.info(
            f"Concurrent DIA: {len(eligible)} nights, "
            f"max_workers={run_cfg.concurrent_nights}"
        )
        outcomes = _dispatch_concurrent(
            _dia_one,
            eligible,
            max_workers=run_cfg.concurrent_nights,
            item_label="dia",
        )
        for night in eligible:
            failed_pairs = outcomes.get(night)
            if failed_pairs is None:
                # Thread raised an exception — mark all bands failed
                failed_pairs = [f"{night}/{b}" for b in list(run_cfg.bands)]
            result.failed_dia.extend(failed_pairs)
            if not run_cfg.continue_on_error and failed_pairs:
                result.success = False
                result.error = f"DIA failed for {failed_pairs[0]}"
                return result
    else:
        # Sequential: per-band. A missing template is skipped gracefully (it
        # never triggers the continue_on_error abort) — only a real dia.run
        # failure does, matching the pre-refactor behavior. (The concurrent
        # path above treats both as a night failure, as it did before.)
        for night in all_nights:
            if _skip(night):
                continue
            for band in list(run_cfg.bands):
                if result.template_collections.get(band) is None:
                    log.warning(
                        f"Skipping DIA for {night}/{band} (no template available)"
                    )
                    result.failed_dia.append(f"{night}/{band}")
                    continue
                if not _dia_one_band(night, band):
                    result.failed_dia.append(f"{night}/{band}")
                    if not run_cfg.continue_on_error:
                        result.success = False
                        result.error = f"DIA failed for {night}/{band}"
                        return result

    return None


def _run_fphot_step(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
    *,
    executor=None,
) -> None:
    """Run forced photometry per night per successful DIA band."""
    from stips.core import fphot

    failed_dia_set = set(result.failed_dia)

    if dry_run:
        for night in all_nights:
            bands_for_night = list(run_cfg.bands)
            successful_bands = [
                b for b in bands_for_night if f"{night}/{b}" not in failed_dia_set
            ]
            if not successful_bands:
                log.info(
                    f"Skipping forced photometry for {night} (all DIA bands failed)"
                )
                result.failed_fphot.append(night)
                continue
            for band in successful_bands:
                log.info(f"Running forced photometry for {night}/{band}...")
                log.info(
                    f"  [DRY RUN] fphot.run({night}, band={band}, "
                    f"image_type={run_cfg.forced_phot_image_type})"
                )
        return

    def _ok_bands(night: str) -> list[str]:
        return [b for b in list(run_cfg.bands) if f"{night}/{b}" not in failed_dia_set]

    def _skip(night: str) -> bool:
        """Skip predicate; records the night failed when no DIA band survived."""
        if not _ok_bands(night):
            log.info(f"Skipping forced photometry for {night} (all DIA bands failed)")
            result.failed_fphot.append(night)
            return True
        return False

    def _fphot_one(night: str) -> tuple[list[str], bool]:
        """All surviving bands for one night. Returns (output_collections, had_failure)."""
        colls: list[str] = []
        had_failure = False
        for band in _ok_bands(night):
            log.info(f"Running forced photometry for {night}/{band}...")
            fphot_log = _get_step_log_file("fphot", night=night, band=band)
            fphot_result = fphot.run(
                night=night,
                ra=run_cfg.ra,
                dec=run_cfg.dec,
                config=config,
                band=band,
                image_type=run_cfg.forced_phot_image_type,
                jobs=run_cfg.jobs,
                log_file=fphot_log,
                executor=executor,
            )
            _maybe_split_log(fphot_log)
            if not fphot_result.success:
                had_failure = True
                log.warning(
                    f"Forced phot failed for {night}/{band}: {fphot_result.error}"
                )
            else:
                colls.extend(fphot_result.output_collections)
        return (colls, had_failure)

    def _record(night: str, res: tuple[list[str], bool] | None) -> None:
        if res is None:  # thread raised
            result.failed_fphot.append(night)
            return
        colls, had_failure = res
        if colls:
            result.forced_phot_collections[night] = colls
        if had_failure:
            result.failed_fphot.append(night)

    if run_cfg.concurrent_nights > 1:
        eligible = [n for n in all_nights if not _skip(n)]
        log.info(
            f"Concurrent fphot: {len(eligible)} nights, "
            f"max_workers={run_cfg.concurrent_nights}"
        )
        outcomes = _dispatch_concurrent(
            _fphot_one,
            eligible,
            max_workers=run_cfg.concurrent_nights,
            item_label="fphot",
        )
        for night in eligible:
            _record(night, outcomes.get(night))
    else:
        for night in all_nights:
            if _skip(night):
                continue
            _record(night, _fphot_one(night))


def _run_lightcurve_step(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
) -> None:
    """Extract lightcurve from forced photometry or DIA sources."""
    from stips.core import dataset_types, lightcurve

    use_forced_phot = run_cfg.lc_config.dataset_type.startswith(
        dataset_types.FORCED_PHOT_PREFIX
    )

    if use_forced_phot:
        collections_list = _discover_fphot_collections(
            all_nights, run_cfg, config, result, dry_run
        )
    else:
        collections_list = _discover_dia_collections(
            all_nights, run_cfg, config, result, dry_run
        )

    if not collections_list:
        source_type = "forced photometry" if use_forced_phot else "DIA"
        log.warning(
            f"No {source_type} collections found, skipping lightcurve extraction"
        )
        return

    collections = ",".join(collections_list)
    source_label = "forced phot" if use_forced_phot else "diff"
    log.info(f"Lightcurve using {len(collections_list)} {source_label} collections")

    if not dry_run:
        lc_type = "forced_phot" if use_forced_phot else "dia_sources"
        lc_log = _get_step_log_file("lightcurve", night=lc_type)
        lc_result = lightcurve.run(
            ra=run_cfg.ra,
            dec=run_cfg.dec,
            collections=collections,
            config=config,
            name=run_cfg.object_name,
            plot=True,
            log_file=lc_log,
            lc_config=run_cfg.lc_config,
        )
        if lc_result.success:
            result.lightcurve_path = lc_result.csv_path
        else:
            log.warning(f"Lightcurve extraction failed: {lc_result.error}")
    else:
        log.info("  [DRY RUN] lightcurve.run()")


def _run_period_step(
    run_cfg: RunConfig,
    result: RunResult,
    dry_run: bool,
) -> None:
    """Run period analysis on extracted lightcurve."""
    if not result.lightcurve_path:
        log.warning("No lightcurve available, skipping period search")
        return

    if not dry_run:
        from stips.core import period

        period_log = _get_step_log_file("period")
        period_result = period.run(
            csv_path=Path(result.lightcurve_path),
            period_min=run_cfg.period_min,
            period_max=run_cfg.period_max,
            n_samples=run_cfg.period_samples,
            output_dir=Path(result.lightcurve_path).parent / "period_analysis",
            log_file=period_log,
        )
        result.period_result_path = str(period_result.output_dir)
        log.info(
            f"  Best period: {period_result.best_period:.6f} d "
            f"(FAP={period_result.fap:.2e})"
        )
    else:
        log.info("  [DRY RUN] period.run()")


def _run_transit_step(
    run_cfg: RunConfig,
    result: RunResult,
    dry_run: bool,
) -> None:
    """Run BLS transit search on extracted lightcurve."""
    if not result.lightcurve_path:
        log.warning("No lightcurve available, skipping transit search")
        return

    if not dry_run:
        from stips.core import transit

        transit_log = _get_step_log_file("transit")
        transit_result = transit.run(
            csv_path=Path(result.lightcurve_path),
            period_min=run_cfg.period_min,
            period_max=run_cfg.period_max,
            duration_min=run_cfg.transit_duration_min,
            duration_max=run_cfg.transit_duration_max,
            n_samples=run_cfg.period_samples,
            output_dir=Path(result.lightcurve_path).parent / "transit_analysis",
            log_file=transit_log,
        )
        result.transit_result_path = str(transit_result.output_dir)
        log.info(
            f"  Transit: P={transit_result.best_period:.4f} d, "
            f"depth={transit_result.depth*100:.3f}%, "
            f"SNR={transit_result.transit_snr:.1f}"
        )
    else:
        log.info("  [DRY RUN] transit.run()")


def _run_differential_phot_step(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
) -> None:
    """Run LSST differential aperture photometry pipeline.

    For bright stars where PSF-fitting forced photometry is unreliable,
    this runs DifferentialPhotTask which reads aperture fluxes from
    calibrateImage star catalogs, selects comparison stars, and computes
    differential flux ratios.
    """
    from stips.core import butler_query
    from stips.core.pipeline import CollectionNames
    from stips.core.stack import run_pipetask

    prof = config.require_profile()
    repo = str(config.repo)

    # Discover science collection via Butler (consistent with fphot.py pattern)
    science_coll = None
    for night in all_nights:
        colls = butler_query.list_collections(
            config,
            f"{prof.collection_prefix}/runs/{night}/processCcd/*",
            prefix=f"{prof.collection_prefix}/",
        )
        if colls:
            # Prefer CHAINED parents over individual RUNs
            chained = [
                c for c in colls if not c.endswith(("/run",)) and "/run_fb" not in c
            ]
            if chained:
                science_coll = sorted(chained)[-1]
            else:
                science_coll = sorted(colls)[-1]
            break

    if not science_coll:
        log.warning("No science collection found, skipping differential photometry")
        result.differential_phot_success = False
        return

    log.info(f"Running LSST differential photometry on {science_coll}")

    pipeline_yaml = str(config.resolve_pipeline("DifferentialPhot.yaml"))
    input_colls = (
        f"{science_coll},{prof.collection_prefix}/calib/current,"
        f"refcats,{prof.skymap_collection}"
    )
    output_coll = CollectionNames(
        all_nights[0], prefix=prof.collection_prefix
    ).differential_phot

    bands = run_cfg.bands
    band_filter = bands[0] if len(bands) == 1 else ""

    # Build pipetask run arguments
    run_args = [
        "run",
        "-b",
        repo,
        "-p",
        pipeline_yaml,
        "-i",
        input_colls,
        "-o",
        output_coll,
        "-c",
        f"differentialPhot:targetRa={run_cfg.ra}",
        "-c",
        f"differentialPhot:targetDec={run_cfg.dec}",
        "-c",
        f"differentialPhot:targetName={run_cfg.object_name}",
        "--register-dataset-types",
        "--clobber-outputs",
    ]
    if band_filter:
        run_args.extend(["-c", f"differentialPhot:bandFilter={band_filter}"])

    if not dry_run:
        log_file = _get_step_log_file("differential_phot")
        try:
            proc = run_pipetask(
                run_args,
                config,
                check=False,
                log_file=log_file,
            )
            if proc.returncode == 0:
                log.info("  Differential photometry pipeline complete")
                result.differential_phot_success = True
            else:
                log.error(
                    "  Differential photometry pipeline failed (exit code %d)",
                    proc.returncode,
                )
                result.differential_phot_success = False
        except Exception as e:
            log.error(f"Differential photometry failed: {e}")
            result.differential_phot_success = False
    else:
        log.info(
            f"  [DRY RUN] pipetask run -p DifferentialPhot.yaml "
            f"-c differentialPhot:targetRa={run_cfg.ra} "
            f"-c differentialPhot:targetDec={run_cfg.dec}"
        )


def _discover_fphot_collections(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
) -> list[str]:
    """Gather forced photometry collections for lightcurve extraction."""
    from stips.core import butler_query
    from stips.core.pipeline import CollectionNames

    prof = config.require_profile()
    fphot_colls: list[str] = []
    for night in all_nights:
        colls = result.forced_phot_collections.get(night, [])
        if colls:
            fphot_colls.extend(colls)

    # If no forced phot ran this session, discover from Butler
    if not fphot_colls and not dry_run:
        log.info("No forced phot from this run, discovering from Butler...")
        # When image_type is "both", search for both visit and diffim patterns
        if run_cfg.forced_phot_image_type == "both":
            fphot_suffixes = ["visit", "diffim"]
        else:
            fphot_suffixes = [run_cfg.forced_phot_image_type]
        for night in all_nights:
            for fphot_suffix in fphot_suffixes:
                try:
                    fphot_colls.extend(
                        butler_query.list_collections(
                            config,
                            CollectionNames.forced_phot_glob(
                                prof.collection_prefix,
                                night=night,
                                tail=f"*/{fphot_suffix}*",
                            ),
                            prefix=f"{prof.collection_prefix}/runs/",
                        )
                        or []
                    )
                except Exception as e:
                    log.debug(
                        f"Failed to discover fphot collections for {night}/{fphot_suffix}: {e}"
                    )
    elif not fphot_colls and dry_run:
        for night in all_nights:
            fphot_colls.append(
                CollectionNames.forced_phot_glob(
                    prof.collection_prefix, night=night, tail="*/run"
                )
            )

    return sorted(set(fphot_colls))


def _discover_dia_collections(
    all_nights: list[str],
    run_cfg: RunConfig,
    config: Config,
    result: RunResult,
    dry_run: bool,
) -> list[str]:
    """Gather DIA diff collections for lightcurve extraction."""
    from stips.core import butler_query

    prof = config.require_profile()
    verified: list[str] = []
    failed_night_bands = set(result.failed_dia)

    for night in all_nights:
        bands_for_night = list(run_cfg.bands)
        has_success = any(
            f"{night}/{b}" not in failed_night_bands for b in bands_for_night
        )
        if not has_success:
            continue

        if not dry_run:
            try:
                verified.extend(
                    butler_query.list_collections(
                        config,
                        f"{prof.collection_prefix}/runs/{night}/diff/*/run",
                        prefix=f"{prof.collection_prefix}/runs/",
                    )
                    or []
                )
            except Exception:
                log.debug(f"Could not verify diff collection for {night}")
        else:
            verified.append(f"{prof.collection_prefix}/runs/{night}/diff/*/run")

    return verified


def _run_refcat_step(run_cfg, config, result, dry_run):
    """Ensure Gaia/PS1 refcats cover the target before science/templates.

    On-demand and idempotent: a no-op when coverage already exists or when
    ``refcat_mode == "monster"``. Requires a target RA/Dec.
    """
    if run_cfg.ra is None or run_cfg.dec is None:
        return None
    if dry_run:
        log.info(
            "[DRY RUN] Would ensure refcats (mode=%s, radius=%.3f deg)",
            run_cfg.refcat_mode,
            run_cfg.refcat_radius_deg,
        )
        return None

    refcat_result = ensure_refcats(
        config,
        run_cfg.ra,
        run_cfg.dec,
        radius_deg=run_cfg.refcat_radius_deg,
        mode=run_cfg.refcat_mode,
        gaia_quality=run_cfg.refcat_gaia_quality,
    )
    log.info(
        "Refcat (%s): gaia=%s, ps1=%s",
        refcat_result.mode,
        refcat_result.gaia_status,
        refcat_result.ps1_status,
    )
    failed = [
        name
        for name, status in (
            ("gaia", refcat_result.gaia_status),
            ("ps1", refcat_result.ps1_status),
        )
        if status == "failed"
    ]
    if failed:
        # Without reference catalogs, calibrateImage cannot build a quantum
        # graph — every science night would fail downstream with an opaque
        # MissingDatasetTypeError. Fail here, loudly, with the root cause.
        result.success = False
        result.error = (
            f"Refcat ensure failed for {', '.join(failed)}: {refcat_result.error}. "
            "Science calibration cannot proceed without reference catalogs — "
            "fix the root cause above (e.g. missing astroquery, no network) "
            "or rerun with refcat mode 'monster' if the repo already has refcats."
        )
        log.error(result.error)
        return result
    if refcat_result.error:
        log.warning("Refcat issues: %s", refcat_result.error)
    return None


def run(
    config_file: Path,
    config: Config,
    *,
    dry_run: bool = False,
    site_override: str | None = None,
    concurrent_override: int | None = None,
) -> RunResult:
    """Run full pipeline from YAML configuration.

    This orchestrates:
    0. Bootstrap repository if needed (auto-detected)
    1. PS1 template ingestion (or coadd building) per band
    2. Calibrations per night
    3. Science processing per night
    4. DIA per night per band
    5. Forced photometry per night
    6. Lightcurve extraction
    7. Period analysis (variable stars, if period_search=true)

    Args:
        config_file: Path to YAML configuration file
        config: Pipeline configuration
        dry_run: Print commands without executing
        site_override: Override execution site (implies BPS execution)
        concurrent_override: Override concurrent_nights value

    Returns:
        RunResult with status and any failures
    """
    from stips.core import bootstrap
    from stips.core.science import ScienceConfig

    # Set up unified logging directory for this pipeline run
    run_id = _generate_run_id()
    run_log_dir = _setup_run_logging(run_id, config)
    log.info(f"Logs: {run_log_dir}")

    # Step 0: Check if bootstrap is needed
    if bootstrap.needs_bootstrap(config):
        log.info(f"Repository not initialized, running bootstrap: {config.repo}")
        if not dry_run:
            bootstrap_log = _get_step_log_file("bootstrap")
            bootstrap_result = bootstrap.run(config, log_file=bootstrap_log)
            if not bootstrap_result.success:
                return RunResult(
                    success=False,
                    error=f"Bootstrap failed: {bootstrap_result.error}",
                    log_dir=str(run_log_dir),
                )
            log.info("Bootstrap complete")
        else:
            log.info("[DRY RUN] Would run bootstrap")

    # Load run configuration
    run_cfg = RunConfig.from_yaml(config_file)

    # Apply CLI overrides
    if site_override:
        run_cfg.execution = "bps"
        run_cfg.site = site_override
    if concurrent_override is not None:
        run_cfg.concurrent_nights = concurrent_override

    executor = _create_executor(run_cfg)
    log.info(f"Execution: {run_cfg.execution} (site={run_cfg.site})")

    # Build ScienceConfig from YAML paths
    science_cfg = ScienceConfig.default(config)
    science_cfg.refcat_mode = run_cfg.refcat_mode
    if run_cfg.science_configs.calibrate_image:
        science_cfg.calibrate_image = config.resolve_config(
            run_cfg.science_configs.calibrate_image
        )
    if run_cfg.science_configs.colorterms:
        science_cfg.colorterms = config.resolve_config(
            run_cfg.science_configs.colorterms
        )
    if run_cfg.science_configs.calibrate_image_fallbacks:
        science_cfg.calibrate_image_fallbacks = [
            config.resolve_config(fb)
            for fb in run_cfg.science_configs.calibrate_image_fallbacks
        ]

    result = RunResult(success=True, log_dir=str(run_log_dir))
    all_nights = list(run_cfg.nights)

    log.info(f"Pipeline run for {run_cfg.object_name or '(survey mode)'}")
    if run_cfg.ra is not None and run_cfg.dec is not None:
        log.info(f"  Target: RA={run_cfg.ra:.4f}, Dec={run_cfg.dec:.4f}")
    log.info(f"  Bands: {run_cfg.bands}")
    log.info(f"  Template type: {run_cfg.template_type}")
    log.info(f"  Nights: {len(all_nights)}")
    if run_cfg.template_type == "coadd" and run_cfg.template_nights:
        log.info(f"  Template nights: {run_cfg.template_nights}")

    if dry_run:
        log.info("[DRY RUN] Commands would be executed:")

    # Step 0b: Ensure reference catalogs (on-demand Gaia/PS1; no RSP/MONSTER).
    # Runs before templates because coadd templates also consume refcats.
    early_exit = _run_refcat_step(run_cfg, config, result, dry_run)
    if early_exit is not None:
        return early_exit

    # Step 1: Templates per band
    early_exit = _run_template_step(
        run_cfg, config, result, science_cfg, dry_run, executor=executor
    )
    if early_exit is not None:
        return early_exit

    # Step 2: Calibrations per night (always local — BPS overhead too high
    # for small calib pipelines, and calib qgraphs lack --output-run)
    if not run_cfg.skip_calibs:
        early_exit = _run_calibs_step(
            all_nights,
            run_cfg,
            config,
            result,
            dry_run,
        )
        if early_exit is not None:
            return early_exit

    # Step 3: Science per night
    if not run_cfg.skip_science:
        early_exit = _run_science_step(
            all_nights,
            run_cfg,
            config,
            result,
            science_cfg,
            dry_run,
            executor=executor,
        )
        if early_exit is not None:
            return early_exit

    # Step 4: DIA per night per band
    if not run_cfg.skip_dia:
        early_exit = _run_dia_step(
            all_nights,
            run_cfg,
            config,
            result,
            dry_run,
            executor=executor,
        )
        if early_exit is not None:
            return early_exit

    # Step 5: Forced photometry per night per successful DIA band
    if run_cfg.forced_phot:
        _run_fphot_step(
            all_nights,
            run_cfg,
            config,
            result,
            dry_run,
            executor=executor,
        )

    # Step 6: Lightcurve extraction
    if run_cfg.lc_config.enabled:
        log.info("Extracting lightcurve...")
        _run_lightcurve_step(all_nights, run_cfg, config, result, dry_run)

    # Step 6b: Differential aperture photometry (transit targets)
    if run_cfg.pipeline_type == "transit":
        log.info("Running differential aperture photometry...")
        _run_differential_phot_step(all_nights, run_cfg, config, result, dry_run)

    # Step 7a: Period analysis (variable stars)
    if run_cfg.period_search:
        log.info("Running period analysis...")
        _run_period_step(run_cfg, result, dry_run)

    # Step 7b: Transit search (exoplanets)
    if run_cfg.transit_search:
        log.info("Running transit search...")
        _run_transit_step(run_cfg, result, dry_run)

    # Determine overall success
    # Use a three-tier status: SUCCESS (no failures), PARTIAL (some failures but
    # usable results like lightcurves or successful nights), FAILED (nothing worked)
    has_failures = bool(
        result.failed_calibs
        or result.failed_science
        or result.failed_dia
        or result.failed_fphot
        or result.differential_phot_success is False
    )
    has_successes = False

    if has_failures:
        # Check if we got any usable results despite failures
        total_nights = len(all_nights)
        successful_science = total_nights - len(result.failed_science)
        successful_dia_pairs = sum(
            1
            for night in all_nights
            for b in list(run_cfg.bands)
            if f"{night}/{b}" not in set(result.failed_dia)
        )
        successful_fphot = total_nights - len(result.failed_fphot)

        has_successes = (
            successful_science > 0
            or successful_dia_pairs > 0
            or successful_fphot > 0
            or result.lightcurve_path is not None
            or result.period_result_path is not None
            or result.transit_result_path is not None
        )

        if has_successes:
            result.success = True  # Partial success is still success
        else:
            result.success = False

        failures = []
        if result.failed_calibs:
            failures.append(f"calibs: {result.failed_calibs}")
        if result.failed_science:
            failures.append(f"science: {result.failed_science}")
        if result.failed_dia:
            failures.append(f"dia: {result.failed_dia}")
        if result.failed_fphot:
            failures.append(f"fphot: {result.failed_fphot}")
        result.error = "; ".join(failures)

    # Post-process: split any remaining unsplit logs by exposure
    if not dry_run:
        _split_step_logs(run_log_dir)

    # Write summary and log final location
    if not has_failures:
        status = "SUCCESS"
    elif has_successes:
        status = "PARTIAL SUCCESS"
    else:
        status = "FAILED"
    log.info(f"Pipeline {status}")
    log.info(f"All logs: {run_log_dir}")

    # Log success/failure counts
    total_nights = len(all_nights)
    n_calibs_ok = total_nights - len(result.failed_calibs)
    n_science_ok = total_nights - len(result.failed_science)
    n_fphot_ok = total_nights - len(result.failed_fphot)
    total_dia_pairs = sum(len(list(run_cfg.bands)) for night in all_nights)
    n_dia_ok = total_dia_pairs - len(result.failed_dia)
    info_parts = [
        f"Calibs: {n_calibs_ok}/{total_nights}",
        f"Science: {n_science_ok}/{total_nights}",
    ]
    if not run_cfg.skip_dia:
        info_parts.append(f"DIA: {n_dia_ok}/{total_dia_pairs}")
    if run_cfg.forced_phot:
        info_parts.append(f"Fphot: {n_fphot_ok}/{total_nights}")
    log.info("  " + ", ".join(info_parts))
    if result.differential_phot_success is not None:
        dp_status = "OK" if result.differential_phot_success else "FAILED"
        log.info(f"  Differential phot: {dp_status}")
    if result.lightcurve_path:
        log.info(f"  Lightcurve: {result.lightcurve_path}")
    if result.period_result_path:
        log.info(f"  Period analysis: {result.period_result_path}")
    if result.transit_result_path:
        log.info(f"  Transit analysis: {result.transit_result_path}")

    summary_file = run_log_dir / "summary.txt"
    with open(summary_file, "w") as f:
        f.write(f"Status: {status}\n")
        f.write(f"Object: {run_cfg.object_name}\n")
        f.write(f"Bands: {run_cfg.bands}\n")
        f.write(f"Nights: {total_nights}\n")
        f.write(f"Calibs OK: {n_calibs_ok}/{total_nights}\n")
        f.write(f"Science OK: {n_science_ok}/{total_nights}\n")
        if not run_cfg.skip_dia:
            f.write(f"DIA OK: {n_dia_ok}/{total_dia_pairs}\n")
        if run_cfg.forced_phot:
            f.write(f"Fphot OK: {n_fphot_ok}/{total_nights}\n")
        if result.failed_calibs:
            f.write(f"Failed calibs: {result.failed_calibs}\n")
        if result.failed_science:
            f.write(f"Failed science: {result.failed_science}\n")
        if not run_cfg.skip_dia and result.failed_dia:
            f.write(f"Failed DIA: {result.failed_dia}\n")
        if run_cfg.forced_phot and result.failed_fphot:
            f.write(f"Failed fphot: {result.failed_fphot}\n")
        if result.differential_phot_success is not None:
            dp_status = "OK" if result.differential_phot_success else "FAILED"
            f.write(f"Differential phot: {dp_status}\n")
        if result.template_collections:
            f.write(f"Templates: {result.template_collections}\n")
        if result.lightcurve_path:
            f.write(f"Lightcurve: {result.lightcurve_path}\n")
        if result.period_result_path:
            f.write(f"Period analysis: {result.period_result_path}\n")
        if result.transit_result_path:
            f.write(f"Transit analysis: {result.transit_result_path}\n")

    return result
