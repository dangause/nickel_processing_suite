"""STIPS - Small Telescope Image Processing Suite CLI.

Unified command-line interface for processing small-telescope data
with the LSST Science Pipelines.

Usage:
    stips -c <config.yaml> calibs 20240625
    stips -c <config.yaml> science 20240625
    stips -c <config.yaml> dia 20240625 --auto-template
    stips -c <config.yaml> env

Configuration:
    The group-level ``-c/--config`` YAML file is the sole config source; its
    ``env:`` block supplies REPO/STACK_DIR/INSTRUMENT_DIR/RAW_PARENT_DIR.

    stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml dia 20230519 --auto
    stips -c scripts/config/2020wnt/pipeline_ps1_template.yaml calibs 20201207
"""

from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path

import click

from stips.core import config as cfg_module


def _print_error(msg: str) -> None:
    """Print error message in red."""
    click.secho(f"Error: {msg}", fg="red", err=True)


def _print_success(msg: str) -> None:
    """Print success message in green."""
    click.secho(msg, fg="green")


def _print_info(msg: str) -> None:
    """Print info message."""
    click.echo(msg)


@click.group()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    help="YAML config file (its env: block supplies REPO/STACK_DIR/INSTRUMENT_DIR/RAW_PARENT_DIR)",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose logging (DEBUG level)",
)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None, verbose: bool) -> None:
    """STIPS - Small Telescope Image Processing Suite. LSST pipeline tools.

    Process small-telescope observations using LSST Science Pipelines.
    Configure your environment with a YAML config file's env: block.

    \b
    Quick start:
        stips env                    # Check configuration
        stips calibs 20240625        # Run calibrations
        stips science 20240625       # Process science frames
        stips dia 20240625 --auto    # Difference imaging

    \b
    Selecting a config:
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml env
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml calibs ...
    """
    # Configure logging for all core modules
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="[%(levelname)s] %(message)s",
    )

    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _load_config(ctx: click.Context) -> cfg_module.Config:
    """Load configuration from the group-level -c/--config YAML file."""
    config_path = ctx.obj.get("config_path")
    if not config_path:
        _print_error(
            "No config provided. Pass -c <config.yaml> before the command, e.g.\n"
            "  stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml calibs 20230519"
        )
        sys.exit(1)
    try:
        return cfg_module.load(config_path)
    except ValueError as e:
        _print_error(str(e))
        sys.exit(1)


def _try_load_config(ctx: click.Context) -> cfg_module.Config | None:
    """Load config, returning None on a missing/invalid ``-c`` instead of exiting.

    For handlers where that is a soft, recoverable condition (e.g. the
    dashboard). Never raises ``SystemExit``; the caller decides how to degrade.
    """
    config_path = ctx.obj.get("config_path")
    if not config_path:
        return None
    try:
        return cfg_module.load(config_path)
    except ValueError:
        return None


def _report_result(result, *, success_msg: str, fail_msg: str, details=None) -> None:
    """Report a ``*Result``: success prints ``success_msg`` + ``details`` lines;
    failure prints ``fail_msg: {result.error}`` in red and exits 1."""
    if result.success:
        _print_success(success_msg)
        for line in details or []:
            click.echo(line)
    else:
        _print_error(f"{fail_msg}: {result.error}")
        sys.exit(1)


def pass_config(f):
    """Decorator: load the group ``-c`` config (exiting via :func:`_load_config`
    on error) and pass it to the handler as ``def handler(ctx, config, ...)``."""

    @functools.wraps(f)
    @click.pass_context
    def wrapper(ctx: click.Context, *args, **kwargs):
        return f(ctx, _load_config(ctx), *args, **kwargs)

    return wrapper


def _load_lightcurve_config(
    ctx: click.Context,
    repo: Path | None = None,
    stack_dir: Path | None = None,
) -> cfg_module.Config:
    """Load configuration for the lightcurve command.

    Two mutually exclusive sources, in priority order:

    1. The group-level ``-c/--config`` YAML (its ``env:`` block). Any
       ``--repo``/``--stack-dir`` flags then override the loaded values.
    2. Explicit ``--repo`` and ``--stack-dir`` flags, with no ``-c``. This
       builds a minimal Config directly from those two flags — lightcurve only
       needs the repo and the stack at runtime.

    There is no ``.env`` file, no ``os.environ`` fallback, and no cwd/stack/
    obs-package auto-detection.

    Args:
        ctx: Click context
        repo: Butler repository path (overrides ``-c``; required without ``-c``)
        stack_dir: LSST stack directory (overrides ``-c``; required without ``-c``)

    Returns:
        Config object with explicit overrides applied
    """
    import dataclasses

    config_path = ctx.obj.get("config_path")
    if config_path:
        try:
            config = cfg_module.load(config_path)
        except ValueError as e:
            _print_error(str(e))
            sys.exit(1)
        if repo is not None:
            config = dataclasses.replace(config, repo=repo)
        if stack_dir is not None:
            config = dataclasses.replace(config, stack_dir=stack_dir)
        return config

    # No -c: build a minimal Config from explicit flags.
    if repo is None:
        _print_error(
            "lightcurve needs configuration. Either:\n"
            "  1. Pass -c <config.yaml> before the command, or\n"
            "  2. Provide --repo and --stack-dir on the command line."
        )
        sys.exit(1)

    if stack_dir is None:
        _print_error("lightcurve without -c requires both --repo and --stack-dir.")
        sys.exit(1)

    # The Config dataclass requires instrument_dir and raw_parent_dir, but the
    # lightcurve command uses neither (it only needs repo + stack_dir). Pass
    # inert placeholders for those two fields — no auto-detection, no env.
    return cfg_module.Config(
        repo=repo,
        stack_dir=stack_dir,
        instrument_dir=Path("/nonexistent"),
        raw_parent_dir=Path("/nonexistent"),
    )


# =============================================================================
# env - Show configuration
# =============================================================================


@cli.command()
@pass_config
def env(ctx: click.Context, config: cfg_module.Config) -> None:
    """Show current configuration and validate paths."""
    click.echo("\nSTIPS Configuration")
    click.echo("=" * 40)

    # Show the config file that supplied the env
    config_path = ctx.obj.get("config_path")
    if config_path:
        click.echo(f"\n{'Config:':<20} {config_path}")

    click.echo(f"\n{'REPO:':<20} {config.repo}")
    click.echo(f"{'STACK_DIR:':<20} {config.stack_dir}")
    click.echo(f"{'INSTRUMENT_DIR:':<20} {config.instrument_dir}")
    click.echo(f"{'RAW_PARENT_DIR:':<20} {config.raw_parent_dir}")

    if config.cp_pipe_dir:
        click.echo(f"{'CP_PIPE_DIR:':<20} {config.cp_pipe_dir}")
    if config.refcat_repo:
        click.echo(f"{'REFCAT_REPO:':<20} {config.refcat_repo}")

    # Validate paths
    errors = config.validate()
    if errors:
        click.echo("\n" + click.style("Validation errors:", fg="red"))
        for err in errors:
            click.echo(f"  - {err}")
        sys.exit(1)
    else:
        click.echo("\n" + click.style("✓ All paths valid", fg="green"))

    # Check stack
    click.echo("\nChecking LSST stack...", nl=False)
    from stips.core.stack import check_stack

    if check_stack(config):
        click.echo(click.style(" ✓ available", fg="green"))
    else:
        click.echo(click.style(" ✗ not accessible", fg="red"))
        click.echo("  Make sure LSST stack is installed at STACK_DIR")


