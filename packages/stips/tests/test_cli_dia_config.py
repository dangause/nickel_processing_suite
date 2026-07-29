"""``stips dia`` CLI: subtract/detect config resolution.

Regression coverage for a real gap found during SkyMapper DIA validation:
``stips.core.dia.run()`` accepts ``subtract_config_file``/``detect_config_file``
and the YAML-driven ``stips run`` path wires them from
``configs.dia.subtract_images``/``configs.dia.detect_and_measure`` — but the
``stips dia`` CLI subcommand exposed neither flag and passed neither, so a user
following the documented ``stips dia <night> --template ...`` workflow silently
got the instrument-dir default DIA config instead of the one their YAML
specified. Concretely: a SkyMapper run made this way applied
``mode='convolveTemplate'`` (the ctio1m default) instead of the intended
``mode="auto"`` from ``subtractImages_skymapper.py``, forcing a deconvolution
that drove the spatial condition number to 2.4e10.

These tests pin: (a) an explicit ``--subtract-config``/``--detect-config``
reaches ``dia.run()``, resolved via ``config.resolve_config()``; (b) with no
flag, a YAML ``configs.dia.subtract_images``/``detect_and_measure`` reaches
``dia.run()``; (c) an explicit flag beats the YAML.
"""

from click.testing import CliRunner
from stips import cli as cli_module
from stips.core import dia as dia_module
from stips.core.dia import DIAResult


def _write_config(tmp_path, *, with_dia_configs=False):
    instrument_dir = tmp_path / "instr"
    lines = [
        "env:",
        f"  REPO: {tmp_path / 'repo'}",
        f"  STACK_DIR: {tmp_path / 'stack'}",
        f"  INSTRUMENT_DIR: {instrument_dir}",
        f"  RAW_PARENT_DIR: {tmp_path / 'raw'}",
    ]
    if with_dia_configs:
        lines += [
            "configs:",
            "  dia:",
            "    subtract_images: dia/subtractImages_skymapper.py",
            "    detect_and_measure: dia/detectAndMeasure_skymapper.py",
        ]
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("\n".join(lines) + "\n")
    return cfg_file


def _fake_dia_run(capture):
    def _run(night, config, **kwargs):
        capture["config"] = config
        capture["kwargs"] = kwargs
        return DIAResult(
            success=True,
            night=night,
            diff_run="Nickel/runs/20240625/diff/ts/run",
            template_collection=kwargs.get("template"),
            diff_image_count=1,
            dia_source_count=1,
        )

    return _run


def test_explicit_subtract_config_reaches_dia_run(tmp_path, monkeypatch):
    cfg_file = _write_config(tmp_path)
    capture = {}
    monkeypatch.setattr(dia_module, "run", _fake_dia_run(capture))

    runner = CliRunner()
    res = runner.invoke(
        cli_module.cli,
        [
            "-c",
            str(cfg_file),
            "dia",
            "20240625",
            "--template",
            "templates/deep/r",
            "--subtract-config",
            "dia/custom_subtract.py",
        ],
    )

    assert res.exit_code == 0, res.output
    expected = capture["config"].resolve_config("dia/custom_subtract.py")
    assert capture["kwargs"]["subtract_config_file"] == expected
    assert str(expected) in res.output


def test_yaml_dia_configs_used_when_no_flag(tmp_path, monkeypatch):
    cfg_file = _write_config(tmp_path, with_dia_configs=True)
    capture = {}
    monkeypatch.setattr(dia_module, "run", _fake_dia_run(capture))

    runner = CliRunner()
    res = runner.invoke(
        cli_module.cli,
        ["-c", str(cfg_file), "dia", "20240625", "--template", "templates/deep/r"],
    )

    assert res.exit_code == 0, res.output
    expected_subtract = capture["config"].resolve_config(
        "dia/subtractImages_skymapper.py"
    )
    expected_detect = capture["config"].resolve_config(
        "dia/detectAndMeasure_skymapper.py"
    )
    assert capture["kwargs"]["subtract_config_file"] == expected_subtract
    assert capture["kwargs"]["detect_config_file"] == expected_detect
    assert str(expected_subtract) in res.output
    assert str(expected_detect) in res.output


def test_explicit_flag_overrides_yaml(tmp_path, monkeypatch):
    cfg_file = _write_config(tmp_path, with_dia_configs=True)
    capture = {}
    monkeypatch.setattr(dia_module, "run", _fake_dia_run(capture))

    runner = CliRunner()
    res = runner.invoke(
        cli_module.cli,
        [
            "-c",
            str(cfg_file),
            "dia",
            "20240625",
            "--template",
            "templates/deep/r",
            "--subtract-config",
            "dia/override_subtract.py",
        ],
    )

    assert res.exit_code == 0, res.output
    expected_subtract = capture["config"].resolve_config("dia/override_subtract.py")
    # detect config still falls back to the YAML since it wasn't overridden.
    expected_detect = capture["config"].resolve_config(
        "dia/detectAndMeasure_skymapper.py"
    )
    assert capture["kwargs"]["subtract_config_file"] == expected_subtract
    assert capture["kwargs"]["detect_config_file"] == expected_detect


def test_no_flag_no_yaml_configs_passes_none(tmp_path, monkeypatch):
    cfg_file = _write_config(tmp_path)
    capture = {}
    monkeypatch.setattr(dia_module, "run", _fake_dia_run(capture))

    runner = CliRunner()
    res = runner.invoke(
        cli_module.cli,
        ["-c", str(cfg_file), "dia", "20240625", "--template", "templates/deep/r"],
    )

    assert res.exit_code == 0, res.output
    assert capture["kwargs"]["subtract_config_file"] is None
    assert capture["kwargs"]["detect_config_file"] is None
    assert "instrument default" in res.output
