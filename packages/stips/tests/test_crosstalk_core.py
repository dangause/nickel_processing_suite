"""Stack-free tests for crosstalk command/argument construction."""

import json
import unittest

from stips.core import crosstalk as ct


class TestBuildWorkerArgs(unittest.TestCase):
    def test_invokes_worker_module_with_coeffs_json(self):
        coeffs = [[0.0, 1e-4], [1e-4, 0.0]]
        args = ct.build_worker_args(
            repo="/repo",
            instrument="CTIO1m",
            run="CTIO1m/calib/crosstalk/gen/ts1",
            coeffs=coeffs,
            units="adu",
        )
        self.assertEqual(args[:3], ["python", "-m", ct.WORKER_MODULE])
        self.assertIn("--repo", args)
        self.assertEqual(args[args.index("--repo") + 1], "/repo")
        self.assertEqual(args[args.index("--instrument") + 1], "CTIO1m")
        self.assertEqual(
            args[args.index("--run") + 1], "CTIO1m/calib/crosstalk/gen/ts1"
        )
        # Coeffs round-trip as JSON.
        self.assertEqual(json.loads(args[args.index("--coeffs-json") + 1]), coeffs)
        self.assertEqual(args[args.index("--units") + 1], "adu")


class TestCertifyArgs(unittest.TestCase):
    def test_certifies_crosstalk_with_wide_window(self):
        args = ct.certify_args("/repo", "gen/ts1", "CTIO1m/calib/crosstalk")
        self.assertEqual(args[0], "certify-calibrations")
        self.assertEqual(args[1], "/repo")
        self.assertEqual(args[2], "gen/ts1")
        self.assertEqual(args[3], "CTIO1m/calib/crosstalk")
        self.assertEqual(args[4], "crosstalk")
        self.assertEqual(args[args.index("--begin-date") + 1], ct.WIDE_BEGIN)
        self.assertEqual(args[args.index("--end-date") + 1], ct.WIDE_END)


class TestChainArgs(unittest.TestCase):
    def test_prepends_calib_into_chain(self):
        args = ct.chain_prepend_args(
            "/repo", "CTIO1m/calib/curated", "CTIO1m/calib/crosstalk"
        )
        self.assertEqual(
            args,
            [
                "collection-chain",
                "/repo",
                "CTIO1m/calib/curated",
                "CTIO1m/calib/crosstalk",
                "--mode",
                "prepend",
            ],
        )


class TestMeasureArgs(unittest.TestCase):
    def test_qgraph_args_include_pipeline_inputs_and_isr(self):
        args = ct.measure_qgraph_args(
            repo="/repo",
            pipeline="/cp/cpCrosstalk.yaml",
            inputs="CTIO1m/calib/current,CTIO1m/raw/20070321/ts1",
            output="CTIO1m/calib/crosstalk/gen",
            output_run="CTIO1m/calib/crosstalk/gen/ts1",
            qgraph_path="/q/ct.qg",
            where="instrument='CTIO1m'",
            isr_args=["--config", "cpCrosstalkIsr:doDefect=False"],
            datastore_records=False,
        )
        self.assertEqual(args[0], "qgraph")
        self.assertEqual(args[args.index("-p") + 1], "/cp/cpCrosstalk.yaml")
        self.assertEqual(
            args[args.index("-i") + 1], "CTIO1m/calib/current,CTIO1m/raw/20070321/ts1"
        )
        self.assertEqual(
            args[args.index("--output-run") + 1], "CTIO1m/calib/crosstalk/gen/ts1"
        )
        self.assertIn("cpCrosstalkIsr:doDefect=False", args)
        self.assertNotIn("--qgraph-datastore-records", args)

    def test_qgraph_args_add_datastore_records_when_needed(self):
        args = ct.measure_qgraph_args(
            repo="/repo",
            pipeline="/p.yaml",
            inputs="a,b",
            output="o",
            output_run="o/ts",
            qgraph_path="/q.qg",
            where="x",
            isr_args=[],
            datastore_records=True,
        )
        self.assertIn("--qgraph-datastore-records", args)

    def test_run_args(self):
        args = ct.measure_run_args("/repo", "/q/ct.qg", jobs=4)
        self.assertEqual(args[0], "run")
        self.assertEqual(args[args.index("-g") + 1], "/q/ct.qg")
        self.assertEqual(args[args.index("-j") + 1], "4")
        self.assertIn("--register-dataset-types", args)


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


class _FakeProfile:
    name = "CTIO1m"
    collection_prefix = "CTIO1m"
    instrument_class = "lsst.obs.stips.active.Instrument"


class _FakeConfig:
    # No profile -> the pre-ingest aliasing scan has nothing to audit and
    # reports the night clean (see find_aliasing_exposure_ids).
    profile = None

    def __init__(self, repo):
        self.repo = repo


