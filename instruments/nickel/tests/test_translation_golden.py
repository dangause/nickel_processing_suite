# tests/test_translation_golden.py
"""Golden translation-parity gate for the NickelTranslator.

These literals were captured from the legacy ``NickelTranslator`` before the
STIPS refactor (commit 7abfe7b). After the reimplementation, this same test now
runs against the ``StipsTranslator``-based ``NickelTranslator`` and must continue
to pass -- so it serves as the translation-parity gate (legacy literals
reproduced by the reimplementation).

Do NOT edit the literals; if a value changes, the reimplementation diverged from
the legacy and must be fixed, not the test.
"""

import importlib
import os
import unittest
from pathlib import Path

import astropy.units as u
import pytest
from astropy.time import Time

# The translator is synthesized from lsst.obs.stips.active, which requires the
# LSST stack; skip cleanly in a plain venv.
pytest.importorskip("lsst.daf.butler")

# instruments/nickel/tests/test_translation_golden.py -> parents[1] == instruments/nickel
_INSTRUMENT_DIR = str(Path(__file__).resolve().parents[1])


def _load_translator():
    """Synthesize the Nickel-bound Translator from the generic machinery.

    Sets ``INSTRUMENT_DIR`` to the reference Nickel dir and (re)loads
    ``lsst.obs.stips.active`` so the golden literals below run against the
    profile loaded from ``instruments/nickel/profile.py``.
    """
    os.environ["INSTRUMENT_DIR"] = _INSTRUMENT_DIR
    import lsst.obs.stips.active as active

    return importlib.reload(active).Translator


NickelTranslator = _load_translator()

# Science header: copied VERBATIM from test_translator.py's setUp, plus
# RA/DEC keywords so the stuck-DEC tracking path has telescope coordinates.
SCIENCE_HEADER = {
    # IDs / instrument
    "INSTRUME": "Nickel Direct Camera",
    "OBSNUM": 1032,
    # Times
    "EXPTIME": 120.0,
    "DATE-BEG": "2024-06-25T05:15:49.25",
    "DATE-END": "2024-06-25T05:17:49.25",
    # WCS center (primary; degrees)
    "CRVAL1": 179.1170349121,
    "CRVAL2": 55.1252822876,
    "CRPIX1": 512.0,
    "CRPIX2": 512.0,
    "CUNIT1": "deg",
    "CUNIT2": "deg",
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
    "RADECSYS": "FK5",
    "EQUINOX": 2000.0,
    # Misc used by trivial map and sanity checks
    "OBJECT": "NGC_3982",
    "AIRMASS": 1.281367778778,
    "TEMPDET": -109.7,
    "FILTNAM": "B",
    "TELESCOP": "Nickel 1m",
    # Telescope control system coordinates (stuck-DEC tracking path)
    "RA": "11:56:28.09",
    "DEC": "+55:07:31.0",
}

# Calibration header: same as science but a dome-flat calibration frame.
CALIB_HEADER = dict(
    SCIENCE_HEADER,
    OBSTYPE="flat",
    OBJECT="dome flat",
    FILTNAM="V",
)


