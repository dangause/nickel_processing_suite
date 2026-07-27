"""On-demand Gaia/PS1 reference-catalog orchestration (no RSP/MONSTER).

``ensure_refcats`` is called from ``stips run`` before the science step. It is
idempotent: it computes the HTM7 trixels covering the target cone, checks which
are already present in the Butler ``refcats`` collection, and only fetches /
converts / ingests the missing ones.

No ``lsst.*`` or ``stips_refcats`` import happens at module load — stack
access is confined to ``run_butler`` / the ``butler_query`` adapters (mocked in
unit tests), and ``stips_refcats`` symbols are bound lazily on first use so
importing this module (and hence the CLI) never fails just because
``stips-refcats`` is broken or missing. The HTM cone-coverage math needs
``lsst.geom`` at call time, so ``_cones_to_htm_ids`` computes it via an
in-stack snippet when ``lsst`` is not importable in-process (the normal
plain-venv case).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from stips.core import butler_query
from stips.core.stack import run_butler, run_butler_python_json, run_with_stack

if TYPE_CHECKING:
    # Static-analysis-only bindings for the lazily loaded names below.
    from stips_refcats.convert import convert_catalog
    from stips_refcats.coverage import missing_trixels
    from stips_refcats.gaia import fetch_gaia_cone
    from stips_refcats.htm import cones_to_htm
    from stips_refcats.ps1 import PS1FootprintError, fetch_ps1_cone

    from stips.core.config import Config

log = logging.getLogger(__name__)

#: stips_refcats symbols bound lazily into module globals by
#: ``_load_refcats`` (and via PEP 562 ``__getattr__`` for attribute
#: access, e.g. monkeypatching in tests).
_REFCATS_NAMES = (
    "convert_catalog",
    "missing_trixels",
    "fetch_gaia_cone",
    "cones_to_htm",
    "PS1FootprintError",
    "fetch_ps1_cone",
)


def _load_refcats() -> None:
    """Bind ``stips_refcats`` symbols into module globals on first use.

    Deferred so that importing ``stips.core.refcat`` (and therefore the
    ``stips`` CLI) does not require ``stips-refcats`` to be importable;
    a broken install only surfaces when refcat functionality is invoked.
    ``setdefault`` keeps any already-bound value (e.g. a test monkeypatch).
    """
    g = globals()
    if all(name in g for name in _REFCATS_NAMES):
        return
    from stips_refcats.convert import convert_catalog
    from stips_refcats.coverage import missing_trixels
    from stips_refcats.gaia import fetch_gaia_cone
    from stips_refcats.htm import cones_to_htm
    from stips_refcats.ps1 import PS1FootprintError, fetch_ps1_cone

    for name, value in (
        ("convert_catalog", convert_catalog),
        ("missing_trixels", missing_trixels),
        ("fetch_gaia_cone", fetch_gaia_cone),
        ("cones_to_htm", cones_to_htm),
        ("PS1FootprintError", PS1FootprintError),
        ("fetch_ps1_cone", fetch_ps1_cone),
    ):
        g.setdefault(name, value)


def __getattr__(name: str):
    if name in _REFCATS_NAMES:
        _load_refcats()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Butler dataset-type names (must match the convert configs + colorterm aliases).
GAIA_DATASET = "gaia_dr3"
PS1_DATASET = "panstarrs1_dr2"

#: convertReferenceCatalog config files (shipped as stips_refcats package data).
GAIA_CONVERT_CONFIG = "gaia_dr3_config.py"
PS1_CONVERT_CONFIG = "ps1_config.py"

#: calibrateImage overlay that switches refcats to Gaia/PS1 (opt-in).
GAIA_PS1_OVERLAY = "refcats_gaia_ps1.py"
# Gaia-only overlay: astrometry AND photometry from Gaia DR3, for fields south of
# PS1's -30deg floor (most of CTIO's sky). See refcats_gaia_only.py.
GAIA_ONLY_OVERLAY = "refcats_gaia_only.py"


def refcat_overlay_config(mode: str) -> str | None:
    """calibrateImage overlay config name for a refcat mode.

    ``None`` means "use the DRP.yaml default" — which is currently MONSTER, so
    ``mode="monster"`` needs no overlay. ``mode="gaia_ps1"`` applies the Gaia/PS1
    overlay on top of the tuned config. ``mode="gaia"`` applies the Gaia-only
    overlay (photometry from Gaia too), for southern fields with no PS1 coverage.
    After the default is flipped to Gaia/PS1, this inverts (monster gets an
    overlay, gaia_ps1 returns None).
    """
    if mode == "gaia_ps1":
        return GAIA_PS1_OVERLAY
    if mode == "gaia":
        return GAIA_ONLY_OVERLAY
    return None


@dataclass
class RefcatResult:
    """Outcome of an ``ensure_refcats`` call."""

    mode: str
    gaia_status: str | None = None  # covered | fetched | skipped | failed
    ps1_status: str | None = None
    collections: list[str] = field(default_factory=list)
    needed_trixels: int = 0
    error: str | None = None


def _query_present_htm7(config: "Config", dataset_type: str) -> set[int]:
    """Return the htm7 ids already present for ``dataset_type`` in ``refcats``.

    Delegates to the structured ``butler_query`` adapter, which runs an in-stack
    Butler Python query and returns the htm7 dataId values as JSON — replacing
    the old regex scrape of ``butler query-datasets`` CLI table text (where any
    numeric column, e.g. a run id or size, could be mistaken for a present
    trixel). Isolated behind this function so unit tests can mock it directly.

    A missing dataset type / collection reads as "none present" (empty set). If
    the in-stack query itself fails to run (adapter returns ``None``), that is
    logged and also treated as none-present, preserving the prior rc!=0 behavior
    while surfacing the failure.
    """
    ids = butler_query.dataset_data_id_values(config, dataset_type, "refcats", "htm7")
    if ids is None:
        log.warning(
            "Could not query present htm7 trixels for %s (in-stack query failed); "
            "treating as none present",
            dataset_type,
        )
        return set()
    return set(ids)


def present_trixels(config: "Config", dataset_type: str) -> set[int]:
    """HTM7 trixels already covered for ``dataset_type``."""
    return _query_present_htm7(config, dataset_type)


def _ingest_refcat(*, config: "Config", name: str, ecsv_map: str, stamp: str) -> str:
    """Register, ingest, and chain a converted refcat into ``refcats``.

    Ingests into a TIMESTAMPED RUN collection (``refcats/<name>/<stamp>``) and
    extends the unified ``refcats`` chain with it. Using a fresh RUN per fetch
    means a re-fetch never collides with already-ingested shards (a fixed RUN
    would raise ConflictingDefinitionError); the CHAINED parent de-duplicates via
    find-first, so the newest run's shards win.

    Returns the timestamped RUN collection name.
    """
    repo = str(config.repo)
    run_collection = f"refcats/{name}/{stamp}"

    # 1) Register the dataset type (tolerate "already exists").
    run_butler(
        ["register-dataset-type", repo, name, "SimpleCatalog", "htm7"],
        config,
        check=False,
    )
    # 2) Ingest the sharded FITS in place (direct mode) into the timestamped RUN.
    run_butler(
        ["ingest-files", "-t", "direct", repo, name, run_collection, ecsv_map],
        config,
        check=True,
    )
    # 3) Extend the unified refcats chain with this RUN collection.
    run_butler(
        ["collection-chain", repo, "--mode", "extend", "refcats", run_collection],
        config,
        check=True,
    )
    return run_collection


def _requested_path(config: "Config") -> Path:
    """Sidecar manifest of HTM7 trixels ever requested per catalog (per repo)."""
    return Path(str(config.repo)) / "refcats_requested.json"


def _load_requested(config: "Config") -> dict[str, set[int]]:
    """Load the per-repo requested-trixels manifest (empty on any error)."""
    try:
        raw = json.loads(_requested_path(config).read_text())
        return {k: set(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001 - missing/unreadable manifest => nothing recorded
        return {}


def _record_requested(config: "Config", name: str, trixels: set[int]) -> None:
    """Union ``trixels`` into the manifest for ``name`` (best-effort).

    Records every trixel we asked the survey for, even ones that turned out to
    contain no usable sources (so no shard was ingested). Coverage then treats
    those legitimately-empty trixels as covered instead of re-fetching forever.
    """
    try:
        current = _load_requested(config)
        current.setdefault(name, set()).update(trixels)
        path = _requested_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({k: sorted(v) for k, v in current.items()}, indent=2)
        )
    except Exception as exc:  # noqa: BLE001 - manifest is an optimization, not critical
        log.debug("Could not record requested trixels for %s: %s", name, exc)


def _convert_config_path(config_name: str) -> Path:
    """Filesystem path to a convertReferenceCatalog config shipped with stips_refcats.

    The configs live as package data under ``stips_refcats/configs/`` and are
    resolved via ``importlib.resources``. ``convertReferenceCatalog`` reads them
    as file PATHS (they are LSST injected-``config`` files, not importable
    modules), so we return a concrete filesystem path — valid here because the
    package is always a normal (non-zipped) install.
    """
    from importlib.resources import files

    return Path(str(files("stips_refcats").joinpath("configs", config_name)))


def _refcat_out_dir(config: "Config", name: str, stamp: str) -> Path:
    """Output directory for a converted refcat under the REFCAT_REPO layout."""
    base = getattr(config, "refcat_repo", None) or getattr(config, "repo", ".")
    return Path(str(base)) / "data" / "refcats" / f"{name}-{stamp}"


def _ensure_one(
    config: "Config",
    name: str,
    needed: set[int],
    force: bool,
    *,
    fetch,
    convert_config: str,
    result: RefcatResult,
    stamp: str,
) -> str:
    """Ensure one catalog covers ``needed``; fetch+convert+ingest if not.

    Returns ``"covered"`` (already present) or ``"fetched"`` (newly ingested).
    """
    _load_refcats()
    # A needed trixel is satisfied if it has an ingested shard OR we already
    # requested it (it may legitimately contain no usable sources, so it never
    # gets a shard — without this it would be re-fetched on every run).
    covered = present_trixels(config, name) | _load_requested(config).get(name, set())
    if not force and not missing_trixels(needed, covered):
        return "covered"

    out_dir = _refcat_out_dir(config, name, stamp)
    out_csv = out_dir / f"{name}.csv"
    fetch(out_csv)
    ecsv = _convert_catalog_venv_safe(
        config,
        name,
        out_csv,
        _convert_config_path(convert_config),
        out_dir,
        force=force,
    )
    collection = _ingest_refcat(
        config=config, name=name, ecsv_map=str(ecsv), stamp=stamp
    )
    # Record every requested trixel (including empty ones) so re-runs are no-ops.
    _record_requested(config, name, needed)
    result.collections.append(collection)
    return "fetched"


def _convert_catalog_venv_safe(
    config: "Config",
    name: str,
    source_csv: Path,
    config_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
) -> Path:
    """Run ``convertReferenceCatalog`` venv-safely.

    ``stips_refcats.convert_catalog`` shells out to the bare
    ``convertReferenceCatalog`` CLI, which is only on PATH inside the LSST
    stack. Try it first (works in-stack, and unit tests patch it); when the
    binary is absent (the normal plain-venv case), rerun the identical
    invocation through ``run_with_stack``.
    """
    try:
        return convert_catalog(name, source_csv, config_path, out_dir, force=force)
    except FileNotFoundError as exc:
        if "convertReferenceCatalog" not in str(exc):
            raise  # missing CSV / missing output map — a real error
        log.debug("convertReferenceCatalog not on PATH; rerunning in-stack")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmap = out_dir / "filename_to_htm.ecsv"
    if fmap.exists() and not force:
        return fmap
    if not Path(source_csv).exists():
        raise FileNotFoundError(f"[{name}] Missing input CSV: {source_csv}")
    run_with_stack(
        ["convertReferenceCatalog", str(out_dir), str(config_path), str(source_csv)],
        config,
    )
    if not fmap.exists():
        raise FileNotFoundError(
            f"[{name}] Expected map not found after conversion: {fmap}"
        )
    return fmap


def _cones_to_htm_ids(
    config: "Config", cones: list[tuple[float, float, float]], *, depth: int = 7
) -> set[int]:
    """HTM trixel ids covering ``cones``, computed venv-safely.

    ``stips_refcats.cones_to_htm`` needs ``lsst.geom``/``meas_algorithms`` at
    call time, which the plain venv does not have. Try it in-process first (it
    works inside the stack, and unit tests patch it), then fall back to running
    the same computation in-stack via ``run_butler_python_json``.
    """
    _load_refcats()
    try:
        return set(cones_to_htm(cones, depth=depth))
    except (ImportError, ModuleNotFoundError):
        log.debug("lsst not importable in-process; computing HTM cones in-stack")
    script = f"""