class TestResolveRawRuns(unittest.TestCase):
    """The measurement path must reuse an existing raw collection (re-ingesting
    skips already-registered exposures, leaving an empty RUN)."""

    def test_reuses_existing_raw_collection_without_ingesting(self):
        import stips.core.crosstalk as mod

        calls = []
        orig_lc = mod.butler_query.list_collections
        orig_butler = mod.run_butler
        orig_get_raw = mod.get_raw_dir
        try:
            mod.butler_query.list_collections = (
                lambda config, pattern, *, prefix=None: ["CTIO1m/raw/20070321/ts1"]
            )
            mod.run_butler = lambda args, config, **k: calls.append(args[0])
            mod.get_raw_dir = lambda config, night: (_ for _ in ()).throw(
                AssertionError("should not touch raw dir when collection exists")
            )
            runs = mod._resolve_raw_runs(
                ["20070321"], _FakeConfig("/repo"), _FakeProfile()
            )
        finally:
            mod.butler_query.list_collections = orig_lc
            mod.run_butler = orig_butler
            mod.get_raw_dir = orig_get_raw

        self.assertEqual(runs, ["CTIO1m/raw/20070321/ts1"])
        self.assertNotIn("ingest-raws", calls)  # no ingest when raws already present

    def test_ingests_when_no_collection_exists(self):
        import stips.core.crosstalk as mod

        calls = []
        orig_lc = mod.butler_query.list_collections
        orig_butler = mod.run_butler
        orig_get_raw = mod.get_raw_dir

        class _ExistingDir:
            def exists(self):
                return True

        try:
            mod.butler_query.list_collections = (
                lambda config, pattern, *, prefix=None: []  # none found
            )
            mod.run_butler = lambda args, config, **k: calls.append(args[0])
            mod.get_raw_dir = lambda config, night: _ExistingDir()
            runs = mod._resolve_raw_runs(
                ["20070321"], _FakeConfig("/repo"), _FakeProfile()
            )
        finally:
            mod.butler_query.list_collections = orig_lc
            mod.run_butler = orig_butler
            mod.get_raw_dir = orig_get_raw

        self.assertEqual(len(runs), 1)
        self.assertIn("ingest-raws", calls)


class TestCrosstalkIdempotency(unittest.TestCase):
    """The crosstalk calib is a static, repo-level product certified into a shared
    CALIBRATION collection. Re-certifying it (e.g. once per night in a multi-night
    run) raises ConflictingDefinitionError after the first night, which broke
    multi-night CTIO calibs. build_and_certify_crosstalk must be idempotent: if the
    calib collection already exists, skip the rebuild + re-certify."""

    class _CT:
        coeffs = [[0.0, 1e-4], [1e-4, 0.0]]
        units = "adu"

    class _Prof:
        name = "CTIO1m"
        collection_prefix = "CTIO1m"
        crosstalk = None  # set per-instance

    class _Cfg:
        repo = "/repo"

        def __init__(self, prof):
            self._prof = prof

        def require_profile(self):
            return self._prof

    def _run(self, exists):
        import stips.core.crosstalk as mod

        prof = self._Prof()
        prof.crosstalk = self._CT()
        calls = []
        orig_lc, orig_b, orig_stack = (
            mod.butler_query.list_collections,
            mod.run_butler,
            mod.run_with_stack,
        )
        try:

            class _Worker:
                returncode = 0
                stdout = ""
                stderr = ""

            mod.butler_query.list_collections = lambda config, pattern, **k: (
                ["CTIO1m/calib/crosstalk"] if exists else []
            )
            mod.run_butler = lambda args, config, **k: calls.append(args[0])
            mod.run_with_stack = lambda *a, **k: (
                calls.append("run_with_stack") or _Worker()
            )
            result = mod.build_and_certify_crosstalk("20070321", self._Cfg(prof))
        finally:
            mod.butler_query.list_collections = orig_lc
            mod.run_butler = orig_b
            mod.run_with_stack = orig_stack
        return result, calls

    def test_skips_rebuild_and_recertify_when_calib_exists(self):
        result, calls = self._run(exists=True)
        self.assertTrue(result.success)
        self.assertEqual(result.calib_collection, "CTIO1m/calib/crosstalk")
        self.assertNotIn("run_with_stack", calls)  # did not rebuild
        self.assertNotIn("certify-calibrations", calls)  # did not re-certify

    def test_builds_and_certifies_when_calib_absent(self):
        # No existing collection -> the worker build runs (and would certify).
        result, calls = self._run(exists=False)
        self.assertIn("run_with_stack", calls)


if __name__ == "__main__":
    unittest.main()
