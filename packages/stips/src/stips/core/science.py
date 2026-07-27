"""Science processing (ISR, calibration, source detection)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from stips.core import butler_query, quanta_report
from stips.core.pipeline import (
    REFCATS_CHAIN,
    CollectionNames,
    PipetaskStage,
    build_exclusion_expr,
    ensure_instrument_registered,
    find_bad_coord_exposures,
    isr_config_args,
    latest_raw_run,
    night_day_obs_expr,
    night_to_day_obs,
    parse_bad_exposures,
    read_log_delta,
    redefine_chain,
    validate_night,
)
from stips.core.query import butler_str_literal
from stips.core.refcat import refcat_overlay_config
from stips.profile import coerce_date as _coerce_date

if TYPE_CHECKING:
    from stips.core.config import Config

log = logging.getLogger(__name__)


def _night_is_boresight_covered(prof, night):
    """Whether the night's campaign has a characterized boresight offset.

    A night's science frames span two consecutive UT ``DATE-OBS`` dates (evening +
    the next morning), and the profile's boresight-offset table windows are keyed
    on per-frame UT. The campaign is characterized for this night if EITHER of the
    night's UT dates falls in a window -- this matches the translator's per-frame
    coverage exactly, so the two never disagree at a window's trailing edge (a
    night-vs-day_obs off-by-one would otherwise flag a covered night uncovered).
    Returns None if the profile has no ``boresight_offset_covered`` hook (e.g. a
    non-CTIO instrument) so callers can no-op.
    """
    hook = getattr(prof, "hooks", {}).get("boresight_offset_covered")
    if hook is None:
        return None
    offset = getattr(prof, "night_to_dayobs_offset_days", 1)
    night_date = _coerce_date(f"{night[:4]}-{night[4:6]}-{night[6:8]}")
    day_obs = night_to_day_obs(night, offset)  # 'YYYYMMDD' (== night + offset)
    dayobs_date = _coerce_date(f"{day_obs[:4]}-{day_obs[4:6]}-{day_obs[6:8]}")
    return bool(hook(night_date) or hook(dayobs_date))


def _warn_if_uncharacterized_campaign(prof, night):
    """Log a WARNING when the night's campaign has no boresight characterization.

    Informational and appropriately hedged: coverage is factual (a row exists or
    not); the "if astrometry fails" clause is conditional, not an assertion of
    cause. (A post-run auto-diagnosis was intentionally NOT added -- attributing a
    broad astrometry failure to a pointing offset cannot be done reliably from
    graph-wide success/failure counts alone; see the boresight-offset design doc.)
    """
    covered = _night_is_boresight_covered(prof, night)
    if covered is False:
        log.warning(
            "Night %s: this campaign has no boresight-offset characterization for "
            "this instrument. If astrometry fails broadly it may need one "
            "(blind-solve one exposure, measure the RA/Dec offset, add a row to the "
            "instrument profile's boresight-offset table).",
            night,
        )


def _count_matching_exposures(config: "Config", where: str) -> int | None:
    """Count exposure records matching a Butler WHERE expression.

    Returns None if the query fails.
    """
    from stips.core.stack import run_butler_python

    script = f"""
from lsst.daf.butler import Butler