# =============================================================================
# calibs - Nightly calibrations
# =============================================================================


@cli.command()
@click.argument("night")
@click.option("-j", "--jobs", default=4, help="Parallel jobs (default: 4)")
@pass_config
def calibs(
    ctx: click.Context, config: cfg_module.Config, night: str, jobs: int
) -> None:
    """Run nightly calibrations (bias, flat, defects).

    NIGHT is the observing date in YYYYMMDD format.

    \b
    Example:
        stips calibs 20240625
        stips calibs 20240625 --jobs 8
    """
    _print_info(f"Running calibrations for {night}...")

    from stips.core import calibs as calibs_module

    result = calibs_module.run(night, config, jobs=jobs)

    _report_result(
        result,
        success_msg=f"\n✓ Calibrations complete for {night}",
        fail_msg="Calibrations failed",
        details=[
            f"  Raw collection: {result.raw_run}",
            f"  Bias: {result.cp_bias}",
            f"  Flat: {result.cp_flat}",
            f"  Calib chain: {result.calib_chain}",
        ],
    )


# =============================================================================
# measure-crosstalk - Measure crosstalk coefficients from exposures
# =============================================================================


@cli.command("measure-crosstalk")
@click.argument("nights", nargs=-1, required=True)
@click.option("-j", "--jobs", default=4, help="Parallel jobs (default: 4)")
@click.option(
    "--export-dir",
    type=click.Path(),
    help="Directory to export the measured matrix ECSV (default: REPO/crosstalk)",
)
@pass_config
def measure_crosstalk(
    ctx: click.Context,
    config: cfg_module.Config,
    nights: tuple[str, ...],
    jobs: int,
    export_dir: str | None,
) -> None:
    """Measure intra-detector crosstalk from exposures (cp_pipe MeasureCrosstalk).

    Runs the cp_pipe crosstalk pipeline over the NIGHTS' science frames to derive
    the coefficient matrix, certifies it into the calib chain (so ISR applies it),
    and exports the matrix for inspection. Run this once when you have no known
    coefficients. Requires bias calibs to be built first (the measurement ISR
    applies bias), and works best with frames containing bright sources spanning
    amplifier boundaries.

    NIGHTS are observing dates in YYYYMMDD format.

    \b
    Example:
        stips -c scripts/config/ctio1m/pipeline.yaml measure-crosstalk 20070321 20070322
    """
    from pathlib import Path

    prof = config.require_profile()
    if prof.crosstalk is None:
        _print_error(
            f"{prof.name} declares no crosstalk in its profile; nothing to measure. "
            "Add a CrosstalkSpec (even a placeholder) to enable crosstalk."
        )
        sys.exit(1)

    _print_info(f"Measuring crosstalk for {prof.name} from nights: {', '.join(nights)}")

    from stips.core import crosstalk as crosstalk_module

    result = crosstalk_module.measure_crosstalk(
        list(nights),
        config,
        jobs=jobs,
        export_dir=Path(export_dir) if export_dir else None,
    )

    details = [f"  Calib collection: {result.calib_collection}"]
    if result.success and result.export_path:
        details.append(f"  Matrix exported:  {result.export_path}")
    _report_result(
        result,
        success_msg="\n✓ Crosstalk measured and certified",
        fail_msg="Crosstalk measurement failed",
        details=details,
    )


# =============================================================================
# science - Science processing
# =============================================================================


@cli.command()
@click.argument("night")
@click.option("-j", "--jobs", default=8, help="Parallel jobs (default: 8)")
@click.option("--bad", help="Comma-separated exposure IDs to exclude")
@click.option(
    "--bad-file",
    type=click.Path(exists=True, path_type=Path),
    help="File with bad exposure IDs",
)
@click.option("--object", "object_filter", help="Filter by OBJECT header value")
@click.option("--skip-coadds", is_flag=True, help="Skip coadd generation")
@click.option(
    "--calibrate-config",
    "science_config",
    type=click.Path(exists=True, path_type=Path),
    help="Override calibrateImage config",
)
@click.option(
    "--ra", type=float, help="Target RA in degrees (enables coordinate validation)"
)
@click.option(
    "--dec", type=float, help="Target Dec in degrees (enables coordinate validation)"
)
@pass_config
def science(
    ctx: click.Context,
    config: cfg_module.Config,
    night: str,
    jobs: int,
    bad: str | None,
    bad_file: Path | None,
    object_filter: str | None,
    skip_coadds: bool,
    science_config: Path | None,
    ra: float | None,
    dec: float | None,
) -> None:
    """Run science processing (ISR, WCS, photometry).

    NIGHT is the observing date in YYYYMMDD format.

    When --ra and --dec are provided, exposures with coordinates far from
    the target are automatically excluded to prevent qgraph failures from
    missing reference catalog coverage.

    \b
    Example:
        stips science 20240625
        stips science 20240625 --object 2020wnt --skip-coadds
        stips science 20240625 --bad 12345,12346
        stips science 20240625 --object 2023ixf --ra 210.91 --dec 54.32
    """
    if (ra is None) != (dec is None):
        _print_error("--ra and --dec must be provided together")
        sys.exit(1)

    _print_info(f"Running science processing for {night}...")

    from stips.core import science as science_module

    result = science_module.run(
        night,
        config,
        jobs=jobs,
        bad_exposures=bad,
        bad_file=bad_file,
        object_filter=object_filter,
        skip_coadds=skip_coadds,
        science_config=science_config,
        target_ra=ra,
        target_dec=dec,
    )

    details = [f"  Science run: {result.science_run}"]
    if result.success and result.coadd_run:
        details.append(f"  Coadd run: {result.coadd_run}")
    _report_result(
        result,
        success_msg=f"\n✓ Science processing complete for {night}",
        fail_msg="Science processing failed",
        details=details,
    )


# =============================================================================
# dia - Difference imaging
# =============================================================================