class TestTranslationGoldenScience(unittest.TestCase):
    """Pin current translator outputs for the science header."""

    def setUp(self):
        self.tr = NickelTranslator(dict(SCIENCE_HEADER))

    def test_instrument(self):
        self.assertEqual(self.tr.to_instrument(), "Nickel")

    def test_physical_filter(self):
        self.assertEqual(self.tr.to_physical_filter(), "B")

    def test_observation_type(self):
        self.assertEqual(self.tr.to_observation_type(), "science")

    def test_observation_reason(self):
        self.assertEqual(self.tr.to_observation_reason(), "science")

    def test_exposure_id(self):
        self.assertEqual(self.tr.to_exposure_id(), 89421032)

    def test_visit_id(self):
        self.assertEqual(self.tr.to_visit_id(), 89421032)

    def test_telescope(self):
        self.assertEqual(self.tr.to_telescope(), "Nickel 1m")

    def test_boresight_airmass(self):
        self.assertAlmostEqual(self.tr.to_boresight_airmass(), 1.281367778778, places=9)

    def test_temperature(self):
        # -109.7 C + 273.15 = 163.45 K
        self.assertAlmostEqual(self.tr.to_temperature().to_value(u.K), 163.45, places=9)

    def test_datetime_begin(self):
        t0 = self.tr.to_datetime_begin()
        self.assertIsInstance(t0, Time)
        self.assertAlmostEqual(t0.mjd, 60486.21932002315, places=9)

    def test_datetime_end(self):
        t1 = self.tr.to_datetime_end()
        self.assertIsInstance(t1, Time)
        self.assertAlmostEqual(t1.mjd, 60486.220708912035, places=9)

    def test_tracking_radec(self):
        coord = self.tr.to_tracking_radec()
        self.assertAlmostEqual(coord.ra.to_value(u.deg), 179.1170349121, places=6)
        self.assertAlmostEqual(coord.dec.to_value(u.deg), 55.1252822876, places=6)

    def test_location(self):
        loc = self.tr.to_location()
        self.assertAlmostEqual(loc.lat.deg, 37.34333333333334, places=4)
        self.assertAlmostEqual(loc.lon.deg, -121.63666666666666, places=4)
        self.assertAlmostEqual(loc.height.to_value(u.m), 1290.0, places=1)

    def test_boresight_rotation_angle(self):
        self.assertAlmostEqual(
            self.tr.to_boresight_rotation_angle().to_value(u.deg), 0.0, places=9
        )

    def test_day_obs(self):
        self.assertEqual(self.tr.to_observing_day(), 20240625)

    def test_observation_id(self):
        self.assertEqual(self.tr.to_observation_id(), "20240625_1032")


class TestTranslationLargeObsnum(unittest.TestCase):
    """Real 2023ixf frame: OBSNUM is an observatory-wide running counter.

    NEW coverage, not a change to the legacy literals above. ``d228001.fits``
    from night 20230519 (UT day 20230520) is verbatim from
    ``/Users/dangause/Developer/lick/data``; before the OBSNUM fold this header
    could not be translated at all (``seqnum 228001 is out of range``), which is
    why no golden value for it existed.
    """

    def setUp(self):
        self.tr = NickelTranslator(
            dict(
                SCIENCE_HEADER,
                OBSNUM=228001,
                EXPTIME=0,
                OBJECT="bias",
                **{
                    "DATE-BEG": "2023-05-20T01:30:36.40",
                    "DATE-END": "2023-05-20T01:30:36.43",
                },
            )
        )

    def test_day_obs(self):
        self.assertEqual(self.tr.to_observing_day(), 20230520)

    def test_exposure_id_folds_obsnum(self):
        # days_since_2000(2023-05-20) == 8540; 228001 % 10000 == 8001
        self.assertEqual(self.tr.to_exposure_id(), 85408001)

    def test_visit_id_tracks_exposure_id(self):
        self.assertEqual(self.tr.to_visit_id(), 85408001)

    def test_observation_id_keeps_full_obsnum(self):
        # Traceable back to the source file d228001.fits — see the profile hook.
        self.assertEqual(self.tr.to_observation_id(), "20230520_228001")


class TestTranslationGoldenCalib(unittest.TestCase):
    """Pin current translator outputs for the calibration (flat) header."""

    def setUp(self):
        self.tr = NickelTranslator(dict(CALIB_HEADER))

    def test_instrument(self):
        self.assertEqual(self.tr.to_instrument(), "Nickel")

    def test_physical_filter(self):
        self.assertEqual(self.tr.to_physical_filter(), "V")

    def test_observation_type(self):
        self.assertEqual(self.tr.to_observation_type(), "flat")

    def test_observation_reason(self):
        self.assertEqual(self.tr.to_observation_reason(), "calibration")

    def test_exposure_id(self):
        self.assertEqual(self.tr.to_exposure_id(), 89421032)

    def test_visit_id(self):
        self.assertEqual(self.tr.to_visit_id(), 89421032)

    def test_telescope(self):
        self.assertEqual(self.tr.to_telescope(), "Nickel 1m")

    def test_day_obs(self):
        self.assertEqual(self.tr.to_observing_day(), 20240625)

    def test_observation_id(self):
        self.assertEqual(self.tr.to_observation_id(), "20240625_1032")


if __name__ == "__main__":
    unittest.main()
