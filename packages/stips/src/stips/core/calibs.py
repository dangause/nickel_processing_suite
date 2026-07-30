"""Nightly calibration processing (bias, flat, defects)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from stips.core import butler_query
from stips.core.pipeline import (
    CollectionNames,
    PipetaskStage,
    ensure_instrument_registered,
    find_aliasing_exposure_ids,
    get_raw_dir,
    isr_config_args,
    night_to_date_range,
    redefine_chain,
    validate_night,
)
from stips.core.stack import run_butler

if TYPE_CHECKING:
    from stips.core.config import Config

log = logging.getLogger(__name__)


@dataclass
class CalibsResult:
    """Result of nightly calibration processing.

    Attributes:
        success: True iff at least one of bias/flat was verified to have
            produced (and certified) calibration products. A pipeline that
            exits non-zero but still built some products counts as a partial
            success; zero products for both means failure.
        night: Observing night (YYYYMMDD).
        raw_run: Butler RUN collection for ingested raw frames.
        calib_chain: Unified calibration CHAINED collection.
        cp_bias: Bias calibration collection name.
        cp_flat: Flat calibration collection name.
        error: Error message if processing failed, None on success.
    """

    success: bool
    night: str
    raw_run: str
    calib_chain: str
    cp_bias: str
    cp_flat: str
    error: str | None = None


def write_curated_calibrations(
    night: str,
    config: Config,
    *,
    log_file: Path | None = None,
) -> None:
    """Write curated calibrations (defects, crosstalk) for a night.

    This is safe to call once before concurrent calibs. The curated
    calibrations are instrument-level data that only need to be written
    once per run.

    Args:
        night: Any observing night (used for raw_run collection reference)
        config: Pipeline configuration
        log_file: Optional log file path
    """
    prof = config.require_profile()
    night = validate_night(night)
    cols = CollectionNames(night, prefix=prof.collection_prefix)
    repo = str(config.repo)

    # Ingest raws for this night first (needed for write-curated-calibrations)
    raw_dir = get_raw_dir(config, night)
    if raw_dir.exists():
        ensure_instrument_registered(config, log_file)
        run_butler(
            [
                "ingest-raws",
                repo,
                str(raw_dir),
                "--transfer",
                "copy",
                "--output-run",
                cols.raw_run,
            ],
            config,
            check=False,
            log_file=log_file,
        )
        run_butler(["define-visits", repo, prof.name], config, log_file=log_file)

    _write_curated_and_chain(night, config, cols, log_file=log_file)


def _write_curated_and_chain(
    night: str,
    config: Config,
    cols: CollectionNames,
    *,
    log_file: Path | None = None,
) -> None:
    """Write curated calibs (defects), build+certify declarative crosstalk, chain.

    The curated chain is redefined to ``[crosstalk_calib, curated_run]`` when the
    profile declares crosstalk (so ISR resolves the ``crosstalk`` prerequisite),
    otherwise just ``[curated_run]``. Shared by ``write_curated_calibrations``
    (orchestrator one-time) and standalone ``run_calibs``.
    """
    from stips.core.crosstalk import build_and_certify_crosstalk

    prof = config.require_profile()
    repo = str(config.repo)

    run_butler(
        [
            "write-curated-calibrations",
            repo,
            prof.name,
            cols.raw_run,
            "--collection",
            cols.curated_run,
        ],
        config,
        log_file=log_file,
    )

    curated_children = [cols.curated_run]
    if prof.crosstalk is not None:
        ct_result = build_and_certify_crosstalk(night, config, log_file=log_file)
        if ct_result.success:
            curated_children = [cols.crosstalk_calib, cols.curated_run]
        else:
            log.warning("Crosstalk calib not certified: %s", ct_result.error)

    run_butler(
        [
            "collection-chain",
            repo,
            cols.curated_chain,
            *curated_children,
            "--mode",
            "redefine",
        ],
        config,
        log_file=log_file,
    )


def run(
    night: str,
    config: Config,
    *,
    jobs: int = 4,
    log_file: Path | None = None,
    executor=None,
    skip_curated: bool = False,
) -> CalibsResult:
    """Run nightly calibration processing.

    This performs:
    1. Ingest raw data
    2. Write curated calibrations (defects from obs_nickel_data)
    3. Build and certify bias frames
    4. Build and certify flat frames
    5. Update the unified calibration chain

    Args:
        night: Observing night (YYYYMMDD)
        config: Pipeline configuration
        jobs: Number of parallel jobs
        log_file: Optional path to write LSST pipeline logs
        skip_curated: Skip curated calibrations write (already done externally)

    Returns:
        CalibsResult with collection names and status
    """
    from stips.core.executor import LocalExecutor

    if executor is None:
        executor = LocalExecutor()

    prof = config.require_profile()
    night = validate_night(night)
    cols = CollectionNames(night, prefix=prof.collection_prefix)

    raw_dir = get_raw_dir(config, night)
    if not raw_dir.exists():
        return CalibsResult(
            success=False,
            night=night,
            raw_run=cols.raw_run,
            calib_chain=cols.calib_chain,
            cp_bias=cols.cp_bias,
            cp_flat=cols.cp_flat,
            error=f"Raw directory not found: {raw_dir}",
        )

    repo = str(config.repo)

    # Ensure cp_pipe is configured
    if not config.cp_pipe_dir:
        return CalibsResult(
            success=False,
            night=night,
            raw_run=cols.raw_run,
            calib_chain=cols.calib_chain,
            cp_bias=cols.cp_bias,
            cp_flat=cols.cp_flat,
            error="CP_PIPE_DIR not configured",
        )

    # Pre-ingest: no two frames in this night may claim the same exposure_id.
    # Profiles whose raw sequence keyword is wider than the packed id's sequence
    # field fold it into range (Nickel folds OBSNUM % 10000), and a fold can
    # alias. This is the only layer that sees the whole night, so it is the only
    # one that can catch it — see find_aliasing_exposure_ids().
    collisions = find_aliasing_exposure_ids(config, night)
    if collisions:
        detail = "; ".join(
            f"exposure_id {exp_id} claimed by "
            + ", ".join(f"{name} ({obs_id})" for name, obs_id in frames)
            for exp_id, frames in sorted(collisions.items())
        )
        return CalibsResult(
            success=False,
            night=night,
            raw_run=cols.raw_run,
            calib_chain=cols.calib_chain,
            cp_bias=cols.cp_bias,
            cp_flat=cols.cp_flat,
            error=(
                f"Colliding exposure_ids in {night}: {detail}. Ingesting these "
                "would silently collapse distinct frames onto one exposure. "
                "Move the offending frames out of the night's raw dir, or widen "
                "the profile's exposure_id scheme."
            ),
        )

    try:
        # Register instrument (idempotent)
        ensure_instrument_registered(config, log_file)

        # Ingest raws. check=False because a re-run of an already-ingested
        # night legitimately returns non-zero (the raw datasets already exist).
        # But that same non-zero also covers real failures (disk full,
        # permissions, corrupt FITS), so on failure we verify that raws for the
        # night are actually present before continuing.
        ingest_result = run_butler(
            [
                "ingest-raws",
                repo,
                str(raw_dir),
                "--transfer",
                "copy",
                "--output-run",
                cols.raw_run,
            ],
            config,
            check=False,  # May fail if already ingested
            capture_output=True,
            log_file=log_file,
        )
        if ingest_result.returncode != 0:
            stderr = (ingest_result.stderr or "").strip()
            log.warning(
                "ingest-raws returned %s for %s: %s",
                ingest_result.returncode,
                night,
                stderr,
            )
            raw_pattern = f"{cols.prefix}/raw/{night}/*"
            raw_count = butler_query.count_datasets(config, "raw", raw_pattern) or 0
            if raw_count == 0:
                return CalibsResult(
                    success=False,
                    night=night,
                    raw_run=cols.raw_run,
                    calib_chain=cols.calib_chain,
                    cp_bias=cols.cp_bias,
                    cp_flat=cols.cp_flat,
                    error=(
                        f"ingest-raws failed for {night} and no raws are present "
                        f"for the night: {stderr}"
                    ),
                )

        # Define visits
        run_butler(["define-visits", repo, prof.name], config, log_file=log_file)

        # Write curated calibrations (skip if already done by orchestrator)
        if not skip_curated:
            _write_curated_and_chain(night, config, cols, log_file=log_file)

        begin_iso, end_iso = night_to_date_range(night)

        # Build bias
        qg_bias = config.repo / "qgraphs" / f"cp_bias_{night}_{cols.run_ts}.qg"
        qg_bias.parent.mkdir(parents=True, exist_ok=True)
        bias_ok = False

        try:
            bias_stage = PipetaskStage(
                repo=repo,
                pipeline=str(config.resolve_pipeline("CpBias.yaml")),
                inputs=f"{cols.curated_chain},{cols.raw_run}",
                output_parent=cols.cp_bias,
                output_run=cols.cp_bias_run,
                qgraph_path=str(qg_bias),
                data_query=(
                    f"instrument='{prof.name}' " "AND exposure.observation_type='bias'"
                ),
                post_query_args=isr_config_args(prof, "cpBiasIsr"),
                jobs=jobs,
            )
            executor.run_pipetask(
                bias_stage.qgraph_args(),
                config,
                log_file=log_file,
            )

            result = executor.run_pipetask(
                bias_stage.run_args(),
                config,
                check=False,
                log_file=log_file,
                output_run=cols.cp_bias_run,
            )
            if result.returncode != 0:
                log.warning(
                    f"Bias pipeline had partial failures for {night} "
                    f"(exit code {result.returncode}). "
                    "Verifying any successfully-built products."
                )

            # A non-zero return code (or even a zero one, when every quantum
            # failed) can leave an empty RUN. Verify the pipeline actually wrote
            # bias products before chaining + certifying: zero products means
            # this night's bias did NOT succeed, regardless of exit code.
            bias_count = (
                butler_query.count_datasets(config, "bias", cols.cp_bias_run) or 0
            )
            if bias_count > 0:
                redefine_chain(
                    config, cols.cp_bias, cols.cp_bias_run, log_file=log_file
                )

                # Certify bias (check=False to handle already-certified case)
                run_butler(
                    [
                        "certify-calibrations",
                        repo,
                        cols.cp_bias,
                        cols.calib_out,
                        "bias",
                        "--begin-date",
                        begin_iso,
                        "--end-date",
                        end_iso,
                    ],
                    config,
                    check=False,
                    log_file=log_file,
                )
                bias_ok = True
            else:
                log.warning(
                    f"Bias pipeline produced no bias products for {night}; "
                    "not certifying."
                )
        except Exception as e:
            log.warning(f"Bias qgraph/setup failed for {night}: {e}")

        # Build flat (continue even if bias had issues — certify what succeeds)
        qg_flat = config.repo / "qgraphs" / f"cp_flat_{night}_{cols.run_ts}.qg"
        flat_ok = False

        try:
            flat_stage = PipetaskStage(
                repo=repo,
                pipeline=str(config.resolve_pipeline("CpFlat.yaml")),
                inputs=f"{cols.curated_chain},{cols.raw_run},{cols.calib_out},{cols.cp_bias_run}",
                output_parent=cols.cp_flat,
                output_run=cols.cp_flat_run,
                qgraph_path=str(qg_flat),
                data_query=(
                    f"instrument='{prof.name}' " "AND exposure.observation_type='flat'"
                ),
                post_query_args=[
                    "-c",
                    "cpFlatIsr:doDark=False",
                    "-c",
                    "cpFlatIsr:doOverscan=True",
                ]
                + isr_config_args(prof, "cpFlatIsr"),
                jobs=jobs,
            )
            executor.run_pipetask(
                flat_stage.qgraph_args(),
                config,
                log_file=log_file,
            )

            result = executor.run_pipetask(
                flat_stage.run_args(),
                config,
                check=False,
                log_file=log_file,
                output_run=cols.cp_flat_run,
            )
            if result.returncode != 0:
                log.warning(
                    f"Flat pipeline had partial failures for {night} "
                    f"(exit code {result.returncode}). "
                    "Verifying any successfully-built products."
                )

            # Verify the pipeline actually wrote flat products before chaining +
            # certifying; zero products means this night's flat did NOT succeed.
            flat_count = (
                butler_query.count_datasets(config, "flat", cols.cp_flat_run) or 0
            )
            if flat_count > 0:
                redefine_chain(
                    config, cols.cp_flat, cols.cp_flat_run, log_file=log_file
                )

                # Certify flat
                run_butler(
                    [
                        "certify-calibrations",
                        repo,
                        cols.cp_flat,
                        cols.calib_out,
                        "flat",
                        "--begin-date",
                        begin_iso,
                        "--end-date",
                        end_iso,
                    ],
                    config,
                    check=False,
                    log_file=log_file,
                )
                flat_ok = True
            else:
                log.warning(
                    f"Flat pipeline produced no flat products for {night}; "
                    "not certifying."
                )
        except Exception as e:
            log.warning(f"Flat qgraph/setup failed for {night}: {e}")

        # Always update unified calib chain (certify what we have)
        run_butler(
            [
                "collection-chain",
                repo,
                cols.calib_chain,
                cols.calib_out,
                cols.curated_chain,
                "--mode",
                "prepend",
            ],
            config,
            check=False,
            log_file=log_file,
        )

        if not bias_ok and not flat_ok:
            return CalibsResult(
                success=False,
                night=night,
                raw_run=cols.raw_run,
                calib_chain=cols.calib_chain,
                cp_bias=cols.cp_bias,
                cp_flat=cols.cp_flat,
                error=(
                    f"Neither bias nor flat produced verified calibration "
                    f"products for {night}"
                ),
            )

        return CalibsResult(
            success=True,
            night=night,
            raw_run=cols.raw_run,
            calib_chain=cols.calib_chain,
            cp_bias=cols.cp_bias,
            cp_flat=cols.cp_flat,
        )

    except Exception as e:
        return CalibsResult(
            success=False,
            night=night,
            raw_run=cols.raw_run,
            calib_chain=cols.calib_chain,
            cp_bias=cols.cp_bias,
            cp_flat=cols.cp_flat,
            error=str(e),
        )