@cli.command()
@click.argument("night")
@click.option("-j", "--jobs", default=8, help="Parallel jobs (default: 8)")
@click.option("-t", "--template", help="Template collection to use")
@click.option("--auto", "auto_template", is_flag=True, help="Auto-discover template")
@click.option("--prefer-ps1", is_flag=True, help="Prefer PS1 templates (with --auto)")
@click.option("-b", "--band", help="Filter by band (b/v/r/i)")
@click.option("--object", "object_filter", help="Filter by OBJECT header value")
@click.option("--bad", help="Comma-separated exposure IDs to exclude")
@click.option(
    "--bad-file",
    type=click.Path(exists=True, path_type=Path),
    help="File with bad exposure IDs",
)
@click.option(
    "--subtract-config",
    "subtract_config",
    help=(
        "Override for the subtractImages task config (resolved instrument-dir-"
        "first, else framework default, same as `stips run`). Defaults to the "
        "-c YAML's configs.dia.subtract_images if set."
    ),
)
@click.option(
    "--detect-config",
    "detect_config",
    help=(
        "Override for the detectAndMeasure task config (resolved instrument-"
        "dir-first, else framework default, same as `stips run`). Defaults to "
        "the -c YAML's configs.dia.detect_and_measure if set."
    ),
)
@pass_config
def dia(
    ctx: click.Context,
    config: cfg_module.Config,
    night: str,
    jobs: int,
    template: str | None,
    auto_template: bool,
    prefer_ps1: bool,
    band: str | None,
    object_filter: str | None,
    bad: str | None,
    bad_file: Path | None,
    subtract_config: str | None,
    detect_config: str | None,
) -> None:
    """Run difference imaging analysis.

    NIGHT is the observing date in YYYYMMDD format.

    \b
    Example:
        stips dia 20240625 --auto
        stips dia 20240625 --template templates/deep/r
        stips dia 20240625 --auto --band r --object 2020wnt
        stips dia 20240625 --template templates/skymapper/i \\
            --subtract-config dia/subtractImages_skymapper.py

    If --subtract-config/--detect-config are omitted, the group -c YAML's
    configs.dia.subtract_images/detect_and_measure are used when set (the same
    keys `stips run` reads) -- otherwise the instrument-dir/framework default
    DIA configs apply.
    """
    if not template and not auto_template:
        _print_error("Specify --template or --auto")
        sys.exit(1)

    _print_info(f"Running difference imaging for {night}...")

    from stips.core import dia as dia_module

    # Fall back to the group -c YAML's configs.dia.* when a flag is omitted,
    # reusing RunConfig's YAML parsing rather than a second scheme -- this is
    # what makes `stips dia` (not just `stips run`) honor a YAML-specified DIA
    # config instead of silently applying the instrument default.
    subtract_name = subtract_config
    detect_name = detect_config
    if subtract_name is None or detect_name is None:
        config_path = ctx.obj.get("config_path")
        if config_path:
            from stips.core.run import RunConfig

            yaml_dia_configs = RunConfig.from_yaml(config_path).dia_configs
            if subtract_name is None:
                subtract_name = yaml_dia_configs.subtract_images
            if detect_name is None:
                detect_name = yaml_dia_configs.detect_and_measure

    subtract_config_file = (
        config.resolve_config(subtract_name) if subtract_name else None
    )
    detect_config_file = config.resolve_config(detect_name) if detect_name else None

    _print_info(f"  Subtract config: {subtract_config_file or '(instrument default)'}")
    _print_info(f"  Detect config: {detect_config_file or '(instrument default)'}")

    result = dia_module.run(
        night,
        config,
        jobs=jobs,
        template=template,
        auto_template=auto_template,
        prefer_ps1=prefer_ps1,
        band=band,
        object_filter=object_filter,
        bad_exposures=bad,
        bad_file=bad_file,
        subtract_config_file=subtract_config_file,
        detect_config_file=detect_config_file,
    )

    _report_result(
        result,
        success_msg=f"\n✓ Difference imaging complete for {night}",
        fail_msg="Difference imaging failed",
        details=[
            f"  Template: {result.template_collection}",
            f"  DIA run: {result.diff_run}",
            f"  Difference images: {result.diff_image_count}",
            f"  DIA sources: {result.dia_source_count}",
        ],
    )


# =============================================================================
# download - Fetch data from archive
# =============================================================================


@cli.command()
@click.argument(
    "nights",
    nargs=-1,
)
@click.option("--overwrite", is_flag=True, help="Re-download existing files")
@click.option(
    "--missing-only",
    is_flag=True,
    help="Only download nights with no FITS files in raw directory",
)
@pass_config
def download(
    ctx: click.Context,
    config: cfg_module.Config,
    nights: tuple[str, ...],
    overwrite: bool,
    missing_only: bool,
) -> None:
    """Download raw data for one or more nights using the active instrument's data-fetch hook.

    Nights are taken from the command line (one or more YYYYMMDD dates). If no
    nights are given, they are read from the group -c config YAML's science: and
    coadd template: nights: lists.

    \b
    Examples:
        # Download all nights from the -c pipeline config
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml download

        # Download only missing nights from config
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml download --missing-only

        # Download specific nights
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml download 20240625
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml download 20240416 20240429
    """
    from stips.core import download as download_module

    prof = config.require_profile()
    if prof.fetch_data is None:
        raise click.ClickException(
            f"Data download is not configured for instrument '{prof.name}'. "
            f"Place raw FITS under {config.raw_parent_dir}/<night>/raw/."
        )

    nights_list: list[str] = list(nights)

    if not nights_list:
        # No nights on the CLI: read them from the group -c config YAML.
        config_path = ctx.obj.get("config_path")
        if not config_path:
            _print_error(
                "No nights given and no -c config to read them from. Pass "
                "YYYYMMDD night(s) or -c <config.yaml> with science:/template: nights:."
            )
            sys.exit(1)

        nights_list = download_module.nights_from_config(config_path)
        if not nights_list:
            _print_error(f"No nights found in config file: {config_path}")
            sys.exit(1)

        _print_info(f"Found {len(nights_list)} nights in config")

    if missing_only:
        missing = download_module.missing_nights(nights_list, config)
        skipped = len(nights_list) - len(missing)
        if skipped > 0:
            _print_info(f"Skipping {skipped} nights that already have data")
        nights_list = missing

    if not nights_list:
        _print_success("All nights already have data, nothing to download")
        return

    _print_info(f"Downloading {len(nights_list)} nights...")

    def _on_event(night: str, status: str, error: str | None) -> None:
        if status == "start":
            _print_info(f"Downloading {night}...")
        elif status == "ok":
            _print_success(f"✓ Downloaded {night}")
        elif status == "not_found":
            click.secho(f"⚠ {night}: not found in archive", fg="yellow")
        elif error:
            _print_error(f"Failed to download {night}: {error}")
        else:
            _print_error(f"Failed to download {night}")

    result = download_module.fetch_nights(
        nights_list, config, overwrite=overwrite, on_event=_on_event
    )

    # Summary
    click.echo("")
    if result.succeeded:
        _print_success(f"Downloaded: {len(result.succeeded)} nights")
    if result.not_in_archive:
        click.secho(
            f"Not in archive: {len(result.not_in_archive)} nights "
            f"({', '.join(result.not_in_archive)})",
            fg="yellow",
        )
    if result.failed:
        _print_error(
            f"Failed: {len(result.failed)} nights ({', '.join(result.failed)})"
        )
        sys.exit(1)

    if result.not_in_archive and not result.succeeded:
        click.secho(
            "\nNone of the requested nights were found in the archive. "
            "The data may not have been uploaded yet, or the dates may be incorrect.",
            fg="yellow",
        )
        sys.exit(2)


# =============================================================================
# bootstrap - Initialize a new Butler repository
# =============================================================================


@cli.command()
@pass_config
def bootstrap(ctx: click.Context, config: cfg_module.Config) -> None:
    """Initialize a new Butler repository.

    Creates the Butler repo, registers the instrument, ingests reference
    catalogs, and sets up the skymap. Run this once before processing data.

    Configuration comes from the group -c config YAML's env: block.

    \b
    Examples:
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml bootstrap
        stips -c scripts/config/2020wnt/pipeline_nickel_template.yaml bootstrap
    """
    from stips.core import bootstrap as bootstrap_module

    _print_info("Bootstrapping Butler repository...")
    _print_info(f"  Repo: {config.repo}")

    result = bootstrap_module.run(config)

    _report_result(
        result,
        success_msg="✓ Bootstrap complete",
        fail_msg="Bootstrap failed",
        details=[f"  Repository ready: {config.repo}"],
    )


# =============================================================================
# clean - Remove processing runs from Butler repo
# =============================================================================


