"""Tests for external-template orchestration and the PS1 back-compat shim."""

from types import SimpleNamespace

from stips.core import external_template, ps1_template


def _config(band_maps, repo="/tmp/repo"):
    from pathlib import Path

    return SimpleNamespace(
        repo=Path(repo),
        profile=SimpleNamespace(ps1_band_map={}, template_band_maps=band_maps),
    )


def test_run_rejects_band_not_in_source_map():
    cfg = _config({"skymapper": {"r": "r", "i": "i"}})
    result = external_template.run("skymapper", 102.2, -36.0, "v", cfg)
    assert result.success is False
    assert "v" in result.error
    assert "r, i" in result.error or "i, r" in result.error


def test_run_rejects_unknown_source():
    cfg = _config({"skymapper": {"r": "r"}})
    result = external_template.run("decals", 102.2, -36.0, "r", cfg)
    assert result.success is False
    assert "decals" in result.error


def test_run_default_collection_is_source_namespaced(monkeypatch):
    cfg = _config({"skymapper": {"i": "i"}})
    monkeypatch.setattr(external_template, "check_exists", lambda *a, **k: True)
    result = external_template.run("skymapper", 102.2, -36.0, "i", cfg)
    assert result.collection == "templates/skymapper/i"
    assert result.skipped is True
    assert result.success is True


def test_run_overwrite_bypasses_exists_check(monkeypatch):
    cfg = _config({"skymapper": {"i": "i"}})
    monkeypatch.setattr(external_template, "check_exists", lambda *a, **k: True)
    calls = {}

    def fake_stack(args, config, **kwargs):
        calls["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(external_template, "run_with_stack", fake_stack)
    result = external_template.run("skymapper", 102.2, -36.0, "i", cfg, overwrite=True)
    assert result.skipped is False
    assert "--overwrite" in calls["args"]


def test_run_passes_mjd_window_to_ingest(monkeypatch):
    cfg = _config({"skymapper": {"i": "i"}})
    monkeypatch.setattr(external_template, "check_exists", lambda *a, **k: False)
    calls = {}

    def fake_stack(args, config, **kwargs):
        calls["args"] = args
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(external_template, "run_with_stack", fake_stack)
    external_template.run(
        "skymapper", 102.2, -36.0, "i", cfg, mjd_start=58000.0, mjd_end=59000.0
    )
    assert "--mjd-start" in calls["args"]
    assert "58000" in " ".join(calls["args"])


def test_run_reports_failure_from_subprocess(monkeypatch):
    cfg = _config({"skymapper": {"i": "i"}})
    monkeypatch.setattr(external_template, "check_exists", lambda *a, **k: False)
    monkeypatch.setattr(
        external_template,
        "run_with_stack",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    result = external_template.run("skymapper", 102.2, -36.0, "i", cfg)
    assert result.success is False
    assert "boom" in result.error


def test_ps1_shim_still_exposes_its_public_api():
    """run.py, cli.py and bps.py import these names; they must not move."""
    assert hasattr(ps1_template, "run")
    assert hasattr(ps1_template, "check_exists")
    assert ps1_template.PS1TemplateResult is external_template.ExternalTemplateResult


def test_run_parses_tract_and_patch_from_data_id_stdout(monkeypatch):
    """The ingest.py log line is ``Data ID: {'skymap': ..., 'tract': 1825,
    'patch': 0}`` -- containing ``'tract':``, not ``tract=``. A guard that
    requires the literal substring ``tract=`` (the historical bug) never
    matches, so tract/patch stayed None forever. This must FAIL against that
    old guard and pass once the guard is dropped in favor of always applying
    the regex searches.
    """
    cfg = _config({"skymapper": {"i": "i"}})
    monkeypatch.setattr(external_template, "check_exists", lambda *a, **k: False)
    stdout = (
        "[2024-01-01 00:00:00] INFO: SUCCESS: skymapper template ingested!\n"
        "[2024-01-01 00:00:00] INFO:   Collection: templates/skymapper/i\n"
        "[2024-01-01 00:00:00] INFO:   Data ID: {'skymap': 'stips_skymap', "
        "'tract': 1825, 'patch': 0}\n"
        "[2024-01-01 00:00:00] INFO:   FITS file: /tmp/out/lsst_template_skymapper_i.fits\n"
    )
    monkeypatch.setattr(
        external_template,
        "run_with_stack",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    result = external_template.run("skymapper", 102.2, -36.0, "i", cfg)
    assert result.success is True
    assert result.tract == 1825
    assert result.patch == 0
    assert result.fits_path == "/tmp/out/lsst_template_skymapper_i.fits"


def test_ps1_shim_delegates_with_source_ps1(monkeypatch):
    cfg = _config({}, repo="/tmp/repo")
    cfg.profile.ps1_band_map = {"r": "r"}
    seen = {}

    def fake_run(source, ra, dec, band, config, **kwargs):
        seen["source"] = source
        return external_template.ExternalTemplateResult(
            success=True, source=source, band=band, collection="templates/ps1/r"
        )

    monkeypatch.setattr(external_template, "run", fake_run)
    result = ps1_template.run(ra=210.9, dec=54.3, band="r", config=cfg)
    assert seen["source"] == "ps1"
    assert result.collection == "templates/ps1/r"


def test_cli_external_template_is_registered():
    from click.testing import CliRunner
    from stips.cli import cli

    result = CliRunner().invoke(cli, ["external-template", "--help"])
    assert result.exit_code == 0
    assert "--source" in result.output
    assert "skymapper" in result.output


def test_cli_ps1_template_still_registered():
    from click.testing import CliRunner
    from stips.cli import cli

    result = CliRunner().invoke(cli, ["ps1-template", "--help"])
    assert result.exit_code == 0


def _reload_cli():
    import importlib

    import stips.cli as cli_mod

    return importlib.reload(cli_mod)


def test_cli_source_choices_come_from_the_registry(monkeypatch):
    """A hardcoded click.Choice made the documented one-file extension false."""
    from stips.pipeline_tools.external_template import sources as src_mod

    monkeypatch.setitem(src_mod.SOURCES, "decals", object())
    try:
        cli = _reload_cli()
        params = {p.name: p for p in cli.cli.commands["external-template"].params}
        choices = params["source"].type.choices
        assert "decals" in choices
        assert {"ps1", "skymapper"} <= set(choices)
    finally:
        monkeypatch.undo()
        _reload_cli()
