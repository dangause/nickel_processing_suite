"""Nickel OBSNUM -> exposure_id packing, including the large-OBSNUM path.

Nickel's ``OBSNUM`` is an observatory-wide RUNNING counter, not a per-night
sequence: it crossed 10,000 between 2018 and 2020 and is ~228,000 for the
2023ixf campaign. ``pack_exposure_id`` only accepts a 4-digit sequence number,
so the profile must fold OBSNUM into that window (``OBSNUM % 10000``).

These tests run stack-free: the profile is loaded by path via the same helper
the shared instrument-contract suite uses, and every hook exercised here is
pure Python + astropy.

The small-OBSNUM cases duplicate the golden literals pinned by
``test_translation_golden.py`` / ``contract_data.py`` on purpose -- they are the
backward-compatibility proof that folding is the identity below 10,000.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from stips.testing import instrument_contract as ic

# instruments/nickel/tests/test_exposure_id_obsnum.py -> parents[1] == instruments/nickel
_INSTRUMENT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def hooks():
    info = ic.InstrumentDirInfo(name="nickel", path=_INSTRUMENT_DIR)
    return ic.load_profile(info).hooks


def _header(obsnum, date_beg, date_end):
    return {
        "INSTRUME": "Nickel Direct Camera",
        "OBSNUM": obsnum,
        "EXPTIME": 120.0,
        "DATE-BEG": date_beg,
        "DATE-END": date_end,
        "OBJECT": "SN2023ixf",
        "FILTNAM": "R",
    }


# --------------------------------------------------------------------------- #
# Backward compatibility: OBSNUM < 10000 is untouched by the fold.
# --------------------------------------------------------------------------- #


def test_small_obsnum_matches_golden_exposure_id(hooks):
    """The golden frame (OBSNUM 1032, 2024-06-25) keeps its pinned id."""
    h = _header(1032, "2024-06-25T05:15:49.25", "2024-06-25T05:17:49.25")
    assert hooks["exposure_id"](h) == 89421032
    assert hooks["visit_id"](h) == 89421032


def test_small_obsnum_matches_golden_observation_id(hooks):
    h = _header(1032, "2024-06-25T05:15:49.25", "2024-06-25T05:17:49.25")
    assert hooks["observation_id"](h) == "20240625_1032"


# --------------------------------------------------------------------------- #
# The bug: real campaign data with OBSNUM >= 10000.
# --------------------------------------------------------------------------- #


def test_2023ixf_first_frame_ingests(hooks):
    """d228001.fits from night 20230519 (UT day 20230520), verbatim header values.

    Before the fix this raised ValueError from ``pack_exposure_id``:
    ``seqnum 228001 is out of range [0, 10000)``.
    """
    h = _header(228001, "2023-05-20T01:30:36.40", "2023-05-20T01:30:36.43")
    # days_since_2000(2023-05-20) == 8540; 228001 % 10000 == 8001
    assert hooks["exposure_id"](h) == 85408001


def test_2020wnt_frame_ingests(hooks):
    """Night 20201207 of the 2020wnt campaign runs OBSNUM 12001-12154."""
    h = _header(12001, "2020-12-08T03:00:00.00", "2020-12-08T03:02:00.00")
    assert hooks["exposure_id"](h) == 76472001


def test_large_obsnum_fits_31_bits(hooks):
    h = _header(273001, "2023-12-12T04:00:00.00", "2023-12-12T04:02:00.00")
    assert 0 < hooks["exposure_id"](h) < 2**31


def test_large_obsnum_is_monotonic_within_a_night(hooks):
    """Consecutive OBSNUMs within a night stay consecutive after the fold."""
    a = _header(228001, "2023-05-20T01:30:36.40", "2023-05-20T01:30:36.43")
    b = _header(228002, "2023-05-20T01:31:36.40", "2023-05-20T01:31:36.43")
    assert hooks["exposure_id"](b) == hooks["exposure_id"](a) + 1


def test_night_spanning_a_10000_boundary_stays_unique(hooks):
    """Night 20230521 runs OBSNUM 228100-229066, i.e. across a fold boundary.

    The fold is injective over any 10,000-wide window, so a night that merely
    crosses a multiple of 10,000 is fine; only a >=10,000 span would alias.
    """
    ids = {
        hooks["exposure_id"](
            _header(n, "2023-05-22T05:00:00.00", "2023-05-22T05:02:00.00")
        )
        for n in (228100, 229999, 230000, 229066)
    }
    assert len(ids) == 4


# --------------------------------------------------------------------------- #
# observation_id keeps the FULL OBSNUM (traceability + alias detectability).
# --------------------------------------------------------------------------- #


def test_observation_id_keeps_full_obsnum(hooks):
    """observation_id is a string, not 31-bit-limited: keep the un-folded OBSNUM
    so it still names the source file ``d228001.fits``."""
    h = _header(228001, "2023-05-20T01:30:36.40", "2023-05-20T01:30:36.43")
    assert hooks["observation_id"](h) == "20230520_228001"


def test_observation_id_distinguishes_folded_collisions(hooks):
    """Two frames that WOULD alias on exposure_id still differ in observation_id.

    This is what makes an alias detectable downstream rather than silent: Butler's
    ``exposure`` dimension has a unique alternate key on ``obs_id``, so two records
    sharing ``id`` but differing in ``obs_id`` are a conflicting definition.
    """
    a = _header(228001, "2023-05-20T01:30:36.40", "2023-05-20T01:30:36.43")
    b = _header(238001, "2023-05-20T02:30:36.40", "2023-05-20T02:30:36.43")
    assert hooks["exposure_id"](a) == hooks["exposure_id"](b)
    assert hooks["observation_id"](a) != hooks["observation_id"](b)