@cli.command()
@click.option(
    "--night",
    "nights",
    multiple=True,
    help="Only clean this night (repeatable, e.g. --night 20201207 --night 20201219)",
)
@click.option(
    "--step",
    "steps",
    multiple=True,
    type=click.Choice(["calibs", "science", "dia", "fphot", "coadd"]),
    help="Only clean this step (repeatable, e.g. --step calibs --step science)",
)
@click.option(
    "--dry-run", is_flag=True, help="List what would be removed without deleting"
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Skip confirmation prompt",
)
@pass_config
def clean(
    ctx: click.Context,
    config: cfg_module.Config,
    nights: tuple[str, ...],
    steps: tuple[str, ...],
    dry_run: bool,
    yes: bool,
) -> None:
    """Remove processing runs from the Butler repository.

    Deletes science (processCcd), DIA (diff), forced photometry, and per-night
    coadd runs. Calibrations (cp, calib) are only removed when explicitly
    requested via --step calibs. Preserves raws, reference catalogs, and skymaps.

    Configuration comes from the group -c config YAML's env: block.

    \b
    Examples:
        # Preview what would be removed
        stips -c pipeline.yaml clean --dry-run

    \b
        # Remove all processing runs (not calibs)
        stips -c pipeline.yaml clean -y

    \b
        # Remove calibs and all processing runs
        stips -c pipeline.yaml clean --step calibs --step science --step dia --step fphot --step coadd -y

    \b
        # Remove only DIA and forced phot runs
        stips -c pipeline.yaml clean --step dia --step fphot

    \b
        # Remove runs for specific nights only
        stips -c pipeline.yaml clean --night 20201207 --night 20201219
    """
    from stips.core import clean as clean_module

    nights_list = list(nights) if nights else None
    steps_list = list(steps) if steps else None

    # First show what would be removed
    _print_info(f"Repository: {config.repo}")
    if nights_list:
        _print_info(f"Nights: {', '.join(nights_list)}")
    if steps_list:
        _print_info(f"Steps: {', '.join(steps_list)}")

    # Discover once. The confirmed plan is exactly what gets executed, so the
    # user can never approve one set of collections and have a different set
    # deleted (they cannot drift between a separate preview and delete pass).
    plan = clean_module.plan(config, nights=nights_list, steps=steps_list)

    if plan.error:
        _print_error(plan.error)
        sys.exit(1)

    if plan.is_empty:
        _print_info("No collections found to remove")
        return

    _print_info(f"\nFound {len(plan.names)} collections to remove:")
    for col in plan.names:
        click.echo(f"  {col}")

    if dry_run:
        _print_info("\n[DRY RUN] No changes made")
        return

    # Confirm
    if not yes:
        click.echo()
        if not click.confirm(f"Remove {len(plan.names)} collections?"):
            click.echo("Cancelled")
            return

    # Do the actual removal of THIS plan (no re-discovery).
    result = clean_module.execute(config, plan)

    if result.success:
        _print_success(f"\n✓ Removed {len(result.collections_removed)} collections")
    else:
        _print_error("Some collections could not be removed:")
        for err in result.errors:
            click.echo(f"  {err}")
        if result.collections_removed:
            click.echo(
                f"\n  ({len(result.collections_removed)} collections were removed successfully)"
            )
        sys.exit(1)


# =============================================================================
# ps1-template - Ingest PS1 template for DIA
# =============================================================================


@cli.command("ps1-template")
@click.option("--ra", type=float, required=True, help="Right ascension in degrees")
@click.option("--dec", type=float, required=True, help="Declination in degrees")
@click.option(
    "-b",
    "--band",
    required=True,
    # No static click.Choice: the set of PS1-eligible bands is instrument-specific
    # and lives in the active profile's ``ps1_band_map``, which is only loaded at
    # runtime (INSTRUMENT_DIR). We validate against it inside the handler instead,
    # so the error can name the profile's actual eligible bands.
    help="Local science band; must be PS1-eligible for the active instrument",
)
@click.option("--collection", help="Output collection (default: templates/ps1/{band})")
@click.option("--tract", type=int, help="Tract number (auto-determined if not set)")
@click.option(
    "--size", type=float, default=0.2, help="Cutout size in degrees (default: 0.2)"
)
@click.option("--degrade-seeing", type=float, help="Convolve to this FWHM in arcsec")
@click.option("--overwrite", is_flag=True, help="Replace existing template")
@pass_config
def ps1_template(
    ctx: click.Context,
    config: cfg_module.Config,
    ra: float,
    dec: float,
    band: str,
    collection: str | None,
    tract: int | None,
    size: float,
    degrade_seeing: float | None,
    overwrite: bool,
) -> None:
    """Download and ingest PS1 template for difference imaging.

    PS1 templates are available only for the active instrument's PS1-eligible
    bands (the keys of the profile's ``ps1_band_map`` — e.g. r and i for Nickel).

    \b
    Example:
        stips ps1-template --ra 210.91 --dec 54.32 --band r
        stips ps1-template --ra 210.91 --dec 54.32 --band i --degrade-seeing 2.0
    """
    from stips.core.pipeline import ps1_band_map

    eligible = ps1_band_map(config)
    if band not in eligible:
        allowed = ", ".join(sorted(eligible)) or "(none configured)"
        _print_error(
            f"Band {band!r} is not PS1-eligible for this instrument; "
            f"available: {allowed}"
        )
        sys.exit(1)

    _print_info(f"Ingesting PS1 {band}-band template at RA={ra:.4f}, Dec={dec:.4f}...")

    from stips.core import ps1_template as ps1_module

    # The skip-if-exists policy (and the exists check itself) lives in
    # ps1_module.run(); the handler just surfaces the result.
    result = ps1_module.run(
        ra=ra,
        dec=dec,
        band=band,
        config=config,
        collection=collection,
        tract=tract,
        size=size,
        degrade_seeing=degrade_seeing,
        overwrite=overwrite,
    )

    if result.skipped:
        _print_info(f"PS1 template already exists in {result.collection}")
        _print_info("Use --overwrite to replace")
        return

    details = [f"  Collection: {result.collection}"]
    if result.tract is not None:
        patches = result.patches or ([result.patch] if result.patch is not None else [])
        if len(patches) > 1:
            details.append(f"  Tract: {result.tract}, Patches: {patches}")
        else:
            details.append(f"  Tract: {result.tract}, Patch: {result.patch}")
    if result.fits_path:
        details.append(f"  FITS file: {result.fits_path}")
    _report_result(
        result,
        success_msg="\n✓ PS1 template ingested",
        fail_msg="PS1 template ingestion failed",
        details=details,
    )


# =============================================================================
# external-template - Ingest an external survey template for DIA
# =============================================================================


def _external_source_choices() -> list[str]:
    """Valid ``--source`` values: every registered external-survey adapter.

    Driven by the ``SOURCES`` registry rather than a literal so a new adapter is
    one ``sources/*.py`` file, as ``docs/architecture.md`` promises. Evaluated
    once at import (click builds the Choice at decoration time); the registry is
    populated at import of ``sources/__init__``, so nothing is missed.
    """
    from stips.pipeline_tools.external_template.sources import SOURCES

    return sorted(SOURCES)


