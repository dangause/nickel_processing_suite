"""Crosstalk calibration: build/certify a declared matrix, or measure one.

Two entry points, both landing a certified ``crosstalk`` (``CrosstalkCalib``)
in ``{prefix}/calib/crosstalk`` and chaining it into the curated calib chain so
ISR resolves it as the ``crosstalk`` prerequisite input:

- :func:`build_and_certify_crosstalk` — the declarative path. Builds a
  ``CrosstalkCalib`` from ``profile.crosstalk.coeffs`` (via the stack-side worker
  ``stips.pipeline_tools.build_crosstalk_calib``) and certifies it. Called once
  per repo from ``calibs.write_curated_calibrations``.
- :func:`measure_crosstalk` — the measurement path. Runs cp_pipe's ``cpCrosstalk``
  pipeline over chosen exposures to derive coefficients, certifies the result, and
  exports the matrix for inspection.

The argument-builders (:func:`build_worker_args`, :func:`certify_args`,
:func:`chain_prepend_args`, :func:`measure_qgraph_args`, :func:`measure_run_args`)
are pure so the constructed commands can be unit-tested without the stack.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from stips.collections import CollectionNames
from stips.core import butler_query
from stips.core.pipeline import (
    find_aliasing_exposure_ids,
    get_raw_dir,
    isr_config_args,
    validate_night,
)
from stips.core.stack import (
    run_butler,
    run_butler_python_json,
    run_with_stack,
)

if TYPE_CHECKING:
    from stips.core.config import Config

log = logging.getLogger(__name__)

#: Stack-side worker module that builds the CrosstalkCalib and puts it in Butler.
WORKER_MODULE = "stips.pipeline_tools.build_crosstalk_calib"
#: Dataset type / storage class name for the crosstalk calib.
CROSSTALK_DATASET = "crosstalk"
#: Crosstalk is a fixed instrument property, so it is certified over a wide window.
WIDE_BEGIN = "1970-01-01"
WIDE_END = "2100-01-01"
#: cp_pipe crosstalk-measurement pipeline, relative to ``cp_pipe_dir``.
CP_CROSSTALK_PIPELINE = "pipelines/_ingredients/cpCrosstalk.yaml"


# --------------------------------------------------------------------------- #
# Pure argument builders (unit-tested without the stack)
# --------------------------------------------------------------------------- #
def build_worker_args(
    *, repo: str, instrument: str, run: str, coeffs, units: str = "adu"
) -> list[str]:
    """``python -m`` invocation for the stack-side CrosstalkCalib builder."""
    return [
        "python",
        "-m",
        WORKER_MODULE,
        "--repo",
        repo,
        "--instrument",
        instrument,
        "--run",
        run,
        "--coeffs-json",
        json.dumps(coeffs),
        "--units",
        units,
    ]


def certify_args(
    repo: str,
    input_coll: str,
    calib_coll: str,
    *,
    dataset: str = CROSSTALK_DATASET,
    begin: str = WIDE_BEGIN,
    end: str = WIDE_END,
) -> list[str]:
    """``butler certify-calibrations`` args for a crosstalk calib (wide window)."""
    return [
        "certify-calibrations",
        repo,
        input_coll,
        calib_coll,
        dataset,
        "--begin-date",
        begin,
        "--end-date",
        end,
    ]


def chain_prepend_args(repo: str, chain: str, child: str) -> list[str]:
    """``butler collection-chain`` args to prepend ``child`` into ``chain``."""
    return ["collection-chain", repo, chain, child, "--mode", "prepend"]


def measure_qgraph_args(
    *,
    repo: str,
    pipeline: str,
    inputs: str,
    output: str,
    output_run: str,
    qgraph_path: str,
    where: str,
    isr_args: list[str],
    datastore_records: bool = False,
) -> list[str]:
    """``pipetask qgraph`` args for the cp_pipe crosstalk-measurement pipeline."""
    args = [
        "qgraph",
        "-b",
        repo,
        "-p",
        pipeline,
        "-i",
        inputs,
        "-o",
        output,
        "--output-run",
        output_run,
        "--save-qgraph",
        qgraph_path,
        "-d",
        where,
        *isr_args,
    ]
    if datastore_records:
        args.append("--qgraph-datastore-records")
    return args


def measure_run_args(repo: str, qgraph_path: str, jobs: int) -> list[str]:
    """``pipetask run`` args to execute a saved crosstalk-measurement qgraph."""
    return [
        "run",
        "-b",
        repo,
        "-g",
        qgraph_path,
        "-j",
        str(jobs),
        "--register-dataset-types",
    ]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
@dataclass
class CrosstalkResult:
    """Outcome of a crosstalk build/measurement."""

    success: bool
    calib_collection: str
    detectors: list[int]
    export_path: Path | None = None
    error: str | None = None


def build_and_certify_crosstalk(
    night: str,
    config: Config,
    *,
    log_file: Path | None = None,
) -> CrosstalkResult:
    """Build a CrosstalkCalib from ``profile.crosstalk`` and certify it.

    Builds the calib into ``crosstalk_gen`` (RUN), certifies it into
    ``crosstalk_calib`` (CALIBRATION). Chaining ``crosstalk_calib`` into the
    curated chain is the caller's responsibility (so the chain is redefined once,
    including both defects and crosstalk). Returns a :class:`CrosstalkResult`;
    ``success=False`` (with ``error`` set) if the profile declares no crosstalk or
    the build fails — the caller then chains only the curated (defects) run.
    """
    prof = config.require_profile()
    if prof.crosstalk is None:
        return CrosstalkResult(False, "", [], error="profile declares no crosstalk")

    night = validate_night(night)
    cols = CollectionNames(night, prefix=prof.collection_prefix)
    repo = str(config.repo)

    # Idempotent: the crosstalk calib is a static, repo-level product certified
    # into a shared CALIBRATION collection. If it already exists, skip the
    # rebuild + re-certify — re-certifying the same matrix raises
    # ConflictingDefinitionError on that collection, which is what broke
    # multi-night calibs (every night after the first re-certified and conflicted,
    # with a fragile cascade that could fail the whole night's calibs).
    already = (
        butler_query.list_collections(
            config,
            cols.crosstalk_calib,
            prefix=f"{prof.collection_prefix}/",
        )
        or []
    )
    if already:
        log.info(
            "Crosstalk calib already certified in %s; skipping rebuild/re-certify",
            cols.crosstalk_calib,
        )
        return CrosstalkResult(True, cols.crosstalk_calib, [])

    worker = run_with_stack(
        build_worker_args(
            repo=repo,
            instrument=prof.name,
            run=cols.crosstalk_gen,
            coeffs=prof.crosstalk.coeffs,
            units=prof.crosstalk.units,
        ),
        config,
        capture_output=True,
        check=False,
    )
    if worker.returncode != 0:
        log.warning(
            "Crosstalk calib build failed (exit %s); skipping crosstalk.\n%s",
            worker.returncode,
            (worker.stderr or "").strip()[-2000:],
        )
        return CrosstalkResult(
            False, "", [], error=f"build failed (exit {worker.returncode})"
        )

    detectors = _parse_worker_detectors(worker.stdout)

    _certify_crosstalk(config, cols.crosstalk_gen, cols, log_file=log_file)
    log.info(
        "Certified declarative crosstalk calib for %d detector(s) into %s",
        len(detectors),
        cols.crosstalk_calib,
    )
    return CrosstalkResult(True, cols.crosstalk_calib, detectors)


def _certify_crosstalk(config, source_run, cols, *, log_file=None) -> None:
    """Certify the ``crosstalk`` dataset from ``source_run`` into the calib chain.

    Shared by the declarative and measurement paths so the dataset name and
    validity window stay in lockstep. ``check=False`` tolerates re-certification on
    a re-run.
    """
    run_butler(
        certify_args(str(config.repo), source_run, cols.crosstalk_calib),
        config,
        check=False,
        log_file=log_file,
    )


def _parse_worker_detectors(stdout: str | None) -> list[int]:
    """Pull the detector-id list from the worker's final JSON line (best effort)."""
    if not stdout:
        return []
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return list(json.loads(line).get("detectors", []))
            except json.JSONDecodeError:
                continue
    return []