import json
import lsst.geom as geom
from lsst.meas.algorithms.htmIndexer import HtmIndexer

htm = HtmIndexer(depth={depth!r})
ids = set()
for ra, dec, rad_deg in {cones!r}:
    center = geom.SpherePoint(ra * geom.degrees, dec * geom.degrees)
    shards, _ = htm.getShardIds(center, rad_deg * geom.degrees)
    ids.update(int(s) for s in shards)
print(json.dumps(sorted(ids)))
"""
    result = run_butler_python_json(script, config)
    if result is None:
        raise RuntimeError(
            "Could not compute HTM coverage for the refcat cone: the in-stack "
            "helper failed (is STACK_DIR valid and the LSST stack installed?). "
            "See WARNING logs above for the underlying error."
        )
    return {int(x) for x in result}


def ensure_refcats(
    config: "Config",
    ra: float,
    dec: float,
    *,
    radius_deg: float = 0.3,
    mode: str = "gaia_ps1",
    force: bool = False,
    gaia_quality: dict | None = None,
) -> RefcatResult:
    """Ensure Gaia (astrometry) + PS1 (photometry) refcats cover the target cone.

    Idempotent: skips catalogs whose HTM7 coverage already exists. ``mode``
    ``"monster"`` is a no-op (the MONSTER path manages its own coverage).
    """
    result = RefcatResult(mode=mode)
    if mode == "monster":
        return result

    _load_refcats()
    needed = _cones_to_htm_ids(config, [(ra, dec, radius_deg)], depth=7)
    result.needed_trixels = len(needed)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Gaia DR3 — astrometry, all-sky.
    try:
        result.gaia_status = _ensure_one(
            config,
            GAIA_DATASET,
            needed,
            force,
            fetch=lambda csv: fetch_gaia_cone(
                ra, dec, radius_deg, out_csv=csv, **(gaia_quality or {})
            ),
            convert_config=GAIA_CONVERT_CONFIG,
            result=result,
            stamp=stamp,
        )
    except Exception as exc:  # noqa: BLE001 - record and continue to PS1
        result.gaia_status = "failed"
        result.error = f"gaia: {exc}"
        log.warning("Gaia refcat ensure failed: %s", exc)

    # mode="gaia": photometry comes from Gaia too (southern fields, no PS1
    # coverage), so skip the PS1 fetch entirely.
    if mode == "gaia":
        result.ps1_status = "skipped"
        return result

    # PS1 DR2 — photometry, Dec > -30 only.
    try:
        result.ps1_status = _ensure_one(
            config,
            PS1_DATASET,
            needed,
            force,
            fetch=lambda csv: fetch_ps1_cone(ra, dec, radius_deg, out_csv=csv),
            convert_config=PS1_CONVERT_CONFIG,
            result=result,
            stamp=stamp,
        )
    except PS1FootprintError as exc:
        result.ps1_status = "skipped"
        log.warning("PS1 skipped (footprint): %s", exc)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - record; Gaia astrometry may still be usable
        result.ps1_status = "failed"
        result.error = ((result.error or "") + f" ps1: {exc}").strip()
        log.warning("PS1 refcat ensure failed: %s", exc)

    return result