@cli.command("external-template")
@click.option(
    "--source",
    type=click.Choice(_external_source_choices()),
    required=True,
    help="Survey to fetch the template from",
)
@click.option("--ra", type=float, required=True, help="Right ascension in degrees")
@click.option("--dec", type=float, required=True, help="Declination in degrees")
@click.option(
    "-b",
    "--band",
    required=True,
    help="Local science band; must be mapped for this source in the profile",
)
@click.option(
    "--collection", help="Output collection (default: templates/{source}/{band})"
)
@click.option("--tract", type=int, help="Tract number (auto-determined if not set)")
@click.option(
    "--size", type=float, default=0.2, help="Cutout size in degrees (default: 0.2)"
)
@click.option("--degrade-seeing", type=float, help="Convolve to this FWHM in arcsec")
@click.option("--mjd-start", type=float, help="Earliest frame MJD to consider")
@click.option("--mjd-end", type=float, help="Latest frame MJD to consider")
@click.option("--overwrite", is_flag=True, help="Replace existing template")
@pass_config
def external_template_cmd(
    ctx: click.Context,
    config: cfg_module.Config,
    source: str,
    ra: float,
    dec: float,
    band: str,
    collection: str | None,
    tract: int | None,
    size: float,
    degrade_seeing: float | None,
    mjd_start: float | None,
    mjd_end: float | None,
    overwrite: bool,
) -> None:
    """Download and ingest an external survey template for difference imaging.

    Eligible bands come from the profile's ``template_band_maps[source]``
    (falling back to ``ps1_band_map`` for source=ps1).

    \b
    SkyMapper caveats (southern fields, Dec <= -30):
      - single-epoch 100s frames, NOT deep stacks
      - each REQUEST is capped at 0.17 deg (10.2'), but --size above that is
        served by mosaicking tiles of one frame, up to the CCD's 17' x 34'
      - prefer a CTIO self-coadd template when SN-free epochs exist

    \b
    Example:
        stips external-template --source ps1 --ra 210.91 --dec 54.32 -b r
        stips external-template --source skymapper --ra 102.25 --dec -36.01 -b i
    """
    from stips.core import external_template as et
    from stips.core.pipeline import template_band_map

    eligible = template_band_map(config, source)
    if band not in eligible:
        allowed = ", ".join(sorted(eligible)) or "(none configured)"
        _print_error(
            f"Band {band!r} has no {source} template mapping for this "
            f"instrument; available: {allowed}"
        )
        sys.exit(1)

    _print_info(
        f"Ingesting {source} {band}-band template at RA={ra:.4f}, Dec={dec:.4f}..."
    )

    result = et.run(
        source,
        ra,
        dec,
        band,
        config,
        collection=collection,
        tract=tract,
        size=size,
        degrade_seeing=degrade_seeing,
        mjd_start=mjd_start,
        mjd_end=mjd_end,
        overwrite=overwrite,
    )

    if result.skipped:
        _print_info(f"{source} template already exists in {result.collection}")
        _print_info("Use --overwrite to replace")
        return

    details = [f"  Collection: {result.collection}"]
    if result.tract is not None:
        patches = result.patches or ([result.patch] if result.patch is not None else [])
        if len(patches) > 1:
            details.append(f"  Tract: {result.tract}, Patches: {patches}")
        else:
            details.append(f"  Tract: {result.tract}, Patch: {result.patch}")
    if result.fits_path:
        details.append(f"  FITS file: {result.fits_path}")
    _report_result(
        result,
        success_msg=f"\n✓ {source} template ingested",
        fail_msg=f"{source} template ingestion failed",
        details=details,
    )


# =============================================================================
# fphot - Forced photometry at RA/Dec
# =============================================================================


@cli.command("fphot")
@click.argument("night")
@click.option("--ra", type=float, required=True, help="Right ascension in degrees")
@click.option("--dec", type=float, required=True, help="Declination in degrees")
@click.option("-b", "--band", help="Filter by band (default: all bands)")
@click.option(
    "--image-type",
    type=click.Choice(["visit", "diffim", "both"]),
    default="diffim",
    help="Image type for photometry (default: diffim)",
)
@pass_config
def fphot(
    ctx: click.Context,
    config: cfg_module.Config,
    night: str,
    ra: float,
    dec: float,
    band: str | None,
    image_type: str,
) -> None:
    """Run forced photometry at specified RA/Dec coordinates.

    Performs forced photometry on calibrated visit images and/or difference images.

    \b
    Example:
        stips fphot 20230519 --ra 210.91 --dec 54.32
        stips fphot 20230519 --ra 210.91 --dec 54.32 --band r --image-type both
    """
    _print_info(
        f"Running forced photometry for {night} at RA={ra:.4f}, Dec={dec:.4f}..."
    )

    from stips.core import fphot as fphot_module

    result = fphot_module.run(
        night=night,
        ra=ra,
        dec=dec,
        config=config,
        band=band,
        image_type=image_type,
    )

    _report_result(
        result,
        success_msg=f"\n✓ Forced photometry complete for {night}",
        fail_msg="Forced photometry failed",
        details=[f"  Collection: {coll}" for coll in result.output_collections],
    )


# =============================================================================
# lightcurve - Extract lightcurve from DIA sources
# =============================================================================


@cli.command("lightcurve")
@click.option("--ra", type=float, required=True, help="Right ascension in degrees")
@click.option("--dec", type=float, required=True, help="Declination in degrees")
@click.option(
    "--collections", required=True, help="Comma-separated DIA collections to query"
)
@click.option(
    "--repo",
    type=click.Path(exists=True, path_type=Path),
    help="Butler repository path",
)
@click.option(
    "--stack-dir",
    type=click.Path(exists=True, path_type=Path),
    help="LSST stack directory (required if not using -c)",
)
@click.option(
    "--radius", type=float, default=1.0, help="Match radius in arcsec (default: 1.0)"
)
@click.option(
    "--min-snr", type=float, default=3.0, help="Minimum S/N filter (default: 3.0)"
)
@click.option("-b", "--band", help="Restrict to single band")
@click.option("--name", help="Target name for plot title")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output CSV file")
@click.option("--plot/--no-plot", default=True, help="Generate plot (default: yes)")
@click.option(
    "--dataset-type",
    default="dia_source_unfiltered",
    help="Dataset type to query (default: dia_source_unfiltered). Use 'forced_phot_diffim_radec' for forced phot.",
)
@click.option(
    "--y-axis",
    type=click.Choice(["apparent_mag", "absolute_mag", "flux_nJy", "flux_adu"]),
    default="apparent_mag",
    help="Y-axis display mode (default: apparent_mag)",
)
@click.option(
    "--x-axis",
    type=click.Choice(["mjd", "days_since_explosion"]),
    default="mjd",
    help="X-axis display mode (default: mjd)",
)
@click.option(
    "--explosion-mjd",
    type=float,
    default=None,
    help="Explosion MJD (required with --x-axis=days_since_explosion)",
)
@click.option(
    "--distance-modulus",
    type=float,
    default=None,
    help="Distance modulus (required with --y-axis=absolute_mag)",
)
@click.option(
    "--max-mag-err",
    type=float,
    default=None,
    help="Maximum magnitude error for plot filtering",
)
@click.pass_context
def lightcurve(
    ctx: click.Context,
    ra: float,
    dec: float,
    collections: str,
    repo: Path | None,
    stack_dir: Path | None,
    radius: float,
    min_snr: float,
    band: str | None,
    name: str | None,
    output: Path | None,
    plot: bool,
    dataset_type: str,
    y_axis: str,
    x_axis: str,
    explosion_mjd: float | None,
    distance_modulus: float | None,
    max_mag_err: float | None,
) -> None:
    """Extract lightcurve from DIA source catalogs or forced photometry.

    Queries source catalogs for detections near the specified coordinates
    and generates a lightcurve CSV and optional plot.

    \b
    Collection globs below use the active instrument's collection prefix
    (``<prefix>``); the examples are shown for the Nickel reference profile.

    \b
    Example (standalone - no config file):
        stips lightcurve \\
            --repo /path/to/butler_repo --stack-dir /path/to/lsst_stack \\
            --ra 210.91 --dec 54.32 \\
            --collections "<prefix>/runs/*/diff/*/run" \\
            --name "SN 2023ixf"

    \b
    Example (with config file):
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml lightcurve \\
            --ra 210.91 --dec 54.32 \\
            --collections "<prefix>/runs/20230519/diff/*/run" --name "SN 2023ixf"

    \b
    Example (forced photometry):
        stips lightcurve --repo /path/to/repo --ra 210.91 --dec 54.32 \\
            --collections "<prefix>/runs/*/forcedPhotRaDec/*/run" \\
            --dataset-type forced_phot_diffim_radec --name "SN 2023ixf"
    """
    # Validate dependent options
    if x_axis == "days_since_explosion" and explosion_mjd is None:
        raise click.UsageError(
            "--explosion-mjd required with --x-axis=days_since_explosion"
        )
    if y_axis == "absolute_mag" and distance_modulus is None:
        raise click.UsageError("--distance-modulus required with --y-axis=absolute_mag")

    # Build config from the group -c YAML, or from explicit --repo/--stack-dir
    config = _load_lightcurve_config(ctx, repo=repo, stack_dir=stack_dir)

    _print_info(f"Extracting lightcurve at RA={ra:.4f}, Dec={dec:.4f}...")

    from stips.core import lightcurve as lc_module
    from stips.core.lightcurve import LightcurveConfig

    lc_config = LightcurveConfig(
        dataset_type=dataset_type,
        min_snr=min_snr,
        max_mag_err=max_mag_err,
        radius=radius,
        band=band,
        y_axis=y_axis,
        x_axis=x_axis,
        explosion_mjd=explosion_mjd,
        distance_modulus=distance_modulus,
    )

    # lc_config is the single source of truth for radius/min_snr/band/dataset_type
    # and the display knobs; no redundant loose kwargs.
    result = lc_module.run(
        ra=ra,
        dec=dec,
        collections=collections,
        config=config,
        name=name,
        output=output,
        plot=plot,
        lc_config=lc_config,
    )

    details = []
    if result.success:
        details.append(f"  Detections: {result.n_detections}")
        if result.csv_path:
            details.append(f"  CSV: {result.csv_path}")
        if result.plot_path:
            details.append(f"  Plot: {result.plot_path}")
    _report_result(
        result,
        success_msg="\n✓ Lightcurve extracted",
        fail_msg="Lightcurve extraction failed",
        details=details,
    )