butler = Butler({str(config.repo)!r})
rows = butler.registry.queryDimensionRecords("exposure", where={where!r})
print(sum(1 for _ in rows))
"""
    output = run_butler_python(script, config)
    if not output:
        return None

    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.isdigit():
            return int(line)

    return None


@dataclass
class ScienceConfig:
    """Configuration for science processing."""

    # Config file paths (resolved instrument-dir-first, else framework defaults;
    # callers may still pass relative or absolute Path values)
    calibrate_image: Path | None = None
    colorterms: Path | None = None

    # Fallback configs to try if primary fails
    calibrate_image_fallbacks: list[Path] = field(default_factory=list)

    # Reference-catalog mode; "gaia_ps1" applies the Gaia/PS1 overlay on top of
    # the tuned calibrateImage config. "monster" uses the DRP.yaml default.
    refcat_mode: str = "monster"

    @classmethod
    def default(cls, config: "Config") -> "ScienceConfig":
        """Create default config with standard paths (resolver-aware).

        Tuned calibrateImage configs are instrument-owned (they live under
        ``instruments/<x>/configs/``); the legacy names below resolve on the
        reference instrument but may not exist for a fork. When they don't,
        ``calibrate_image`` is ``None`` and science runs with the pipeline's
        stock calibrateImage config instead of failing.
        """
        primary = config.resolve_config(
            "calibrateImage/tuned_configs/2023ixf_relaxed.py"
        )
        fallback = config.resolve_config(
            "calibrateImage/tuned_configs/2023ixf_relaxed_psfex_sparse.py"
        )
        if not primary.exists():
            # No instrument-tuned config: use the neutral schema-compat
            # default (stock calibrateImage alone omits the aperture-flux
            # columns the rest of stage1 requires).
            primary = config.resolve_config("calibrateImage/neutral_default.py")
        return cls(
            calibrate_image=primary if primary.exists() else None,
            colorterms=config.resolve_config("apply_colorterms.py"),
            calibrate_image_fallbacks=[fallback] if fallback.exists() else [],
        )


@dataclass
class ScienceResult:
    """Result of science processing."""

    success: bool
    night: str
    science_run: str
    coadd_run: str | None
    error: str | None = None
    config_used: str | None = None  # Which config file succeeded
    fallback_used: bool = False  # Whether a fallback config was used
    quanta_succeeded: int = 0
    quanta_failed: int = 0


def _read_landolt_target_names() -> list[str]:
    """Read fits_object names from scripts/config/landolt_validation/landolt_catalog.csv.

    Used by run() when object='landolt_validation' to filter the science qgraph
    to just Landolt-field exposures.
    """
    import csv

    # science.py → core/ → stips/ → src/ → stips/ → packages/ → repo root
    #   (parents[0]=core, [1]=stips, [2]=src, [3]=stips, [4]=packages, [5]=root)
    repo_root = Path(__file__).resolve().parents[5]
    catalog = (
        repo_root / "scripts" / "config" / "landolt_validation" / "landolt_catalog.csv"
    )
    if not catalog.exists():
        return []
    names: list[str] = []
    with open(catalog, newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("fits_object") or "").strip()
            if name:
                names.append(name)
    return names


def resolve_object_filter(
    object_filter: str,
    config: "Config",
    night: str | None = None,
) -> str | None:
    """Resolve object filter to exact target_name using flexible matching.

    Performs case-insensitive substring matching against target_name values
    in the Butler repository.

    Args:
        object_filter: User-provided object name (can be partial, any case)
        config: Pipeline configuration
        night: Optional night to restrict search

    Returns:
        Exact target_name from FITS headers, or None if no match
    """
    from stips.core.stack import run_butler_python_json

    prof = config.require_profile()

    # Query all unique target names from Butler directly.
    where = f"instrument='{prof.name}' AND exposure.observation_type='science'"
    if night:
        # See run() — a Lick observing night spans two UT days.
        where += f" AND {night_day_obs_expr(night, prof)}"

    script = f"""
import json
from lsst.daf.butler import Butler

