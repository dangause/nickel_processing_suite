"""Tests for RunConfig variable star extensions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def sn_yaml(tmp_path):
    cfg = {
        "object": "2023ixf",
        "ra": 210.91,
        "dec": 54.32,
        "bands": ["r", "i"],
        "science": {"nights": [20230519]},
        "options": {"jobs": 4},
    }
    path = tmp_path / "sn.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


@pytest.fixture
def variable_yaml(tmp_path):
    cfg = {
        "object": "V0678-Oph",
        "ra": 257.123,
        "dec": -18.456,
        "bands": ["b", "v", "r", "i"],
        "template": {"type": "coadd", "nights": [20230601, 20230615]},
        "science": {"nights": [20230701]},
        "options": {
            "pipeline_type": "variable",
            "period_search": True,
            "period_min": 0.5,
            "period_max": 50.0,
            "period_samples": 8000,
            "forced_phot_image_type": "both",
        },
    }
    path = tmp_path / "variable.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


@pytest.fixture
def variable_defaults_yaml(tmp_path):
    cfg = {
        "object": "RR-Lyr",
        "ra": 286.0,
        "dec": 42.0,
        "bands": ["r"],
        "science": {"nights": [20230801]},
        "options": {"pipeline_type": "variable"},
    }
    path = tmp_path / "var_defaults.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


class TestRunConfigNewFields:
    def test_sn_config_has_defaults(self, sn_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(sn_yaml)
        assert cfg.pipeline_type == "supernova"
        assert cfg.period_search is False
        assert cfg.period_min == 0.1
        assert cfg.period_max == 100.0
        assert cfg.period_samples == 10_000
        assert cfg.forced_phot_image_type == "diffim"

    def test_variable_config_parses_all_fields(self, variable_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(variable_yaml)
        assert cfg.pipeline_type == "variable"
        assert cfg.period_search is True
        assert cfg.period_min == 0.5
        assert cfg.period_max == 50.0
        assert cfg.period_samples == 8000
        assert cfg.forced_phot_image_type == "both"

    def test_variable_type_defaults_forced_phot_to_both(self, variable_defaults_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(variable_defaults_yaml)
        assert cfg.pipeline_type == "variable"
        assert cfg.forced_phot_image_type == "both"

    def test_explicit_forced_phot_overrides_variable_default(self, tmp_path):
        from stips.core.run import RunConfig

        cfg = {
            "object": "test",
            "ra": 100.0,
            "dec": 10.0,
            "bands": ["r"],
            "science": {"nights": [20230101]},
            "options": {
                "pipeline_type": "variable",
                "forced_phot_image_type": "diffim",
            },
        }
        path = tmp_path / "override.yaml"
        with open(path, "w") as f:
            yaml.dump(cfg, f)
        rc = RunConfig.from_yaml(path)
        assert rc.forced_phot_image_type == "diffim"


@pytest.fixture
def transit_yaml(tmp_path):
    """Transit YAML config with BLS search enabled."""
    cfg = {
        "object": "HAT-P-32",
        "ra": 30.456,
        "dec": 46.789,
        "bands": ["r", "i"],
        "template": {"type": "coadd", "nights": [20230601, 20230615]},
        "science": {"nights": [20230701]},
        "options": {
            "pipeline_type": "transit",
            "transit_search": True,
            "period_min": 1.0,
            "period_max": 5.0,
            "transit_duration_min": 1.0,
            "transit_duration_max": 4.0,
        },
    }
    path = tmp_path / "transit.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


@pytest.fixture
def transit_defaults_yaml(tmp_path):
    """Transit config relying on pipeline_type defaults."""
    cfg = {
        "object": "WASP-12",
        "ra": 97.637,
        "dec": 29.672,
        "bands": ["r"],
        "science": {"nights": [20230901]},
        "options": {"pipeline_type": "transit"},
    }
    path = tmp_path / "transit_defaults.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


class TestRunConfigTransitFields:
    """Test transit extension fields in RunConfig."""

    def test_transit_config_parses_all_fields(self, transit_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(transit_yaml)
        assert cfg.pipeline_type == "transit"
        assert cfg.transit_search is True
        assert cfg.search_method == "bls"
        assert cfg.period_min == 1.0
        assert cfg.period_max == 5.0
        assert cfg.transit_duration_min == 1.0
        assert cfg.transit_duration_max == 4.0

    def test_transit_type_defaults_forced_phot_to_visit(self, transit_defaults_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(transit_defaults_yaml)
        assert cfg.pipeline_type == "transit"
        assert cfg.forced_phot_image_type == "visit"

    def test_transit_type_defaults_search_method_to_bls(self, transit_defaults_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(transit_defaults_yaml)
        assert cfg.search_method == "bls"
        assert cfg.transit_search is True

    def test_transit_type_defaults_duration_range(self, transit_defaults_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(transit_defaults_yaml)
        assert cfg.transit_duration_min == 0.5
        assert cfg.transit_duration_max == 6.0

    def test_explicit_search_method_overrides_transit_default(self, tmp_path):
        from stips.core.run import RunConfig

        cfg = {
            "object": "test",
            "ra": 100.0,
            "dec": 10.0,
            "bands": ["r"],
            "science": {"nights": [20230101]},
            "options": {
                "pipeline_type": "transit",
                "search_method": "both",
            },
        }
        path = tmp_path / "override.yaml"
        with open(path, "w") as f:
            yaml.dump(cfg, f)
        rc = RunConfig.from_yaml(path)
        assert rc.search_method == "both"

    def test_sn_config_has_transit_defaults(self, sn_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(sn_yaml)
        assert cfg.search_method == "lomb_scargle"
        assert cfg.transit_search is False
        assert cfg.transit_duration_min == 0.5
        assert cfg.transit_duration_max == 6.0


@pytest.fixture
def bps_yaml(tmp_path):
    cfg = {
        "object": "2023ixf",
        "ra": 210.91,
        "dec": 54.32,
        "bands": ["r", "i"],
        "science": {"nights": [20230519]},
        "options": {
            "execution": "bps",
            "site": "slurm",
            "concurrent_nights": 4,
            "bps_poll_interval": 10.0,
            "bps_timeout": 3600,
        },
    }
    path = tmp_path / "bps.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


class TestRunConfigExecutionFields:
    def test_parses_bps_execution_fields(self, bps_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(bps_yaml)
        assert cfg.execution == "bps"
        assert cfg.site == "slurm"
        assert cfg.concurrent_nights == 4
        assert cfg.bps_poll_interval == 10.0
        assert cfg.bps_timeout == 3600

    def test_default_execution_is_local(self, sn_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(sn_yaml)
        assert cfg.execution == "local"
        assert cfg.site == "local"
        assert cfg.concurrent_nights == 0
        assert cfg.bps_poll_interval == 5.0
        assert cfg.bps_timeout == 7200.0

    def test_concurrent_nights_without_bps(self, tmp_path):
        from stips.core.run import RunConfig

        cfg_data = {
            "object": "test",
            "ra": 100.0,
            "dec": 10.0,
            "bands": ["r"],
            "science": {"nights": [20230101]},
            "options": {"concurrent_nights": 3},
        }
        path = tmp_path / "concurrent.yaml"
        with open(path, "w") as f:
            yaml.dump(cfg_data, f)
        cfg = RunConfig.from_yaml(path)
        assert cfg.execution == "local"
        assert cfg.concurrent_nights == 3


class TestExecutorFactory:
    def test_local_config_creates_local_executor(self, sn_yaml):
        from stips.core.executor import LocalExecutor
        from stips.core.run import RunConfig, _create_executor

        cfg = RunConfig.from_yaml(sn_yaml)
        executor = _create_executor(cfg)
        assert isinstance(executor, LocalExecutor)

    def test_bps_config_creates_bps_executor(self, bps_yaml):
        from stips.core.executor import BPSExecutor
        from stips.core.run import RunConfig, _create_executor

        cfg = RunConfig.from_yaml(bps_yaml)
        executor = _create_executor(cfg)
        assert isinstance(executor, BPSExecutor)
        assert executor.site == "slurm"
        assert executor.poll_interval == 10.0
        assert executor.timeout == 3600


@pytest.fixture
def coadd_select_yaml(tmp_path):
    cfg = {
        "object": "E2",
        "ra": 100.0,
        "dec": -45.0,
        "bands": ["r"],
        "template": {"type": "coadd", "nights": [20111110, 20111111]},
        "science": {"nights": [20111113]},
        "configs": {
            "coadd": {
                "make_direct_warp": "coadds/makeDirectWarp.py",
                "select_template_coadd_visits": "coadds/selectTemplateCoaddVisits.py",
                "select_deep_coadd_visits": "coadds/selectDeepCoaddVisits.py",
            }
        },
    }
    path = tmp_path / "coadd_select.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


class TestRunConfigCoaddSelectConfigs:
    def test_parses_select_visit_config_paths(self, coadd_select_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(coadd_select_yaml)
        assert cfg.coadd_configs.make_direct_warp == "coadds/makeDirectWarp.py"
        assert (
            cfg.coadd_configs.select_template_coadd_visits
            == "coadds/selectTemplateCoaddVisits.py"
        )
        assert (
            cfg.coadd_configs.select_deep_coadd_visits
            == "coadds/selectDeepCoaddVisits.py"
        )

    def test_select_visit_configs_default_to_none(self, sn_yaml):
        from stips.core.run import RunConfig

        cfg = RunConfig.from_yaml(sn_yaml)
        assert cfg.coadd_configs.select_template_coadd_visits is None
        assert cfg.coadd_configs.select_deep_coadd_visits is None


# ---------------------------------------------------------------------------
# template.type validation (fail fast, not silently)
# ---------------------------------------------------------------------------


def _write_cfg(tmp_path, template_type):
    cfg = {
        "object": "NGC2298",
        "ra": 102.246542,
        "dec": -36.005333,
        "bands": ["i"],
        "template": {"type": template_type},
        "science": {"nights": [20061216]},
    }
    path = tmp_path / f"{template_type}.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


def test_unknown_template_type_is_rejected_at_parse_time(tmp_path):
    """A typo used to no-op: nothing was ingested, then every band failed DIA
    with "no template available" -- after calibs and science had already run."""
    from stips.core.run import RunConfig

    with pytest.raises(ValueError) as exc:
        RunConfig.from_yaml(_write_cfg(tmp_path, "skymappper"))
    message = str(exc.value)
    assert "skymappper" in message
    for valid in ("coadd", "auto", "ps1", "skymapper"):
        assert valid in message


@pytest.mark.parametrize("template_type", ["ps1", "skymapper", "coadd", "auto"])
def test_known_template_types_are_accepted(tmp_path, template_type):
    from stips.core.run import RunConfig

    assert RunConfig.from_yaml(_write_cfg(tmp_path, template_type)).template_type == (
        template_type
    )


def test_template_type_validation_covers_direct_construction():
    """Not only from_yaml -- a RunConfig built in code gets the same guard."""
    from stips.core.run import RunConfig

    with pytest.raises(ValueError, match="nosuchsurvey"):
        RunConfig(
            object_name="x",
            ra=1.0,
            dec=2.0,
            bands=["i"],
            template_type="nosuchsurvey",
        )


def test_a_newly_registered_source_becomes_a_valid_template_type(tmp_path, monkeypatch):
    """The valid set is the SOURCES registry, not a hardcoded list."""
    from stips.core.run import RunConfig
    from stips.pipeline_tools.external_template import sources as src_mod

    monkeypatch.setitem(src_mod.SOURCES, "decals", object())
    assert RunConfig.from_yaml(_write_cfg(tmp_path, "decals")).template_type == "decals"