def measure_crosstalk(
    nights: list[str],
    config: Config,
    *,
    executor=None,
    jobs: int = 1,
    export_dir: Path | None = None,
    log_file: Path | None = None,
) -> CrosstalkResult:
    """Measure crosstalk from exposures via cp_pipe and certify the result.

    Runs cp_pipe's ``cpCrosstalk`` pipeline (ISR → extract → solve) over the
    ingested raws of ``nights`` — reusing the profile's ``isr_overrides`` on the
    measurement ISR (but NOT applying crosstalk while measuring it) — then
    certifies the produced ``crosstalk`` calib into ``crosstalk_calib``, chains it
    into the curated chain, and exports the matrix (ECSV) for inspection.

    Intended to be run once (e.g. at bootstrap) when no coefficients are known.
    Requires bias calibs to be built first (the measurement ISR applies bias).
    """
    from stips.core.executor import LocalExecutor

    if executor is None:
        executor = LocalExecutor()

    prof = config.require_profile()
    repo = str(config.repo)
    nights = [validate_night(n) for n in nights]
    cols = CollectionNames(nights[0], prefix=prof.collection_prefix)

    if config.cp_pipe_dir is None:
        return CrosstalkResult(False, "", [], error="CP_PIPE_DIR not configured")
    pipeline = str(Path(config.cp_pipe_dir) / CP_CROSSTALK_PIPELINE)

    raw_runs = _resolve_raw_runs(nights, config, prof, log_file=log_file)
    if not raw_runs:
        return CrosstalkResult(
            False, "", [], error="no raw collections found/ingested for measurement"
        )

    inputs = ",".join([cols.calib_chain, *raw_runs])
    qgraph = config.repo / "qgraphs" / f"cpCrosstalk_{cols.run_ts}.qg"
    qgraph.parent.mkdir(parents=True, exist_ok=True)

    # Measurement ISR must NOT apply crosstalk (include_crosstalk=False), but must
    # match the rest of the pipeline's overscan/defect handling.
    isr_args = isr_config_args(prof, "cpCrosstalkIsr", include_crosstalk=False)

    try:
        executor.run_pipetask(
            measure_qgraph_args(
                repo=repo,
                pipeline=pipeline,
                inputs=inputs,
                output=cols.crosstalk_gen,
                output_run=f"{cols.crosstalk_gen}/run",
                qgraph_path=str(qgraph),
                where=f"instrument='{prof.name}' AND "
                f"exposure.observation_type='science'",
                isr_args=isr_args,
            ),
            config,
            log_file=log_file,
        )
        result = executor.run_pipetask(
            measure_run_args(repo, str(qgraph), jobs),
            config,
            check=False,
            log_file=log_file,
            output_run=f"{cols.crosstalk_gen}/run",
        )
    except Exception as e:  # noqa: BLE001 - surface as a result, not a crash
        return CrosstalkResult(False, "", [], error=f"measurement pipeline failed: {e}")

    if result.returncode != 0:
        return CrosstalkResult(
            False, "", [], error=f"measurement pipeline exit {result.returncode}"
        )

    # The solve output lands in the gen RUN; certify from there, then ensure the
    # crosstalk calib is in the curated chain ISR reads.
    _certify_crosstalk(config, f"{cols.crosstalk_gen}/run", cols, log_file=log_file)
    run_butler(
        chain_prepend_args(repo, cols.curated_chain, cols.crosstalk_calib),
        config,
        check=False,  # tolerate already-chained on re-run
        log_file=log_file,
    )

    export_path = _export_matrix(
        config, prof, cols.crosstalk_calib, export_dir, log_file=log_file
    )
    log.info("Measured crosstalk certified into %s", cols.crosstalk_calib)
    return CrosstalkResult(True, cols.crosstalk_calib, [], export_path=export_path)