butler = Butler({str(config.repo)!r})
records = butler.registry.queryDimensionRecords("exposure", where={where!r})
target_names = sorted(
    {{
        str(rec.target_name).strip()
        for rec in records
        if getattr(rec, "target_name", None) not in (None, "")
    }}
)
print(json.dumps(target_names))
"""
    target_names = run_butler_python_json(script, config)
    if not target_names:
        target_names = []

    # Find matches (case-insensitive substring)
    object_lower = object_filter.lower()
    matches = [t for t in target_names if object_lower in t.lower()]

    if len(matches) == 1:
        log.debug(f"Resolved object filter '{object_filter}' -> '{matches[0]}'")
        return matches[0]
    elif len(matches) > 1:
        # Prefer exact match (case-insensitive) if available
        exact = [t for t in matches if t.lower() == object_lower]
        if exact:
            log.debug(
                f"Resolved object filter '{object_filter}' -> '{exact[0]}' (exact)"
            )
            return exact[0]
        # Otherwise use the first match but warn
        log.warning(
            f"Multiple matches for '{object_filter}': {matches}. Using '{matches[0]}'"
        )
        return matches[0]
    else:
        log.warning(
            f"No target_name matches for '{object_filter}'. Available: {target_names[:10]}"
        )
        return None


def _failure(night: str, science_parent: str, error: str, **kwargs) -> ScienceResult:
    """Build a failed ScienceResult (no coadd run)."""
    return ScienceResult(
        success=False,
        night=night,
        science_run=science_parent,
        coadd_run=None,
        error=error,
        **kwargs,
    )


def _build_exclusions(
    config: "Config",
    night: str,
    prof,
    *,
    bad_exposures: str | None,
    bad_file: Path | None,
    object_filter: str | None,
    target_ra: float | None,
    target_dec: float | None,
) -> tuple[str, str]:
    """Build the object-filter and bad-exposure WHERE fragments for a night.

    Combines three exclusion sources: explicitly listed bad exposure IDs
    (``bad_exposures``/``bad_file``), the resolved object filter, and
    pre-flight coordinate validation against the expected target position.

    Returns:
        ``(object_expr, exclusion_expr)`` — each either ``""`` or an
        ``" AND ..."`` fragment ready to append to the data query.
    """
    bad_ids = parse_bad_exposures(bad_exposures, bad_file)

    # Resolve object filter with flexible matching
    object_expr = ""
    resolved_object = None
    if object_filter:
        # Special case: "landolt_validation" resolves to the list of fits_object
        # names from the Landolt reference catalog, so the science qgraph only
        # includes Landolt-field visits (not other science targets observed the
        # same night).
        if object_filter.lower() == "landolt_validation":
            names = _read_landolt_target_names()
            if names:
                quoted = ", ".join(butler_str_literal(n) for n in names)
                object_expr = f" AND exposure.target_name IN ({quoted})"
            else:
                log.warning(
                    "object='landolt_validation' but landolt_catalog.csv could not "
                    "be read. Processing all science exposures for this night."
                )
        else:
            resolved_object = resolve_object_filter(object_filter, config, night)
            if resolved_object:
                object_expr = (
                    f" AND exposure.target_name={butler_str_literal(resolved_object)}"
                )
            else:
                # No match found - coordinate filtering (below) can still prune by target position.
                log.warning(
                    f"Could not resolve object '{object_filter}' to exact target_name. "
                    "Processing all science exposures for this night."
                )

    # Pre-flight coordinate validation: find exposures with bad coordinates.
    # If object resolution failed, validate against all science exposures for the
    # night and keep only those near the requested target coordinates.
    if target_ra is not None and target_dec is not None:
        coord_filter = resolved_object if resolved_object else None
        coord_bad_ids = find_bad_coord_exposures(
            config,
            night,
            target_ra,
            target_dec,
            object_filter=coord_filter,
            instrument_name=prof.name,
        )
        if coord_bad_ids:
            log.warning(
                f"Excluding {len(coord_bad_ids)} exposures with bad coordinates: "
                f"{coord_bad_ids}"
            )
            bad_ids.extend(coord_bad_ids)
            bad_ids = sorted(set(bad_ids))

    return object_expr, build_exclusion_expr(bad_ids)


def _band_expr(bands: list[str] | None) -> str:
    """Build the optional band-filter WHERE fragment (LSST dimension: "band").

    Bands are normalized (stripped, lowercased), validated, and deduplicated
    while preserving order. Raises ValueError on a malformed band value.
    """
    if not bands:
        return ""

    normalized_bands: list[str] = []
    for band in bands:
        b = str(band).strip().lower()
        if not b:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]+", b):
            raise ValueError(f"Invalid band value in science bands filter: {band!r}")
        normalized_bands.append(b)

    if not normalized_bands:
        return ""

    # Keep deterministic order while dropping duplicates.
    unique_bands = list(dict.fromkeys(normalized_bands))
    band_csv = ",".join(f"'{b}'" for b in unique_bands)
    log.info(f"Filtering science processing to bands: {unique_bands}")
    return f" AND band IN ({band_csv})"


def _build_data_query(
    config: "Config",
    night: str,
    prof,
    *,
    bad_exposures: str | None,
    bad_file: Path | None,
    object_filter: str | None,
    target_ra: float | None,
    target_dec: float | None,
    bands: list[str] | None,
) -> str:
    """Assemble the Butler WHERE expression selecting this night's science data.

    A single Lick observing night can span two UT days: exposures taken
    before Pacific midnight have day_obs=night, and post-midnight exposures
    have day_obs=night+1 (what night_to_day_obs returns). Query both, then
    apply the object/band/exclusion fragments.
    """
    object_expr, exclusion_expr = _build_exclusions(
        config,
        night,
        prof,
        bad_exposures=bad_exposures,
        bad_file=bad_file,
        object_filter=object_filter,
        target_ra=target_ra,
        target_dec=target_dec,
    )
    band_expr = _band_expr(bands)
    return (
        f"instrument='{prof.name}' AND exposure.observation_type='science'"
        f" AND {night_day_obs_expr(night, prof)}"
        f"{object_expr}{band_expr}{exclusion_expr}"
    )


def _resolve_configs_to_try(
    science_cfg: ScienceConfig, use_fallbacks: bool
) -> "list[Path | None]":
    """Build the ordered list of calibrateImage configs to attempt.

    The primary config comes first; fallbacks are appended (deduplicated)
    only when ``use_fallbacks`` is set. Configs whose files do not exist
    are dropped. An empty result means science processing cannot start.
    """
    configs_to_try: list[Path | None] = []
    if science_cfg.calibrate_image and science_cfg.calibrate_image.exists():
        configs_to_try.append(science_cfg.calibrate_image)
    if use_fallbacks:
        for fb in science_cfg.calibrate_image_fallbacks:
            if fb.exists() and fb not in configs_to_try:
                configs_to_try.append(fb)
    if not configs_to_try and science_cfg.calibrate_image is None:
        # No instrument-tuned config exists (normal for a fork that has not
        # tuned calibrateImage yet): run with the pipeline's stock config.
        log.info(
            "No instrument-tuned calibrateImage config found; "
            "using the pipeline default"
        )
        configs_to_try.append(None)
    return configs_to_try


# F-042: known-fatal error patterns. When a config attempt raises and the error
# text matches one of these, a different calibrateImage config cannot help
# (missing repo inputs, repo-level definition conflicts, permission or disk
# problems), so the fallback cascade stops. These are best-effort heuristics to
# save time and log noise — NEVER correctness signals: an unmatched error
# simply means the remaining fallbacks are still attempted, and a matched one
# only skips work that would have failed identically.
_FATAL_ERROR_PATTERNS: tuple[str, ...] = (
    "MissingCollectionError",
    "ConflictingDefinitionError",
    "Permission denied",
    "No space left on device",
)


def _is_fatal_error(error_str: str) -> bool:
    """Return True when no calibrateImage fallback could fix this error.

    The refcat-missing combination (FileNotFoundError + astrometry_ref_cat)
    means the field has no reference-catalog shard at all; the generic
    patterns are repo/environment failures. See _FATAL_ERROR_PATTERNS for
    the heuristic caveat.
    """
    if "FileNotFoundError" in error_str and "astrometry_ref_cat" in error_str:
        return True
    lowered = error_str.lower()
    return any(p.lower() in lowered for p in _FATAL_ERROR_PATTERNS)


@dataclass
class _AttemptOutcome:
    """Raw facts from one calibrateImage config attempt.

    ``rc`` is the pipetask exit code, or None when the attempt raised before
    completing. Exactly one of ``raised``/``full_success``/``partial_success``/
    ``total_failure`` holds for any outcome.
    """

    run_collection: str
    rc: int | None = None
    quanta_ok: int = 0
    quanta_fail: int = 0
    parse_failed: bool = False  # quanta summary could not be parsed
    error: str | None = None  # exception text or pipetask output tail
    failed_exposures: list[dict] = field(default_factory=list)
    fatal: bool = False  # raised and matched a known-fatal pattern (F-042)

    @property
    def raised(self) -> bool:
        return self.rc is None

    @property
    def full_success(self) -> bool:
        return self.rc == 0

    @property
    def partial_success(self) -> bool:
        return self.rc is not None and self.rc != 0 and self.quanta_ok > 0

    @property
    def total_failure(self) -> bool:
        return self.rc is not None and self.rc != 0 and self.quanta_ok == 0

    @property
    def produced_outputs(self) -> bool:
        """True when this attempt wrote at least some outputs to its RUN."""
        return self.full_success or self.partial_success

    def to_attempt(self, config_name: str, is_fallback: bool):
        """Map this outcome onto a processing-log ConfigAttempt record.

        Field semantics match the pre-decomposition loop exactly: honest
        parsed counts, ``quanta_parse_failed`` markers instead of fabricated
        counts (F-026), and ``error`` populated only for total failures and
        raised attempts.
        """
        from stips.core import processing_log

        attempt = processing_log.ConfigAttempt(
            config=config_name, is_fallback=is_fallback
        )
        if self.raised:
            # The attempt raised before any quanta could be counted, so the
            # failure count is unknown, not 1. Leave quanta_failed at 0 and
            # mark the parse failure; the populated ``error`` field carries
            # the failure downstream.
            attempt.error = self.error
            attempt.quanta_parse_failed = True
        elif self.full_success:
            attempt.quanta_succeeded = self.quanta_ok
            attempt.quanta_parse_failed = self.parse_failed
        elif self.partial_success:
            attempt.quanta_succeeded = self.quanta_ok
            attempt.quanta_failed = self.quanta_fail
            attempt.failed_exposures = self.failed_exposures
        else:  # total failure
            attempt.error = self.error
            attempt.failed_exposures = self.failed_exposures
            attempt.quanta_failed = self.quanta_fail
            attempt.quanta_parse_failed = self.parse_failed
        return attempt


@dataclass
class _AttemptContext:
    """Loop-invariant inputs shared by every config attempt in one run()."""

    config: "Config"
    executor: object
    night: str
    cols: CollectionNames
    prof: object
    pipeline: Path
    raw_run: str
    data_query: str
    colorterms_config: Path
    refcat_mode: str
    qg_dir: Path
    jobs: int
    log_file: Path | None


def _attempt_config(
    ctx: _AttemptContext,
    index: int,
    tuned_config: Path | None,
    prior_runs: list[str],
) -> _AttemptOutcome:
    """Run one calibrateImage config attempt end-to-end.

    Builds the quantum graph, executes the pipeline, parses the quanta
    counts, and classifies the outcome. Never raises: exceptions are
    captured into the returned outcome, with the known-fatal classification
    of F-042 deciding whether the caller should stop the fallback cascade.
    """
    is_fallback = index > 0
    config_label = "fallback" if is_fallback else "primary"
    cfg_name = tuned_config.name if tuned_config is not None else "<pipeline-default>"

    # Each config attempt gets its own RUN collection.
    # Primary: .../run
    # Fallbacks: .../run_fb1, .../run_fb2, .../run_fb3
    if is_fallback:
        output_run = f"{ctx.cols.science_parent}/run_fb{index}"
    else:
        output_run = ctx.cols.science_run

    # Each attempt gets its own qgraph (different config = different plan)
    qg_science = ctx.qg_dir / f"processCcd_{ctx.night}_{ctx.cols.run_ts}_cfg{index}.qg"
    repo = str(ctx.config.repo)

    try:
        # calibrateImage config-file chain: tuned config, then (optionally)
        # the Gaia/PS1 refcat overlay, then color terms. Order keeps color
        # terms last so they see the final photometry_ref_cat.
        config_file_args = (
            ["--config-file", f"calibrateImage:{tuned_config}"]
            if tuned_config is not None
            else []
        )
        overlay_name = refcat_overlay_config(ctx.refcat_mode)
        if overlay_name:
            overlay_path = ctx.config.resolve_config(overlay_name)
            config_file_args += [
                "--config-file",
                f"calibrateImage:{overlay_path}",
            ]
            # The stage1 QA ref-match tasks default to MONSTER; in the gaia_ps1
            # and gaia modes redirect them to the same catalogs used for
            # calibration (otherwise fields outside local MONSTER shard
            # coverage fail graph construction on a non-optional connection).
            # Astrometry is Gaia in both modes; photometry is Gaia (gaia,
            # southern fields with no PS1) or PS1 (gaia_ps1).
            qa_astrom = ctx.config.resolve_config("refcats_gaia_ps1_qa_astrom.py")
            qa_photom_name = (
                "refcats_gaia_only_qa_photom.py"
                if ctx.refcat_mode == "gaia"
                else "refcats_gaia_ps1_qa_photom.py"
            )
            qa_photom = ctx.config.resolve_config(qa_photom_name)
            config_file_args += [
                "--config-file",
                f"makeAnalysisSingleVisitStarAstrometricRefMatchVisit:{qa_astrom}",
                "--config-file",
                f"makeAnalysisSingleVisitStarPhotometricRefMatchVisit:{qa_photom}",
            ]
        config_file_args += [
            "--config-file",
            f"calibrateImage:{ctx.colorterms_config}",
        ]

        # Profile-declared ISR overrides (e.g. doDefect=False for an
        # instrument without curated defect maps, or parallel overscan).
        # Applied as inline config so instruments need not fork the shared DRP
        # pipeline; the same overrides feed the calib-build ISR (calibs.py).
        post_query_args = list(isr_config_args(ctx.prof))

        # For fallback attempts, build a qgraph that excludes quanta
        # whose outputs already exist in any prior successful RUN.
        # --skip-existing-in filters at graph-build time based on _metadata
        # datasets, so the qgraph only contains the failed quanta that
        # need retrying with a different config.
        #
        # Each fallback writes to its own RUN to avoid
        # ConflictingDefinitionError (LSST enforces config consistency
        # per task label within a single RUN collection).
        if is_fallback and prior_runs:
            for prior_run in prior_runs:
                post_query_args.extend(["--skip-existing-in", prior_run])
            post_query_args.append("--clobber-outputs")

        summary_file = qg_science.with_suffix(".summary.json")
        stage = PipetaskStage(
            repo=repo,
            pipeline=f"{ctx.pipeline}#stage1-single-visit",
            inputs=f"{ctx.raw_run},{ctx.cols.calib_chain},{REFCATS_CHAIN},{ctx.prof.skymap_collection}",
            output_parent=ctx.cols.science_parent,
            output_run=output_run,
            qgraph_path=str(qg_science),
            data_query=ctx.data_query,
            # calibrateImage config-file chain goes BEFORE -d (science-specific).
            pre_query_args=config_file_args,
            post_query_args=post_query_args,
            jobs=ctx.jobs,
            run_includes_output_run=True,
            summary_file=str(summary_file),
        )

        # Build quantum graph
        ctx.executor.run_pipetask(
            stage.qgraph_args(), ctx.config, log_file=ctx.log_file
        )

        # Fallback qgraphs are already reduced to unresolved quanta via
        # --skip-existing-in at qgraph build time; execute directly.
        log_start_pos = None
        if ctx.log_file and ctx.log_file.exists():
            try:
                log_start_pos = ctx.log_file.stat().st_size
            except OSError:
                pass

        # Run science processing
        result = ctx.executor.run_pipetask(
            stage.run_args(),
            ctx.config,
            capture_output=True,
            check=False,
            log_file=ctx.log_file,
            output_run=output_run,
        )

        # Parse actual quanta counts (structured --summary JSON preferred,
        # stdout/log regex fallback).
        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
        if not combined_output.strip():
            combined_output = read_log_delta(
                ctx.log_file, log_start_pos=log_start_pos, max_chars=8000
            )
        quanta_ok, quanta_fail = quanta_report.counts(
            summary_file,
            combined_output,
            log_file=ctx.log_file,
            log_start_pos=log_start_pos,
        )

        if result.returncode == 0:
            # Full success with this config (rc==0 => pipetask ran the
            # planned quanta). Record the true parsed count. If quanta_ok
            # is 0 here, the quanta summary could not be parsed — record
            # the honest 0 plus an explicit parse-failure marker rather
            # than fabricating a count. Success is still driven by rc==0.
            parse_failed = quanta_ok == 0
            if parse_failed:
                log.warning(
                    "Quanta summary could not be parsed (returncode=0); "
                    "recording 0 succeeded with quanta_parse_failed=True. "
                    "Reported counts may understate the work actually done."
                )
            return _AttemptOutcome(
                run_collection=output_run,
                rc=result.returncode,
                quanta_ok=quanta_ok,
                quanta_fail=quanta_fail,
                parse_failed=parse_failed,
            )

        from stips.core import processing_log

        failed_exposures = processing_log.parse_pipetask_failures(
            result.stderr or "", result.stdout or ""
        )
        if quanta_ok > 0:
            # Partial success - some quanta succeeded, some failed
            return _AttemptOutcome(
                run_collection=output_run,
                rc=result.returncode,
                quanta_ok=quanta_ok,
                quanta_fail=quanta_fail,
                failed_exposures=failed_exposures,
            )

        # Total failure - no quanta succeeded. Record the honest parsed
        # count. If the summary could not be parsed (quanta_fail == 0 despite
        # the non-zero returncode), mark the parse failure instead of
        # fabricating a count of 1.
        return _AttemptOutcome(
            run_collection=output_run,
            rc=result.returncode,
            quanta_fail=quanta_fail,
            parse_failed=quanta_fail == 0,
            error=(
                combined_output.strip()[-500:]
                if combined_output.strip()
                else "Unknown error"
            ),
            failed_exposures=failed_exposures,
        )

    except Exception as e:
        error_str = str(e)

        # Log detailed error information
        log.warning(f"{config_label.capitalize()} config failed: {cfg_name}")

        # Surface key parts of the error for diagnostics
        if "FileNotFoundError" in error_str and "astrometry_ref_cat" in error_str:
            log.error(
                "Reference catalog not found for this field - no refcat shard available"
            )
            log.error("This usually means the field is outside the refcat coverage")
        elif "FileNotFoundError" in error_str:
            # Extract the specific file/dataset that's missing
            match = re.search(r"connection (\S+)", error_str)
            if match:
                log.error(f"Missing required dataset: {match.group(1)}")
            log.error(f"Full error: {error_str[:200]}")
        else:
            log.error(f"Error: {error_str[:200]}")

        # F-042: only a small set of known-fatal patterns stops the fallback
        # cascade; anything else is presumed worth retrying with the next
        # config. Best-effort heuristic, never a correctness signal.
        fatal = _is_fatal_error(error_str)
        if fatal:
            if "astrometry_ref_cat" in error_str:
                log.info("Refcat missing - skipping fallback (won't help)")
            else:
                log.info("Known-fatal error - skipping fallbacks (won't help)")

        return _AttemptOutcome(
            run_collection=output_run,
            error=error_str[:500],
            fatal=fatal,
        )


@dataclass
class _AttemptsSummary:
    """Folded result of the config-attempt cascade."""

    successful_runs: list[str] = field(default_factory=list)
    config_used: Path | None = None
    fallback_used: bool = False
    any_success: bool = False
    cumulative_succeeded: int = 0  # unique successes across all configs


def _run_config_attempts(
    ctx: _AttemptContext,
    configs_to_try: list[Path],
    use_fallbacks: bool,
    plog,
) -> _AttemptsSummary:
    """Try each calibrateImage config in order, folding attempt outcomes.

    The primary config runs first; each fallback retries only the quanta that
    are still unresolved (via --skip-existing-in on the prior RUNs). Every
    attempt is recorded on ``plog``. The fold stops early on full success, on
    an accepted partial result, or on a known-fatal exception (F-042).
    """
    summary = _AttemptsSummary()

    for i, tuned_config in enumerate(configs_to_try):
        cfg_name = (
            tuned_config.name if tuned_config is not None else "<pipeline-default>"
        )
        is_fallback = i > 0
        is_last = i == len(configs_to_try) - 1
        config_label = "fallback" if is_fallback else "primary"
        log.info(f"Trying {config_label} config: {cfg_name}")

        outcome = _attempt_config(ctx, i, tuned_config, summary.successful_runs)
        plog.add_attempt(outcome.to_attempt(cfg_name, is_fallback))

        if outcome.produced_outputs:
            summary.successful_runs.append(outcome.run_collection)
            summary.any_success = True
            summary.config_used = tuned_config
            summary.fallback_used = is_fallback
            # Fallback qgraphs only contain quanta that previously failed, so
            # every fallback success is a new win — no double counting.
            summary.cumulative_succeeded += outcome.quanta_ok

        if outcome.full_success:
            if is_fallback:
                log.info(
                    f"Fallback config {cfg_name} rescued all "
                    f"{outcome.quanta_ok} remaining quanta"
                )
            else:
                log.info(
                    f"Science processing fully succeeded with {config_label} "
                    f"config: {cfg_name} ({outcome.quanta_ok} quanta)"
                )
            break

        elif outcome.partial_success:
            if is_fallback:
                log.warning(
                    f"Fallback config {cfg_name}: "
                    f"{outcome.quanta_ok} new quanta rescued, "
                    f"{outcome.quanta_fail} still failing "
                    f"(cumulative: {summary.cumulative_succeeded} succeeded)"
                )
            else:
                log.warning(
                    f"Partial success with primary config: {cfg_name} "
                    f"({outcome.quanta_ok} quanta succeeded, "
                    f"{outcome.quanta_fail} failed)"
                )
            # Don't stop yet - try fallback for the remaining failures
            if not use_fallbacks or is_last:
                log.info(
                    f"Accepting partial result with {summary.cumulative_succeeded} "
                    "successful quanta"
                )
                break
            log.info(
                f"Trying fallback config for {outcome.quanta_fail} remaining "
                "failures..."
            )

        elif outcome.total_failure:
            log.error(f"No quanta succeeded with {config_label} config: " f"{cfg_name}")
            if not use_fallbacks or is_last:
                if is_last:
                    log.error(
                        f"All {len(configs_to_try)} configs exhausted for "
                        f"{ctx.night}"
                    )
            else:
                log.warning(
                    f"{config_label} config had total failure, trying fallback..."
                )

        else:  # raised
            if outcome.fatal:
                break

    return summary


def _save_processing_log(plog, config: "Config", output_collection: str) -> None:
    """Finalize and persist the processing log and provenance record."""
    from stips.core import processing_log, provenance

    plog.output_collection = output_collection
    plog.finalize()
    processing_log.save_log(plog, config)
    provenance.upsert_from_log(plog, config)  # non-fatal; logs on failure


def _final_counts(attempts: _AttemptsSummary, plog) -> tuple[int, int]:
    """Compute ``(total_succeeded, last_attempt_failed)`` for the result.

    ``cumulative_succeeded`` tracks unique successes across all configs.
    Failures are NOT summed: because each fallback's qgraph is reduced (via
    --skip-existing-in) to only the quanta that previously failed, the LAST
    attempt's failure count is precisely the set of still-unresolved quanta.
    Summing would double-count. Named honestly so callers know it is the last
    attempt's remaining failures, not a total.
    """
    last_attempt_failed = (
        plog.configs_tried[-1].quanta_failed if plog.configs_tried else 0
    )
    return attempts.cumulative_succeeded, last_attempt_failed


def _verify_and_chain_runs(
    config: "Config",
    cols: CollectionNames,
    successful_runs: list[str],
    log_file: Path | None,
) -> list[str]:
    """Verify RUN collections exist, then chain them under the CHAINED parent.

    Chain all successful RUN collections under the parent CHAINED collection
    so downstream consumers (DIA, fphot) see a unified view. Order matters:
    later runs (fallbacks) should be searched first so their outputs take
    precedence over partial/failed primary outputs.

    Each RUN is verified to actually exist in the Butler before chaining —
    BPS may report success even when all quanta failed (no outputs written =
    no RUN collection created).

    Returns the verified RUNs; an empty list means nothing was written and
    the caller should report failure. May raise on a Butler failure.
    """
    verified_runs: list[str] = []
    for run_name in successful_runs:
        if butler_query.collection_exists(config, run_name):
            verified_runs.append(run_name)
        else:
            log.warning(
                f"RUN collection {run_name} does not exist in Butler "
                "(all quanta may have failed) — skipping"
            )

    if not verified_runs:
        return []

    chain_members = list(reversed(verified_runs))
    redefine_chain(config, cols.science_parent, chain_members, log_file=log_file)
    return verified_runs


def _run_coadd_tail(
    config: "Config",
    cols: CollectionNames,
    prof,
    pipeline: Path,
    qg_dir: Path,
    night: str,
    jobs: int,
    log_file: Path | None,
    executor,
) -> str:
    """Build coadds from the night's science outputs and chain the result.

    Returns the coadd RUN collection name. May raise on pipeline failure
    (the caller converts that into a failed ScienceResult).
    """
    repo = str(config.repo)
    qg_coadd = qg_dir / f"coadds_{night}_{cols.run_ts}.qg"

    stage = PipetaskStage(
        repo=repo,
        pipeline=f"{pipeline}#coadds-only",
        inputs=f"{cols.science_parent},{cols.calib_chain},{REFCATS_CHAIN},{prof.skymap_collection}",
        output_parent=cols.coadd_parent,
        output_run=cols.coadd_run,
        qgraph_path=str(qg_coadd),
        data_query=f"instrument='{prof.name}' AND skymap='{prof.skymap_name}'",
        jobs=jobs,
    )

    executor.run_pipetask(stage.qgraph_args(), config, log_file=log_file)

    executor.run_pipetask(
        stage.run_args(),
        config,
        log_file=log_file,
        output_run=cols.coadd_run,
    )

    redefine_chain(config, cols.coadd_parent, cols.coadd_run, log_file=log_file)
    return cols.coadd_run


def run(
    night: str,
    config: Config,
    *,
    jobs: int = 8,
    bad_exposures: str | None = None,
    bad_file: Path | None = None,
    object_filter: str | None = None,
    skip_coadds: bool = False,
    science_config: Path | None = None,
    science_cfg: ScienceConfig | None = None,
    use_fallbacks: bool = True,
    bands: list[str] | None = None,
    target_ra: float | None = None,
    target_dec: float | None = None,
    log_file: Path | None = None,
    executor=None,
) -> ScienceResult:
    """Run science processing for a night.

    This performs:
    1. Single-visit processing (ISR, source detection, WCS, photometry)
    2. Optionally build coadds from the night's data

    Args:
        night: Observing night (YYYYMMDD)
        config: Pipeline configuration
        jobs: Number of parallel jobs
        bad_exposures: Comma-separated exposure IDs to exclude
        bad_file: File with exposure IDs to exclude
        object_filter: Filter by OBJECT header value (case-insensitive, partial match)
        skip_coadds: Skip coadd generation
        science_config: Override calibrateImage config file (legacy, prefer science_cfg)
        science_cfg: Full science configuration with fallbacks
        use_fallbacks: Try fallback configs on failure
        bands: Optional list of bands to process (e.g. ["r", "i"])
        target_ra: Expected target RA in degrees (enables coordinate validation)
        target_dec: Expected target Dec in degrees (enables coordinate validation)
        log_file: Optional path to write LSST pipeline logs

    Returns:
        ScienceResult with collection names and status
    """
    from stips.core.executor import LocalExecutor

    if executor is None:
        executor = LocalExecutor()

    prof = config.require_profile()
    night = validate_night(night)
    cols = CollectionNames(night, prefix=prof.collection_prefix)
    _warn_if_uncharacterized_campaign(prof, night)

    # Build config chain: explicit > legacy > default
    if science_cfg is None:
        science_cfg = ScienceConfig.default(config)
    if science_config is not None:
        science_cfg.calibrate_image = science_config

    # Find the raw collection for this night (targeted query).
    # Use the newest raw ingest — a re-ingest (e.g. after a header fix) appends
    # a newer timestamp that supersedes the stale earlier one.
    try:
        raw_run = latest_raw_run(config, night)
        if not raw_run:
            return _failure(
                night,
                cols.science_parent,
                f"No raw collection found for night {night}",
            )
    except Exception as e:
        return _failure(night, cols.science_parent, f"Failed to query collections: {e}")

    # Data selection: two-UT-day window, object filter, band filter, and
    # bad-exposure/coordinate-validation exclusions.
    data_query = _build_data_query(
        config,
        night,
        prof,
        bad_exposures=bad_exposures,
        bad_file=bad_file,
        object_filter=object_filter,
        target_ra=target_ra,
        target_dec=target_dec,
        bands=bands,
    )

    # Pipeline and config paths
    pipeline = config.resolve_pipeline("DRP.yaml")
    colorterms_config = science_cfg.colorterms or config.resolve_config(
        "apply_colorterms.py"
    )

    # Build list of configs to try (primary + fallbacks)
    configs_to_try = _resolve_configs_to_try(science_cfg, use_fallbacks)
    if not configs_to_try:
        return _failure(
            night,
            cols.science_parent,
            f"No valid config files found. Tried: {science_cfg.calibrate_image}",
        )

    # Fail fast if this night has no matching exposures after filtering.
    match_count = _count_matching_exposures(config, data_query)
    if match_count == 0:
        return _failure(
            night,
            cols.science_parent,
            f"No science exposures matched selection for night {night} "
            "(after object/coordinate/bad-exposure filtering)",
        )
    if match_count is not None:
        log.info(f"Found {match_count} matching science exposures for {night}")

    ensure_instrument_registered(config, log_file)

    qg_dir = config.repo / "qgraphs"
    qg_dir.mkdir(parents=True, exist_ok=True)

    # Create processing log for this night
    from stips.core import processing_log

    plog = processing_log.create_log(night, "science")

    # Try each config in order; fallbacks retry only the still-failed quanta,
    # each in its own RUN collection (see _run_config_attempts).
    ctx = _AttemptContext(
        config=config,
        executor=executor,
        night=night,
        cols=cols,
        prof=prof,
        pipeline=pipeline,
        raw_run=raw_run,
        data_query=data_query,
        colorterms_config=colorterms_config,
        refcat_mode=science_cfg.refcat_mode,
        qg_dir=qg_dir,
        jobs=jobs,
        log_file=log_file,
    )
    attempts = _run_config_attempts(ctx, configs_to_try, use_fallbacks, plog)

    _save_processing_log(plog, config, cols.science_run)
    total_succeeded, last_attempt_failed = _final_counts(attempts, plog)

    # Check if any config succeeded
    if not attempts.any_success:
        last_error = (
            plog.configs_tried[-1].error if plog.configs_tried else "No configs tried"
        )
        return _failure(
            night,
            cols.science_parent,
            last_error or "All configs failed",
            quanta_succeeded=total_succeeded,
            quanta_failed=last_attempt_failed,
        )

    config_used = str(attempts.config_used) if attempts.config_used else None
    try:
        verified_runs = _verify_and_chain_runs(
            config, cols, attempts.successful_runs, log_file
        )
        if not verified_runs:
            return _failure(
                night,
                cols.science_parent,
                "No RUN collections were created (all quanta failed)",
                quanta_succeeded=0,
                quanta_failed=last_attempt_failed,
            )

        coadd_run = None
        if not skip_coadds:
            coadd_run = _run_coadd_tail(
                config, cols, prof, pipeline, qg_dir, night, jobs, log_file, executor
            )

        return ScienceResult(
            success=True,
            night=night,
            science_run=cols.science_parent,
            coadd_run=coadd_run,
            config_used=config_used,
            fallback_used=attempts.fallback_used,
            quanta_succeeded=total_succeeded,
            quanta_failed=last_attempt_failed,
        )

    except Exception as e:
        return _failure(
            night,
            cols.science_parent,
            str(e),
            config_used=config_used,
            fallback_used=attempts.fallback_used,
            quanta_succeeded=total_succeeded,
            quanta_failed=last_attempt_failed,
        )