# =============================================================================
# calib-metrics - Dump astrometric/photometric calibration metrics to CSV
# =============================================================================


@cli.command("calib-metrics")
@click.option(
    "--collection",
    default=None,
    show_default=True,
    help="Butler collection glob to query "
    "(defaults to <profile prefix>/runs/*/processCcd/*)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Output CSV path",
)
@click.option(
    "--night",
    help="Filter to a single night (YYYYMMDD). Omit to include all nights in the collection.",
)
@click.option(
    "--include-refcat-metrics",
    is_flag=True,
    default=False,
    help="Also pull single_visit_star_ref_match_{astrom,photom}_metrics (requires visit-quality pipeline)",
)
@pass_config
def calib_metrics(
    ctx: click.Context,
    config: cfg_module.Config,
    collection: str | None,
    output: Path,
    night: str | None,
    include_refcat_metrics: bool,
) -> None:
    """Extract per-visit astrometric/photometric calibration metrics to CSV.

    Reads REPO and STACK_DIR from the `env:` section of the group -c config YAML
    (same file you pass to `stips run`), then queries the Butler for:

    \b
      - preliminary_visit_summary          (always)
      - calibrateImage_metadata_metrics    (if present)
      - single_visit_star_ref_match_*_metrics  (with --include-refcat-metrics)

    \b
    Example:
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml calib-metrics \\
            -o calib_metrics_2023ixf_all.csv

    \b
    Filter to one night:
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml calib-metrics \\
            --night 20230519 \\
            --collection "<prefix>/runs/20230519/processCcd/*" \\
            -o calib_metrics_20230519.csv
    """
    from stips.collections import CollectionNames
    from stips.core import calib_metrics as cm_module

    if collection is None:
        prof = config.require_profile()
        collection = CollectionNames.science_glob(prof.collection_prefix)

    _print_info(f"Repo: {config.repo}")
    _print_info(f"Extracting calibration metrics from {collection}")
    if night:
        _print_info(f"Filtering to night={night}")

    result = cm_module.run(
        config=config,
        collection=collection,
        output=output,
        night=night,
        include_refcat_metrics=include_refcat_metrics,
    )

    _report_result(
        result,
        success_msg=f"\n✓ Wrote {result.n_rows} rows -> {result.csv_path}",
        fail_msg="calib-metrics failed",
    )


# =============================================================================
# landolt-validate - Validate photometric calibration against Landolt standards
# =============================================================================


@cli.command("landolt-validate")
@click.option(
    "--catalog",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Path to landolt_catalog.csv with published magnitudes",
)
@click.option(
    "--collection",
    default=None,
    show_default=True,
    help="Butler collection glob to query "
    "(defaults to <profile prefix>/runs/*/processCcd/*)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Output CSV path",
)
@click.option(
    "--list-stars",
    is_flag=True,
    default=False,
    help="Dry run: list matched stars per visit without extracting photometry",
)
@pass_config
def landolt_validate(
    ctx: click.Context,
    config: cfg_module.Config,
    catalog: Path,
    collection: str | None,
    output: Path,
    list_stars: bool,
) -> None:
    """Validate photometric calibration against Landolt standard stars.

    Cross-matches pipeline source catalogs against known Landolt star positions,
    converts to the Vega system, and compares to published Landolt magnitudes.

    \b
    Example:
        stips -c scripts/config/landolt_validation/pipeline_landolt.yaml landolt-validate \\
            --catalog scripts/config/landolt_validation/landolt_catalog.csv \\
            -o landolt_validation.csv

    \b
    Dry run (list expected matches):
        stips -c scripts/config/landolt_validation/pipeline_landolt.yaml landolt-validate \\
            --catalog scripts/config/landolt_validation/landolt_catalog.csv \\
            --list-stars \\
            -o /dev/null
    """
    from stips.core import landolt as landolt_module

    _print_info(f"Repo: {config.repo}")
    mode = "listing stars" if list_stars else "validating photometry"
    _print_info(f"Landolt validation: {mode}")

    result = landolt_module.run(
        config=config,
        catalog=catalog,
        output=output,
        collection=collection,
        list_stars=list_stars,
    )

    success_msg = (
        "\n✓ Star listing complete"
        if list_stars
        else f"\n✓ {result.n_measurements} measurements -> {result.csv_path}"
    )
    _report_result(
        result,
        success_msg=success_msg,
        fail_msg="landolt-validate failed",
    )


# =============================================================================
# run - YAML-driven pipeline orchestrator
# =============================================================================