def _resolve_raw_runs(nights, config, prof, *, log_file=None) -> list[str]:
    """Return a raw RUN collection per night, reusing existing ones or ingesting.

    Crosstalk is normally measured after calibs/science, so the night's raws are
    usually already ingested — reuse the existing ``{prefix}/raw/{night}/*``
    collection (same discovery the science/dia paths use) rather than re-ingesting
    (``ingest-raws`` skips already-registered exposures, leaving an empty new RUN).
    Only ingest when no raw collection exists yet.
    """
    repo = str(config.repo)
    raw_runs: list[str] = []
    did_ingest = False
    for night in nights:
        existing = (
            butler_query.list_collections(
                config,
                f"{prof.collection_prefix}/raw/{night}/*",
                prefix=f"{prof.collection_prefix}/",
            )
            or []
        )
        if existing:
            raw_runs.append(existing[0])
            continue

        raw_dir = get_raw_dir(config, night)
        if not raw_dir.exists():
            log.warning(
                "No raw collection or raw dir for %s (%s); skipping", night, raw_dir
            )
            continue
        # Same pre-ingest aliasing guard as calibs: a folded sequence keyword
        # (Nickel's OBSNUM % 10000) can collapse two frames onto one
        # exposure_id, and this is the only layer that sees the whole night.
        collisions = find_aliasing_exposure_ids(config, night)
        if collisions:
            detail = "; ".join(
                f"exposure_id {exp_id} claimed by "
                + ", ".join(f"{name} ({obs_id})" for name, obs_id in frames)
                for exp_id, frames in sorted(collisions.items())
            )
            log.error(
                "Colliding exposure_ids in %s: %s. Skipping this night's "
                "crosstalk ingest; move the offending frames out of the raw "
                "dir or widen the profile's exposure_id scheme.",
                night,
                detail,
            )
            continue
        cols = CollectionNames(night, prefix=prof.collection_prefix)
        run_butler(
            ["register-instrument", repo, prof.instrument_class],
            config,
            check=False,
            log_file=log_file,
        )
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
        raw_runs.append(cols.raw_run)
        did_ingest = True
    if did_ingest:
        run_butler(
            ["define-visits", repo, prof.name], config, check=False, log_file=log_file
        )
    return raw_runs


def _export_matrix(config, prof, calib_collection, export_dir, *, log_file=None):
    """Fetch the certified crosstalk calib and write it as ECSV (best effort).

    Exports detector 0's calib; the matrix is declared per-camera so all detectors
    share it. Informational only — the export path is NOT a Butler discovery
    location (the applied calib is always the one certified into
    ``calib_collection``). Returns the written path, or None on failure.
    """
    if export_dir is None:
        export_dir = config.repo / "crosstalk"
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / f"{prof.name}_crosstalk.ecsv"

    script = (
        "import json\n"
        "from lsst.daf.butler import Butler\n"
        f"b = Butler({str(config.repo)!r}, collections=[{calib_collection!r}])\n"
        f"c = b.get('crosstalk', instrument={prof.name!r}, detector=0)\n"
        f"c.writeText({str(out_path)!r})\n"
        f"print(json.dumps({{'out': {str(out_path)!r}}}))\n"
    )
    result = run_butler_python_json(script, config)
    if result and result.get("out"):
        log.info("Exported crosstalk matrix to %s", out_path)
        return out_path
    log.warning(
        "Crosstalk export failed (calib remains certified in %s)", calib_collection
    )
    return None
