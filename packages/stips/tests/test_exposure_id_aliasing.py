"""Pre-ingest scan for exposure_id aliasing (``find_aliasing_exposure_ids``).

Instrument profiles pack a day term and a 4-digit sequence into a 31-bit
``exposure_id``. When the raw header's sequence keyword is wider than that field
the profile must fold it (Nickel: ``OBSNUM % 10000``), and a fold is only
injective within one window -- two frames on the same day whose sequence numbers
differ by an exact multiple of the window collapse onto one id.

Neither the profile hook (one header at a time, no cross-frame state) nor
``pack_exposure_id`` (the folded value is in range by construction) can see that.
This scan is the layer that can: it walks a night's raw headers before ingest and
reports any id claimed by more than one frame, so ``stips calibs`` aborts naming
the files instead of silently collapsing two exposures into one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stips.core import calibs, crosstalk, pipeline  # noqa: E402
from stips.core.pipeline import find_aliasing_exposure_ids  # noqa: E402

fits = pytest.importorskip("astropy.io.fits")

NIGHT = "20230519"


def _profile():
    """A minimal profile whose hooks fold a wide OBSNUM, like Nickel's."""

    def exposure_id(header):
        day = int(str(header["DATE-END"])[:10].replace("-", ""))
        return day * 10000 + int(header["OBSNUM"]) % 10000

    def observation_id(header):
        return (
            f"{str(header['DATE-END'])[:10].replace('-', '')}_{int(header['OBSNUM'])}"
        )

    return SimpleNamespace(
        hooks={"exposure_id": exposure_id, "observation_id": observation_id}
    )


def _config(tmp_path: Path):
    return SimpleNamespace(
        raw_parent_dir=tmp_path,
        profile=_profile(),
        require_profile=lambda: _profile(),
    )


def _write_raw(
    tmp_path: Path, name: str, obsnum: int, date_end: str = "2023-05-20T05:00:00.00"
):
    raw_dir = tmp_path / NIGHT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU()
    hdu.header["OBSNUM"] = obsnum
    hdu.header["DATE-END"] = date_end
    hdu.writeto(raw_dir / name, overwrite=True)


def test_clean_night_reports_no_collisions(tmp_path):
    """A real night spans a few hundred in OBSNUM -- nothing aliases."""
    for n in range(228001, 228020):
        _write_raw(tmp_path, f"d{n}.fits", n)
    assert find_aliasing_exposure_ids(_config(tmp_path), NIGHT) == {}


def test_night_crossing_a_fold_boundary_is_clean(tmp_path):
    """Crossing a multiple of 10,000 is fine; only a >=10,000 SPAN aliases."""
    for n in (229998, 229999, 230000, 230001):
        _write_raw(tmp_path, f"d{n}.fits", n)
    assert find_aliasing_exposure_ids(_config(tmp_path), NIGHT) == {}


def test_exact_multiple_of_the_window_is_reported(tmp_path):
    """OBSNUMs 10,000 apart on the same day fold to the same id -> reported."""
    _write_raw(tmp_path, "d228001.fits", 228001)
    _write_raw(tmp_path, "d238001.fits", 238001)

    collisions = find_aliasing_exposure_ids(_config(tmp_path), NIGHT)

    assert len(collisions) == 1
    ((exp_id, frames),) = collisions.items()
    assert exp_id == 20230520 * 10000 + 8001
    names = sorted(name for name, _obs_id in frames)
    assert names == ["d228001.fits", "d238001.fits"]
    # Both source frames are named, with their distinct observation_ids, so the
    # abort message can point at the actual files.
    assert sorted(obs_id for _name, obs_id in frames) == [
        "20230520_228001",
        "20230520_238001",
    ]


def test_same_frame_seen_twice_is_not_a_collision(tmp_path):
    """Identical observation_id (e.g. a duplicated copy) is not an alias."""
    _write_raw(tmp_path, "d228001.fits", 228001)
    _write_raw(tmp_path, "d228001_copy.fits", 228001)
    assert find_aliasing_exposure_ids(_config(tmp_path), NIGHT) == {}


def test_different_days_do_not_collide(tmp_path):
    """The day term separates equal folded sequences on different days."""
    _write_raw(tmp_path, "d228001.fits", 228001, date_end="2023-05-20T05:00:00.00")
    _write_raw(tmp_path, "d238001.fits", 238001, date_end="2023-05-21T05:00:00.00")
    assert find_aliasing_exposure_ids(_config(tmp_path), NIGHT) == {}


def test_unreadable_header_is_skipped_not_fatal(tmp_path):
    """A frame the hooks cannot translate is left for ingest to reject.

    The scan must never be the thing that blocks a night it does not understand.
    """
    for n in (228001, 228002):
        _write_raw(tmp_path, f"d{n}.fits", n)
    (tmp_path / NIGHT / "raw" / "junk.fits").write_bytes(b"not a fits file")
    assert find_aliasing_exposure_ids(_config(tmp_path), NIGHT) == {}


def test_missing_raw_dir_reports_nothing(tmp_path):
    assert find_aliasing_exposure_ids(_config(tmp_path), "19990101") == {}