@cli.command("run")
@click.option("--dry-run", is_flag=True, help="Print commands without executing")
@click.option(
    "--site",
    type=click.Choice(["local", "slurm", "htcondor"]),
    default=None,
    help="Execution site (implies BPS execution)",
)
@click.option(
    "--concurrent",
    type=int,
    default=None,
    help="Max nights to process in parallel",
)
@pass_config
def run_pipeline(
    ctx: click.Context,
    config: cfg_module.Config,
    dry_run: bool,
    site: str | None,
    concurrent: int | None,
) -> None:
    """Run full pipeline from the group -c YAML configuration file.

    The -c config YAML specifies target, nights, bands, and options, and supplies
    environment configuration via its env: section.

    \b
    Example env: section (self-contained):
        env:
          REPO: "/path/to/repo"
          STACK_DIR: "/path/to/stack"
          INSTRUMENT_DIR: "/path/to/instruments/nickel"
          RAW_PARENT_DIR: "/path/to/raw"
        object: "2023ixf"
        ...

    \b
    Example:
        stips -c scripts/config/2023ixf/pipeline.yaml run
        stips -c pipeline.yaml run --dry-run
    """
    from stips.core import run as run_module

    config_file = ctx.obj["config_path"]

    _print_info(f"Running pipeline from {config_file}...")

    if dry_run:
        _print_info("[DRY RUN] Commands will be printed but not executed")

    result = run_module.run(
        config_file=config_file,
        config=config,
        dry_run=dry_run,
        site_override=site,
        concurrent_override=concurrent,
    )

    if result.success:
        _print_success("\n✓ Pipeline complete")
        if result.template_collections:
            click.echo("  Templates:")
            for band, coll in result.template_collections.items():
                click.echo(f"    {band}: {coll}")
        if result.lightcurve_path:
            click.echo(f"  Lightcurve: {result.lightcurve_path}")
        if result.log_dir:
            click.echo(f"  Logs: {result.log_dir}")
    else:
        _print_error(f"Pipeline failed: {result.error}")
        if result.failed_calibs:
            click.echo(f"  Failed calibs: {result.failed_calibs}")
        if result.failed_science:
            click.echo(f"  Failed science: {result.failed_science}")
        if result.failed_dia:
            click.echo(f"  Failed DIA: {result.failed_dia}")
        if result.log_dir:
            click.echo(f"  Logs: {result.log_dir}")
        sys.exit(1)


# =============================================================================
# refcat - On-demand Gaia/PS1 reference catalogs (no RSP/MONSTER)
# =============================================================================


@cli.group()
@click.pass_context
def refcat(ctx: click.Context) -> None:
    """Fetch and inspect Gaia DR3 + PS1 DR2 reference catalogs on demand.

    \b
    Example:
        stips -c config.yaml refcat fetch --ra 210.91 --dec 54.31
        stips -c config.yaml refcat status --ra 210.91 --dec 54.31
    """
    pass


@refcat.command("fetch")
@click.option("--ra", type=float, required=True, help="Target RA (degrees)")
@click.option("--dec", type=float, required=True, help="Target Dec (degrees)")
@click.option(
    "--radius", "radius_deg", type=float, default=0.3, help="Cone radius (deg)"
)
@click.option(
    "--mode",
    default="gaia_ps1",
    type=click.Choice(["gaia_ps1", "monster"]),
    help="Refcat mode (default: gaia_ps1)",
)
@click.option("--force", is_flag=True, help="Re-fetch even if coverage exists")
@pass_config
def refcat_fetch(
    ctx: click.Context,
    config: cfg_module.Config,
    ra: float,
    dec: float,
    radius_deg: float,
    mode: str,
    force: bool,
) -> None:
    """Ensure Gaia/PS1 refcats cover a target cone (fetch+convert+ingest)."""
    from stips.core import refcat as refcat_mod

    result = refcat_mod.ensure_refcats(
        config, ra, dec, radius_deg=radius_deg, mode=mode, force=force
    )
    click.echo(
        f"Refcat ({result.mode}): gaia={result.gaia_status}, "
        f"ps1={result.ps1_status}, trixels={result.needed_trixels}"
    )
    if result.collections:
        click.echo("Ingested: " + ", ".join(result.collections))
    if result.error:
        click.echo(f"Issues: {result.error}")


@refcat.command("status")
@click.option("--ra", type=float, required=True, help="Target RA (degrees)")
@click.option("--dec", type=float, required=True, help="Target Dec (degrees)")
@click.option(
    "--radius", "radius_deg", type=float, default=0.3, help="Cone radius (deg)"
)
@pass_config
def refcat_status(
    ctx: click.Context,
    config: cfg_module.Config,
    ra: float,
    dec: float,
    radius_deg: float,
) -> None:
    """Report Gaia/PS1 coverage for a target cone without fetching."""
    from stips.core import refcat as refcat_mod

    needed = set(refcat_mod.cones_to_htm([(ra, dec, radius_deg)], depth=7))
    for name in (refcat_mod.GAIA_DATASET, refcat_mod.PS1_DATASET):
        present = refcat_mod.present_trixels(config, name)
        have = len(needed & present)
        click.echo(f"{name}: {have}/{len(needed)} trixels present")


# =============================================================================
# bps - Batch Processing Service integration
# =============================================================================


@cli.group()
@click.pass_context
def bps(ctx: click.Context) -> None:
    """Submit and manage BPS workflows on HPC clusters.

    BPS (Batch Processing Service) enables large-scale parallel processing
    on Slurm, HTCondor, or local Parsl executors.

    \b
    Available sites:
        slurm     - Slurm clusters via Parsl
        htcondor  - HTCondor pools
        local     - Local machine (for testing)

    \b
    Example:
        stips bps submit calibs 20230519 --site slurm
        stips bps submit dia 20230519 --site slurm --band r
        stips bps status RUN_ID
        stips bps cancel RUN_ID
    """
    pass


@bps.command("submit")
@click.argument("pipeline", type=click.Choice(["calibs", "science", "dia", "fphot"]))
@click.argument("night")
@click.option(
    "--site",
    default="slurm",
    type=click.Choice(["slurm", "htcondor", "local"]),
    help="Compute site (default: slurm)",
)
@click.option("-b", "--band", help="Band for DIA pipeline (required for dia)")
@click.option("-t", "--template", help="Template collection for DIA")
@click.option("--object", "object_filter", help="Filter by OBJECT header")
@click.option("--coords", help="Coordinate collection for forced photometry")
@click.option(
    "--project",
    default=None,
    help="HPC project/account (default: the active instrument profile's name)",
)
@click.option("--dry-run", is_flag=True, help="Show what would be submitted")
@pass_config
def bps_submit(
    ctx: click.Context,
    config: cfg_module.Config,
    pipeline: str,
    night: str,
    site: str,
    band: str | None,
    template: str | None,
    object_filter: str | None,
    coords: str | None,
    project: str | None,
    dry_run: bool,
) -> None:
    """Submit a pipeline to BPS for parallel execution.

    PIPELINE is one of: calibs, science, dia, fphot
    NIGHT is the observing date (YYYYMMDD)

    \b
    Example:
        stips bps submit calibs 20230519 --site slurm
        stips bps submit science 20230519 --site local --dry-run
        stips bps submit dia 20230519 --site slurm --band r
    """
    # Validate DIA requires band
    if pipeline == "dia" and not band:
        _print_error("DIA pipeline requires --band option")
        sys.exit(1)

    from stips.core import bps as bps_module

    bps_cfg = bps_module.BPSConfig(
        pipeline=pipeline,
        night=night,
        site=site,
        band=band,
        template_collection=template,
        object_filter=object_filter,
        coord_collection=coords,
        project=project,
        dry_run=dry_run,
    )

    mode_str = "[DRY RUN] " if dry_run else ""
    _print_info(f"{mode_str}Submitting {pipeline} pipeline for {night} to {site}...")

    result = bps_module.submit(bps_cfg, config)

    if result.success:
        if dry_run:
            _print_success(f"[DRY RUN] Would submit to {site}")
            if result.config_file:
                click.echo(f"  Config file: {result.config_file}")
        else:
            _print_success("BPS workflow submitted")
        if result.submit_dir:
            click.echo(f"  Submit dir: {result.submit_dir}")
        if result.run_id:
            click.echo(f"  Run ID: {result.run_id}")
            click.echo(f"\n  Check status: stips bps status {result.run_id}")
    else:
        _print_error(f"BPS submission failed: {result.error}")
        if result.stderr:
            click.echo(f"\nStderr:\n{result.stderr}")
        sys.exit(1)


