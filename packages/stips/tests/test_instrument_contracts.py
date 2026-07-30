"""Auto-discovered instrument-contract suite.

Every ``instruments/<name>/`` dir shipping a ``profile.py`` is discovered and
run through the shared contracts in ``stips.testing.instrument_contract``.
A new telescope gets this coverage by convention: drop in the instrument dir
plus ``tests/contract_data.py`` (see ``docs/instrument-contract.md``) -- no
test code to copy.

Optional assets skip-with-reason rather than fail:
  - no ``tests/contract_data.py`` -> the data-driven contracts skip
  - no ``fetch.py``               -> the fetch contract skips
  - no ``camera/``                -> the camera-assembly contract skips
  - no ``testdata/``              -> the testdata-layout contract skips
  - no LSST stack in the venv     -> stack-dependent contracts skip
"""

from __future__ import annotations

import pytest
from stips.testing import instrument_contract as ic

INSTRUMENTS = ic.discover_instruments()


def _ids(infos):
    return [i.name for i in infos]


def test_discovery_finds_reference_instruments():
    """The reference instruments must always be discovered (guards against a
    silent discovery regression that would skip-away the whole suite)."""
    names = {i.name for i in INSTRUMENTS}
    assert {"nickel", "ctio1m"} <= names, f"discovery returned only {names}"


@pytest.fixture(params=INSTRUMENTS, ids=_ids(INSTRUMENTS))
def instrument(request):
    return request.param


@pytest.fixture
def profile(instrument):
    return ic.load_profile(instrument)


@pytest.fixture
def contract_data(instrument):
    if not instrument.has_contract_data:
        pytest.skip(f"{instrument.name}: no tests/contract_data.py")
    return ic.load_contract_data(instrument)


# --------------------------------------------------------------------------- #
# Stack-free contracts
# --------------------------------------------------------------------------- #


def test_profile_contract(profile):
    ic.assert_profile_valid(profile)


def test_exposure_id_contract(profile, contract_data):
    ic.assert_exposure_id_scheme(profile, contract_data)


def test_translation_contract(profile, contract_data):
    ic.assert_translation_contract(profile, contract_data)


def test_observation_type_contract(profile, contract_data):
    if not getattr(contract_data, "OBSERVATION_TYPE_CASES", None):
        pytest.skip("no OBSERVATION_TYPE_CASES in contract_data")
    ic.assert_observation_type_cases(profile, contract_data)


def test_unknown_filter_contract(profile, contract_data):
    if getattr(contract_data, "UNKNOWN_FILTER", None) is None:
        pytest.skip("no UNKNOWN_FILTER in contract_data")
    ic.assert_unknown_filter_contract(profile, contract_data)


def test_fetch_contract(instrument, contract_data):
    if not instrument.has_fetch:
        pytest.skip(f"{instrument.name}: no fetch.py")
    fetch_module = ic.load_fetch(instrument)
    ic.assert_fetch_status_contract(fetch_module, contract_data)


def test_fetch_hook_registered(instrument, profile):
    if not instrument.has_fetch:
        pytest.skip(f"{instrument.name}: no fetch.py")
    assert (
        profile.fetch_data is not None
    ), f"{instrument.name}: ships fetch.py but profile.fetch_data is not wired"


# --------------------------------------------------------------------------- #
# Asset-layout contracts (stack-free)
# --------------------------------------------------------------------------- #


def test_camera_asset_contract(instrument, profile):
    """`profile.camera` must resolve: either a CameraSpec or an existing yaml."""
    cam = profile.camera
    if isinstance(cam, str):
        assert (
            instrument.path / cam
        ).is_file(), f"{instrument.name}: profile.camera={cam!r} not found under {instrument.path}"
    else:
        # CameraSpec-style object; minimal structural sanity.
        assert cam.nx > 0 and cam.ny > 0


def test_testdata_layout_contract(instrument):
    """When an instrument ships testdata/, it must contain at least one raw FITS.

    Future work (deferred): synthesize raw FITS from the camera spec so every
    instrument gets ingest coverage without curating real frames -- see
    docs/instrument-contract.md.
    """
    if not instrument.has_testdata:
        pytest.skip(f"{instrument.name}: no testdata/ (ingest contract not applicable)")
    fits = list(instrument.testdata_dir.rglob("*.fits")) + list(
        instrument.testdata_dir.rglob("*.fits.fz")
    )
    assert fits, f"{instrument.name}: testdata/ contains no FITS raws"


# --------------------------------------------------------------------------- #
# Stack-dependent contracts (skip cleanly in a plain venv; the coordinator runs
# them in-stack via scripts/with-stack.sh)
# --------------------------------------------------------------------------- #


def test_camera_assembly_contract(instrument, contract_data):
    """The synthesized Instrument assembles an afw Camera with the pinned
    detector/amplifier counts (EXPECTED_DETECTORS / EXPECTED_AMPS)."""
    # Target the real stack module needed, not the lsst.obs.stips namespace
    # (importable from the editable install even without a stack).
    pytest.importorskip("lsst.obs.base")
    if not instrument.has_camera:
        pytest.skip(f"{instrument.name}: no camera/")
    n_det = getattr(contract_data, "EXPECTED_DETECTORS", None)
    n_amp = getattr(contract_data, "EXPECTED_AMPS", None)
    if n_det is None or n_amp is None:
        pytest.skip("no EXPECTED_DETECTORS/EXPECTED_AMPS in contract_data")

    with ic.active_instrument_dir(instrument.path) as active:
        cam = active.Instrument().getCamera()
        dets = list(cam)
        assert len(dets) == n_det, f"{len(dets)} detectors, expected {n_det}"
        amps = list(dets[0])
        assert len(amps) == n_amp, f"{len(amps)} amplifiers, expected {n_amp}"


def test_translator_synthesis_contract(instrument, contract_data):
    """The generic StipsTranslator synthesized from the profile reproduces the
    pinned EXPECTED_TRANSLATION through the real translator surface (to_*)."""
    pytest.importorskip("lsst.obs.base")
    expected = contract_data.EXPECTED_TRANSLATION

    with ic.active_instrument_dir(instrument.path) as active:
        tr = active.Translator(dict(contract_data.SAMPLE_HEADER))
        assert tr.to_exposure_id() == expected["exposure_id"]
        assert tr.to_visit_id() == expected["visit_id"]
        assert tr.to_observation_type() == expected["observation_type"]
        assert tr.to_observation_id() == expected["observation_id"]


# --------------------------------------------------------------------------- #
# Reference-instrument pins
# --------------------------------------------------------------------------- #

#: Approximate science field of view, arcmin. Consumed by the external-template
#: adapters to warn when a survey cutout cannot cover the science footprint
#: (SkyMapper caps at 10.2', well under Y4KCam's ~20'). Pinned here because the
#: warning is unreachable — and its documented mitigation false — if a reference
#: profile stops declaring it.
REFERENCE_FOV_ARCMIN = {"nickel": 6.3, "ctio1m": 20.0}


def test_reference_profiles_declare_fov_arcmin(instrument, profile):
    if instrument.name not in REFERENCE_FOV_ARCMIN:
        pytest.skip(f"{instrument.name}: not a reference instrument")
    assert profile.fov_arcmin == pytest.approx(REFERENCE_FOV_ARCMIN[instrument.name])
