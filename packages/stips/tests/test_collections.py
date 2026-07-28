import unittest

from stips.collections import (
    CollectionNames,
    template_deep,
    template_deep_glob,
    template_deep_run,
    template_ps1,
    template_ps1_glob,
)


class TestCollectionNames(unittest.TestCase):
    def test_nickel_prefix_parity(self):
        c = CollectionNames("20230519", "ts1", prefix="Nickel")
        self.assertEqual(c.raw_run, "Nickel/raw/20230519/ts1")
        self.assertEqual(c.calib_chain, "Nickel/calib/current")
        self.assertEqual(c.science_parent, "Nickel/runs/20230519/processCcd/ts1")
        self.assertEqual(c.diff_parent, "Nickel/runs/20230519/diff/ts1")

    def test_other_prefix(self):
        c = CollectionNames("20240101", "tsX", prefix="ctio0m9")
        self.assertEqual(c.raw_run, "ctio0m9/raw/20240101/tsX")
        self.assertEqual(c.calib_chain, "ctio0m9/calib/current")
        self.assertEqual(c.science_parent, "ctio0m9/runs/20240101/processCcd/tsX")

    def test_crosstalk_collections(self):
        c = CollectionNames("20230519", "ts1", prefix="CTIO1m")
        # RUN holding the freshly built/measured calib, before certification.
        self.assertEqual(c.crosstalk_gen, "CTIO1m/calib/crosstalk/gen/ts1")
        # CALIBRATION collection the calib is certified into (chained into curated).
        self.assertEqual(c.crosstalk_calib, "CTIO1m/calib/crosstalk")
        # The crosstalk calib is reachable via the existing curated chain.
        self.assertEqual(c.curated_chain, "CTIO1m/calib/curated")

    def test_prefix_is_required(self):
        # The transitional prefix="Nickel" default has been removed; prefix is now
        # a required keyword-only arg. Omitting it must raise TypeError.
        with self.assertRaises(TypeError):
            CollectionNames("20230519", "ts1")

    def test_forced_phot_collections(self):
        c = CollectionNames("20230519", "ts1", prefix="Nickel")
        # No-band variant (matches the legacy `visit`/`diffim` spelling).
        self.assertEqual(
            c.forced_phot_parent("visit"),
            "Nickel/runs/20230519/forcedPhotRaDec/ts1/visit",
        )
        self.assertEqual(
            c.forced_phot_run("visit"),
            "Nickel/runs/20230519/forcedPhotRaDec/ts1/visit/run",
        )
        # Per-band variant suffixes the leaf (`diffim_r`).
        self.assertEqual(
            c.forced_phot_parent("diffim", "r"),
            "Nickel/runs/20230519/forcedPhotRaDec/ts1/diffim_r",
        )
        self.assertEqual(
            c.forced_phot_run("diffim", "r"),
            "Nickel/runs/20230519/forcedPhotRaDec/ts1/diffim_r/run",
        )

    def test_differential_phot(self):
        c = CollectionNames("20230519", prefix="Nickel")
        self.assertEqual(c.differential_phot, "Nickel/runs/20230519/differentialPhot")

    def test_science_glob(self):
        self.assertEqual(
            CollectionNames.science_glob("Nickel"), "Nickel/runs/*/processCcd/*"
        )
        self.assertEqual(
            CollectionNames.science_glob("ctio1m"), "ctio1m/runs/*/processCcd/*"
        )

    def test_forced_phot_glob(self):
        # Default matches the whole forced-phot subtree for every night.
        self.assertEqual(
            CollectionNames.forced_phot_glob("Nickel"),
            "Nickel/runs/*/forcedPhotRaDec/*",
        )
        # Pinned night + a specific tail (as used by discovery / dry-run).
        self.assertEqual(
            CollectionNames.forced_phot_glob(
                "Nickel", night="20230519", tail="*/diffim*"
            ),
            "Nickel/runs/20230519/forcedPhotRaDec/*/diffim*",
        )
        self.assertEqual(
            CollectionNames.forced_phot_glob("Nickel", night="20230519", tail="*/run"),
            "Nickel/runs/20230519/forcedPhotRaDec/*/run",
        )
        # A literal "{night}" placeholder passes through untouched (clean.py uses
        # it and substitutes later).
        self.assertEqual(
            CollectionNames.forced_phot_glob("Nickel", night="{night}"),
            "Nickel/runs/{night}/forcedPhotRaDec/*",
        )


class TestTemplateCollections(unittest.TestCase):
    """Template collections are shared across instruments (no prefix)."""

    def test_template_ps1(self):
        self.assertEqual(template_ps1("r"), "templates/ps1/r")
        self.assertEqual(template_ps1_glob(), "templates/ps1/*")

    def test_template_deep(self):
        self.assertEqual(template_deep(0, "r"), "templates/deep/tract0/r")
        self.assertEqual(template_deep(12, "i"), "templates/deep/tract12/i")
        self.assertEqual(template_deep_glob(), "templates/deep/*/*")

    def test_template_deep_run(self):
        self.assertEqual(
            template_deep_run(3, "r", "ts9"), "templates/deep/tract3/r/ts9"
        )

    def test_template_deep_placeholder_tract(self):
        # Dry-run reporting uses a string placeholder instead of a wrong literal.
        self.assertEqual(template_deep("<TBD>", "r"), "templates/deep/tract<TBD>/r")


def test_template_external_collection_name():
    from stips.collections import template_external

    assert template_external("skymapper", "i") == "templates/skymapper/i"
    assert template_external("ps1", "r") == "templates/ps1/r"


def test_template_external_glob():
    from stips.collections import template_external_glob

    assert template_external_glob("skymapper") == "templates/skymapper/*"


def test_template_ps1_is_the_generic_builder_pinned_to_ps1():
    from stips.collections import template_external, template_ps1

    assert template_ps1("i") == template_external("ps1", "i")


if __name__ == "__main__":
    unittest.main()