def test_missing_hooks_reports_nothing(tmp_path):
    """A profile without the hooks cannot alias; the scan is a no-op."""
    _write_raw(tmp_path, "d228001.fits", 228001)
    config = SimpleNamespace(
        raw_parent_dir=tmp_path,
        profile=SimpleNamespace(hooks={}),
        require_profile=lambda: SimpleNamespace(hooks={}),
    )
    assert find_aliasing_exposure_ids(config, NIGHT) == {}


# --------------------------------------------------------------------------- #
# The scan is wired in: `stips calibs` aborts BEFORE ingest when it fires.
# --------------------------------------------------------------------------- #


def _calibs_config(tmp_path: Path):
    config = MagicMock()
    config.repo = tmp_path
    config.cp_pipe_dir = "/cp"
    prof = MagicMock()
    prof.collection_prefix = "Nickel"
    prof.name = "Nickel"
    config.require_profile.return_value = prof
    return config


def _run_calibs(tmp_path: Path, collisions):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    run_butler = MagicMock(name="run_butler")
    run_butler.return_value = SimpleNamespace(returncode=0, stderr="")
    bq = MagicMock(name="butler_query")
    bq.count_datasets.return_value = 0

    with (
        patch.object(calibs, "run_butler", run_butler),
        # register-instrument and the chain redefines go through the hoisted
        # pipeline helpers, which call pipeline.run_butler.
        patch.object(pipeline, "run_butler", run_butler),
        patch.object(calibs, "butler_query", bq),
        patch.object(calibs, "get_raw_dir", return_value=raw_dir),
        patch.object(calibs, "find_aliasing_exposure_ids", return_value=collisions),
    ):
        result = calibs.run(
            NIGHT,
            _calibs_config(tmp_path),
            jobs=1,
            executor=MagicMock(),
            skip_curated=True,
        )
    return result, run_butler


def test_calibs_aborts_and_names_the_colliding_frames(tmp_path):
    result, run_butler = _run_calibs(
        tmp_path,
        {
            202305208001: [
                ("d228001.fits", "20230520_228001"),
                ("d238001.fits", "20230520_238001"),
            ]
        },
    )

    assert result.success is False
    assert "202305208001" in (result.error or "")
    assert "d228001.fits" in result.error and "d238001.fits" in result.error
    # Aborted BEFORE ingest — nothing was handed to butler.
    assert run_butler.call_args_list == []


def test_calibs_proceeds_when_the_scan_is_clean(tmp_path):
    result, run_butler = _run_calibs(tmp_path, {})

    # It still fails (no products were built by the mocked pipelines), but for a
    # DIFFERENT reason — the scan did not block it, and ingest was attempted.
    assert "Colliding exposure_ids" not in (result.error or "")
    commands = [c.args[0][0] for c in run_butler.call_args_list if c.args and c.args[0]]
    assert "ingest-raws" in commands


# --------------------------------------------------------------------------- #
# The scan is also wired into crosstalk's separate ingest path
# (`_resolve_raw_runs`): a colliding night is skipped, not ingested.
# --------------------------------------------------------------------------- #


def _run_crosstalk_resolve(tmp_path: Path, collisions, *, existing=None):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    run_butler = MagicMock(name="run_butler")
    run_butler.return_value = SimpleNamespace(returncode=0, stderr="")
    bq = MagicMock(name="butler_query")
    bq.list_collections.return_value = existing or []
    scan = MagicMock(name="find_aliasing_exposure_ids", return_value=collisions)

    config = MagicMock()
    config.repo = tmp_path
    prof = MagicMock()
    prof.collection_prefix = "Nickel"
    prof.name = "Nickel"
    prof.instrument_class = "lsst.obs.stips.active.Instrument"

    with (
        patch.object(crosstalk, "run_butler", run_butler),
        patch.object(crosstalk, "butler_query", bq),
        patch.object(crosstalk, "get_raw_dir", return_value=raw_dir),
        patch.object(crosstalk, "find_aliasing_exposure_ids", scan),
    ):
        raw_runs = crosstalk._resolve_raw_runs([NIGHT], config, prof)
    return raw_runs, run_butler, scan


def test_crosstalk_skips_a_colliding_night_before_ingest(tmp_path):
    raw_runs, run_butler, _ = _run_crosstalk_resolve(
        tmp_path,
        {
            202305208001: [
                ("d228001.fits", "20230520_228001"),
                ("d238001.fits", "20230520_238001"),
            ]
        },
    )

    assert raw_runs == []
    commands = [c.args[0][0] for c in run_butler.call_args_list if c.args and c.args[0]]
    assert "ingest-raws" not in commands


def test_crosstalk_ingests_when_the_scan_is_clean(tmp_path):
    raw_runs, run_butler, _ = _run_crosstalk_resolve(tmp_path, {})

    assert len(raw_runs) == 1
    commands = [c.args[0][0] for c in run_butler.call_args_list if c.args and c.args[0]]
    assert "ingest-raws" in commands


def test_crosstalk_reuse_of_existing_raws_does_not_scan(tmp_path):
    raw_runs, run_butler, scan = _run_crosstalk_resolve(
        tmp_path, {}, existing=["Nickel/raw/20230519/x"]
    )

    # Already-ingested raws are reused untouched; the pre-ingest scan is
    # pointless there (the collision would already be in the repo).
    assert raw_runs == ["Nickel/raw/20230519/x"]
    scan.assert_not_called()
    commands = [c.args[0][0] for c in run_butler.call_args_list if c.args and c.args[0]]
    assert "ingest-raws" not in commands
