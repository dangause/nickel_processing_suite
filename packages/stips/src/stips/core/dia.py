"""Difference Image Analysis (DIA) pipeline."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from stips.core import butler_query, dataset_types, quanta_report
from stips.core.pipeline import (
    REFCATS_CHAIN,
    CollectionNames,
    PipetaskStage,
    build_exclusion_expr,
    ensure_instrument_registered,
    is_empty_qgraph,
    latest_raw_run,
    night_day_obs_expr,
    parse_bad_exposures,
    ps1_band_map,
    redefine_chain,
    resolve_processccd_collections,
    template_deep_glob,
    template_external_glob,
    template_ps1,
    validate_night,
)
from stips.core.query import butler_str_literal

if TYPE_CHECKING:
    from stips.core.config import Config

log = logging.getLogger(__name__)


@dataclass
class DIAResult:
    """Result of difference imaging for a single night/band.

    Attributes:
        success: Whether DIA produced at least one difference image.
        night: Observing night (YYYYMMDD).
        diff_run: Butler RUN collection containing DIA outputs.
        template_collection: Template collection used for subtraction.
        diff_image_count: Number of difference images produced.
        dia_source_count: Number of DIA sources detected.
        error: Error message if processing failed, None on success.
    """

    success: bool
    night: str
    diff_run: str
    template_collection: str | None
    diff_image_count: int = 0
    dia_source_count: int = 0
    error: str | None = None


def find_template(
    config: Config,
    band: str | None = None,
    prefer_ps1: bool = False,
    strategy: str | None = None,
) -> str | None:
    """Auto-discover the best template collection.

    Args:
        config: Pipeline configuration
        band: Filter by band (b/v/r/i)
        prefer_ps1: Prefer PS1 templates over internal (legacy flag)
        strategy: "auto" picks per band — PS1 for the profile's PS1-eligible
            bands (if ingested), coadd otherwise. None/"ps1"/"coadd" use the
            legacy preference-based discovery.

    Returns:
        Template collection name, or None if not found
    """
    # Per-band auto: PS1 for the profile's PS1-eligible bands when available,
    # coadd for the rest.
    if strategy == "auto":
        if band in ps1_band_map(config) and butler_query.collection_exists(
            config, template_ps1(band)
        ):
            return template_ps1(band)
        coadds = (
            butler_query.list_collections(
                config, template_deep_glob(), prefix="templates/"
            )
            or []
        )
        if band:
            coadds = [c for c in coadds if c.endswith(f"/{band}")]
        return coadds[0] if coadds else None

    # Query coadd templates plus EVERY registered external survey, so adding a
    # source is one sources/*.py file with no edit here (the extension contract
    # in docs/architecture.md).
    #
    # NOTE: the strategy == "auto" branch above deliberately considers only PS1,
    # by name. It must NOT become registry-driven: SkyMapper templates are
    # single-epoch, ~2" seeing and at most 10' wide, so auto-selecting one would
    # hand DIA a template shallower and blurrier than the science image, and a
    # future adapter is no safer by default. External sources other than PS1 are
    # reachable only by explicit --template / template.type, or this legacy
    # discovery path.
    from stips.pipeline_tools.external_template.sources import SOURCES

    patterns = [template_external_glob(name) for name in sorted(SOURCES)]
    patterns.append(template_deep_glob())

    candidates = []
    for pattern in patterns:
        candidates.extend(
            butler_query.list_collections(config, pattern, prefix="templates/") or []
        )

    if not candidates:
        return None

    # Filter by band if specified
    if band:
        candidates = [c for c in candidates if c.endswith(f"/{band}")]

    if not candidates:
        return None

    # Sort by preference
    if prefer_ps1:
        ps1 = [c for c in candidates if "ps1" in c.lower()]
        if ps1:
            return ps1[0]
    else:
        internal = [c for c in candidates if "templates/deep" in c]
        if internal:
            return internal[0]

    return candidates[0]


def run(
    night: str,
    config: Config,
    *,
    jobs: int = 8,
    template: str | None = None,
    auto_template: bool = False,
    prefer_ps1: bool = False,
    band: str | None = None,
    object_filter: str | None = None,
    bad_exposures: str | None = None,
    bad_file: Path | None = None,
    subtract_config_file: Path | None = None,
    detect_config_file: Path | None = None,
    log_file: Path | None = None,
    executor=None,
) -> DIAResult:
    """Run difference imaging for a night.

    Args:
        night: Observing night (YYYYMMDD)
        config: Pipeline configuration
        jobs: Number of parallel jobs
        template: Template collection to use
        auto_template: Auto-discover template
        prefer_ps1: Prefer PS1 templates (with auto_template)
        band: Filter by band (b/v/r/i)
        object_filter: Filter by OBJECT header value
        bad_exposures: Comma-separated exposure IDs to exclude
        bad_file: File with exposure IDs to exclude
        log_file: Optional path to write LSST pipeline logs

    Returns:
        DIAResult with collection names and counts
    """
    from stips.core.executor import LocalExecutor

    if executor is None:
        executor = LocalExecutor()

    prof = config.require_profile()
    night = validate_night(night)
    cols = CollectionNames(night, prefix=prof.collection_prefix)
    repo = str(config.repo)
    # A Lick observing night spans two UT days (pre-/post-midnight); include
    # both so pre-midnight exposures are not dropped from difference imaging.
    day_obs_expr = night_day_obs_expr(night, prof)

    # Resolve template
    template_collection = template
    if auto_template:
        template_collection = find_template(config, band=band, prefer_ps1=prefer_ps1)

    if not template_collection:
        return DIAResult(
            success=False,
            night=night,
            diff_run=cols.diff_run,
            template_collection=None,
            error="No template specified. Use --template or --auto-template",
        )

    # Find science collection (targeted query instead of fetching all collections)
    # Prefer the CHAINED parent over individual RUN collections, since the
    # CHAINED parent includes results from both primary and fallback configs.
    try:
        # Prefer CHAINED parents over individual RUNs, and join ALL CHAINED
        # parents so DIA sees science outputs from every band group (broadband +
        # narrowband are processed separately). Band groups are disjoint by
        # filter, so no duplicate datasets across parents.
        parent_collections = resolve_processccd_collections(
            config, night, all_parents=True
        )
        sci_parents = ",".join(parent_collections) if parent_collections else None

        if not sci_parents:
            return DIAResult(
                success=False,
                night=night,
                diff_run=cols.diff_run,
                template_collection=template_collection,
                error=f"No science collection found for night {night}. Run science processing first.",
            )
    except Exception as e:
        return DIAResult(
            success=False,
            night=night,
            diff_run=cols.diff_run,
            template_collection=template_collection,
            error=f"Failed to query collections: {e}",
        )

    # Build query
    bad_ids = parse_bad_exposures(bad_exposures, bad_file)
    exclusion_expr = build_exclusion_expr(bad_ids)

    object_expr = ""
    if object_filter:
        object_expr = f" AND exposure.target_name={butler_str_literal(object_filter)}"

    band_expr = ""
    if band:
        band_expr = f" AND band={butler_str_literal(band)}"

    data_query = (
        f"instrument='{prof.name}' AND exposure.observation_type='science' "
        f"AND {day_obs_expr}{object_expr}{exclusion_expr}{band_expr}"
    )

    # Pipeline and config paths
    pipeline = config.resolve_pipeline("DIA.yaml")
    subtract_config = subtract_config_file or (
        config.resolve_config("dia/subtractImages.py")
    )
    detect_config = detect_config_file or (
        config.resolve_config("dia/detectAndMeasure.py")
    )

    try:
        # Register instrument
        ensure_instrument_registered(config, log_file)

        # Build quantum graph
        qg_dir = config.repo / "qgraphs"
        qg_dir.mkdir(parents=True, exist_ok=True)
        qg_file = qg_dir / f"diff_{night}_{cols.run_ts}.qg"

        # Find raw collection (optional for DIA, targeted query). Use the newest
        # raw ingest so a re-ingest supersedes a stale earlier one.
        raw_run = latest_raw_run(config, night) or ""

        input_collections = f"{sci_parents},{cols.calib_chain},{REFCATS_CHAIN},{prof.skymap_collection},{template_collection}"
        if raw_run:
            input_collections = f"{sci_parents},{raw_run},{cols.calib_chain},{REFCATS_CHAIN},{prof.skymap_collection},{template_collection}"

        # Per-stage --config-file overrides go after -d (see PipetaskStage).
        dia_config_args: list[str] = []
        if subtract_config.exists():
            dia_config_args += ["--config-file", f"subtractImages:{subtract_config}"]
        if detect_config.exists():
            dia_config_args += [
                "--config-file",
                f"detectAndMeasureDiaSource:{detect_config}",
            ]

        summary_file = qg_file.with_suffix(".summary.json")
        stage = PipetaskStage(
            repo=repo,
            pipeline=f"{pipeline}#dia-full",
            inputs=input_collections,
            output_parent=cols.diff_parent,
            output_run=cols.diff_run,
            qgraph_path=str(qg_file),
            data_query=data_query,
            post_query_args=dia_config_args,
            jobs=jobs,
            run_includes_output_run=True,
            summary_file=str(summary_file),
        )

        qg_result = executor.run_pipetask(
            stage.qgraph_args(),
            config,
            capture_output=True,
            check=False,
            log_file=log_file,
        )

        # Check for empty quantum graph (no matching data for this night/band).
        # Prefer a structural check on the saved qgraph (len(qg) == 0) over
        # grepping the human-readable "QuantumGraph contains no quanta" log line,
        # which is not a stable API. Fall back to the stdout grep only if the
        # structural check could not run (e.g. file missing / older stack).
        combined_qg_output = (qg_result.stdout or "") + (qg_result.stderr or "")
        empty_qg = butler_query.quantum_graph_is_empty(config, qg_file)
        if empty_qg is None:
            empty_qg = is_empty_qgraph(combined_qg_output)
        if empty_qg:
            log.warning(
                f"Empty quantum graph for {night}/{band or 'all'} — "
                f"no matching science data found for DIA"
            )
            return DIAResult(
                success=False,
                night=night,
                diff_run=cols.diff_run,
                template_collection=template_collection,
                error=f"No matching science data for DIA (empty quantum graph). "
                f"Query: {data_query}",
            )

        if qg_result.returncode != 0:
            return DIAResult(
                success=False,
                night=night,
                diff_run=cols.diff_run,
                template_collection=template_collection,
                error=f"QGraph build failed: {qg_result.stderr or qg_result.stdout}",
            )

        # Run DIA. --summary writes a machine-readable quanta report (preferred
        # over the stdout "Executed N quanta..." regex; see below).
        dia_result = executor.run_pipetask(
            stage.run_args(),
            config,
            capture_output=True,
            check=False,
            log_file=log_file,
            output_run=cols.diff_run,
        )

        # Parse quanta counts to handle partial success.
        combined_output = (dia_result.stdout or "") + (dia_result.stderr or "")
        quanta_ok, quanta_fail = quanta_report.counts(
            summary_file, combined_output, log_file=log_file
        )

        if dia_result.returncode != 0 and quanta_ok == 0:
            return DIAResult(
                success=False,
                night=night,
                diff_run=cols.diff_run,
                template_collection=template_collection,
                error=f"DIA pipeline failed: {dia_result.stderr or dia_result.stdout}",
            )

        if quanta_fail > 0:
            log.warning(
                f"DIA partial success for {night}/{band or 'all'}: "
                f"{quanta_ok} quanta succeeded, {quanta_fail} failed"
            )

        # Verify the RUN collection exists before chaining.
        # BPS may report success even when all quanta failed, leaving
        # no RUN collection in the Butler.
        if not butler_query.collection_exists(config, cols.diff_run):
            return DIAResult(
                success=False,
                night=night,
                diff_run=cols.diff_run,
                template_collection=template_collection,
                error="DIA RUN collection was not created (all quanta may have failed)",
            )

        # Update collection chain
        redefine_chain(config, cols.diff_parent, cols.diff_run, log_file=log_file)

        # Count outputs via the Butler Python query API (structured JSON) instead
        # of parsing query-datasets tabular stdout. A failed query yields None,
        # which we treat as 0 to preserve the prior best-effort behavior.
        where = f"instrument='{prof.name}' AND {day_obs_expr}"
        diff_count = (
            butler_query.count_datasets(
                config, dataset_types.DIFFERENCE_IMAGE, cols.diff_run, where=where
            )
            or 0
        )
        src_count = (
            butler_query.count_datasets(
                config,
                dataset_types.DIA_SOURCE_UNFILTERED,
                cols.diff_run,
                where=where,
            )
            or 0
        )

        # If pipeline "succeeded" but produced no difference images, report failure.
        # This commonly happens when rewarpTemplate finds no template overlap for
        # any visit, so subtractImages is silently skipped by the framework.
        if diff_count == 0:
            log.warning(
                f"DIA pipeline ran but produced no difference images for "
                f"{night}/{band or 'all'} ({quanta_ok} quanta ran, "
                f"{quanta_fail} failed). Template may not overlap science visits."
            )
            return DIAResult(
                success=False,
                night=night,
                diff_run=cols.diff_run,
                template_collection=template_collection,
                diff_image_count=0,
                dia_source_count=0,
                error=(
                    f"No difference images produced ({quanta_ok} quanta ran, "
                    f"{quanta_fail} failed). Template may not overlap science visits."
                ),
            )

        return DIAResult(
            success=True,
            night=night,
            diff_run=cols.diff_run,
            template_collection=template_collection,
            diff_image_count=diff_count,
            dia_source_count=src_count,
        )

    except Exception as e:
        return DIAResult(
            success=False,
            night=night,
            diff_run=cols.diff_run,
            template_collection=template_collection,
            error=str(e),
        )