@bps.command("status")
@click.argument("run_id")
@pass_config
def bps_status(ctx: click.Context, config: cfg_module.Config, run_id: str) -> None:
    """Check status of a BPS run.

    RUN_ID is the identifier returned by bps submit.

    \b
    Example:
        stips bps status 12345
    """
    from stips.core import bps as bps_module

    _print_info(f"Checking status of run {run_id}...")

    result = bps_module.status(run_id, config)

    if result.get("success"):
        click.echo(result.get("output", "No output"))
    else:
        _print_error(f"Failed to get status: {result.get('error', 'Unknown error')}")
        sys.exit(1)


@bps.command("cancel")
@click.argument("run_id")
@click.option("--force", is_flag=True, help="Force cancellation")
@pass_config
def bps_cancel(
    ctx: click.Context, config: cfg_module.Config, run_id: str, force: bool
) -> None:
    """Cancel a running BPS workflow.

    RUN_ID is the identifier returned by bps submit.

    \b
    Example:
        stips bps cancel 12345
    """
    from stips.core import bps as bps_module

    if not force:
        if not click.confirm(f"Cancel run {run_id}?"):
            click.echo("Cancelled")
            return

    _print_info(f"Cancelling run {run_id}...")

    if bps_module.cancel(run_id, config):
        _print_success(f"Run {run_id} cancelled")
    else:
        _print_error(f"Failed to cancel run {run_id}")
        sys.exit(1)


@bps.command("list")
@pass_config
def bps_list(ctx: click.Context, config: cfg_module.Config) -> None:
    """List recent BPS runs.

    \b
    Example:
        stips bps list
    """
    from stips.core import bps as bps_module

    runs = bps_module.list_runs(config)

    if not runs:
        click.echo("No BPS runs found")
        return

    click.echo("Recent BPS runs:")
    for run in runs:
        click.echo(f"  {run.get('raw', run)}")


# =============================================================================
# dashboard - Pipeline monitoring dashboard
# =============================================================================


@cli.command("dashboard")
@click.option("--port", default=8787, help="Server port (default: 8787)")
@click.option("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
@click.option(
    "--logs-dir",
    type=click.Path(exists=True, path_type=Path),
    help="Logs directory to monitor (default: auto-detect from config)",
)
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically")
@click.pass_context
def dashboard(
    ctx: click.Context,
    port: int,
    host: str,
    logs_dir: Path | None,
    no_browser: bool,
) -> None:
    """Launch the pipeline monitoring dashboard.

    Opens a browser-based dashboard showing live pipeline progress,
    per-night status grids, and log tailing.

    \b
    Example:
        stips dashboard
        stips dashboard --port 9000
        stips dashboard --logs-dir ./logs --no-browser
        stips -c scripts/config/2023ixf/pipeline_ps1_template.yaml dashboard
    """
    # A missing/invalid config is a soft condition here: _try_load_config returns
    # None instead of raising SystemExit (never catch SystemExit — that would let
    # an already-reported fatal error keep going, F-016).
    instrument_name = "STIPS"
    config = _try_load_config(ctx)

    if config is not None:
        try:
            instrument_name = config.require_profile().name
        except RuntimeError:
            instrument_name = "STIPS"

    if logs_dir is None:
        if config is not None:
            # Pure Path arithmetic (no I/O) — cannot raise.
            logs_dir = config.instrument_dir.parent.parent / "logs"
        else:
            # Fallback: look for logs/ in current directory
            logs_dir = Path.cwd() / "logs"

    if not logs_dir.is_dir():
        _print_error(f"Logs directory not found: {logs_dir}")
        _print_info("Use --logs-dir to specify the logs directory")
        sys.exit(1)

    _print_info("Starting STIPS Dashboard...")
    _print_info(f"  Logs: {logs_dir}")
    _print_info(f"  URL:  http://{host}:{port}")

    # Open browser
    if not no_browser:
        import threading
        import webbrowser

        def _open_browser() -> None:
            import time

            time.sleep(1.5)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open_browser, daemon=True).start()

    # Import and run (dashboard deps are an optional extra)
    try:
        import uvicorn

        from stips.dashboard import create_app
    except ModuleNotFoundError as exc:
        raise click.ClickException(
            f"Dashboard support is not installed ({exc.name}). "
            "Install it with: pip install 'stips[dashboard]'"
        ) from exc

    # Thread the launch Config through so all dashboard Butler queries run
    # inside the activated LSST stack this config selects (F-023). Without a
    # config, Butler-backed panels report unavailable; log browsing still works.
    app = create_app(logs_dir, instrument_name=instrument_name, config=config)
    uvicorn.run(app, host=host, port=port, log_level="warning")


# =============================================================================
# provenance - Aggregate and maintain the run-provenance document
# =============================================================================


@cli.group()
def provenance():
    """Aggregate and maintain the run-provenance document."""


@provenance.command("sync")
@click.option(
    "--roots",
    multiple=True,
    type=click.Path(path_type=Path),
    help="Repo root dir(s). Repeatable. Defaults to known data roots.",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Output dir for runs.json + RUNS.md (default: <repo>/provenance).",
)
@click.option(
    "--repo-root",
    type=click.Path(path_type=Path),
    default=None,
    help="stips repo root for git-sha lookup (default: inferred).",
)
@click.option("--dry-run", is_flag=True, help="Report without writing.")
@click.pass_context
def provenance_sync(ctx, roots, out_dir, repo_root, dry_run):
    from stips.core import provenance as prov

    repo_root = repo_root or Path(__file__).resolve().parents[4]
    out_dir = out_dir or (repo_root / "provenance")
    # Fall back to config-derived roots when no --roots given: best-effort load
    # of the group-level -c/--config (optional here) so default_roots can use
    # config.repo.parent. STIPS_DATA_ROOTS still takes precedence inside it.
    config = None
    if not roots and ctx.obj.get("config_path"):
        try:
            config = cfg_module.load(ctx.obj["config_path"])
        except ValueError:
            config = None
    roots = list(roots) or prov.default_roots(config)
    if not roots:
        click.echo(
            "No data roots to scan. Set STIPS_DATA_ROOTS, pass --roots, or "
            "provide -c <config.yaml> so the repo's parent dir can be used."
        )
    summary = prov.sync(
        roots=roots, out_dir=out_dir, repo_root=repo_root, dry_run=dry_run
    )
    click.echo(
        f"records: {summary['records']}  repos: {len(summary['repos'])}  "
        f"total_after: {summary['total_records_after']}"
    )
    if summary["empty_or_unparseable"]:
        click.echo(
            "NEEDS REVIEW (no/!parseable processing_log): "
            + ", ".join(summary["empty_or_unparseable"])
        )


@provenance.command("mark-deleted")
@click.argument("repos", nargs=-1, required=True)
@click.option("--out-dir", type=click.Path(path_type=Path), default=None)
def provenance_mark_deleted(repos, out_dir):
    from datetime import datetime, timezone

    from stips.core import provenance as prov

    repo_root = Path(__file__).resolve().parents[4]
    out_dir = out_dir or (repo_root / "provenance")
    n = prov.mark_deleted(
        list(repos), out_dir, datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )
    click.echo(f"marked {n} record(s) deleted")


def main() -> None:
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
