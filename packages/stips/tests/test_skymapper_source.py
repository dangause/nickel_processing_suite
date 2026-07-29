"""Tests for the external-template source registry and the SkyMapper adapter."""

from types import SimpleNamespace

import pytest
from stips.core.pipeline import template_band_map
from stips.pipeline_tools.external_template.sources import (
    SOURCES,
    TemplateSourceError,
    get_source,
)


def test_registry_exposes_ps1_and_skymapper():
    assert set(SOURCES) == {"ps1", "skymapper"}


def test_get_source_returns_adapter():
    assert get_source("skymapper").name == "skymapper"


def test_get_source_rejects_unknown_and_lists_valid():
    with pytest.raises(TemplateSourceError) as excinfo:
        get_source("panstarrs2")
    message = str(excinfo.value)
    assert "panstarrs2" in message
    assert "ps1" in message and "skymapper" in message


def test_skymapper_declares_verified_service_limit():
    """0.17 deg is a hard SIA limit: above it the service returns HTTP 400."""
    assert get_source("skymapper").max_cutout_deg == 0.17


def test_ps1_declares_no_service_limit():
    assert get_source("ps1").max_cutout_deg is None


# --- template_band_maps resolution -------------------------------------------


def _config(*, ps1_band_map=None, template_band_maps=None):
    return SimpleNamespace(
        profile=SimpleNamespace(
            ps1_band_map=ps1_band_map or {},
            template_band_maps=template_band_maps or {},
        )
    )


def test_template_band_map_reads_named_source():
    cfg = _config(template_band_maps={"skymapper": {"r": "r", "i": "i"}})
    assert template_band_map(cfg, "skymapper") == {"r": "r", "i": "i"}


def test_template_band_map_ps1_falls_back_to_ps1_band_map():
    """Existing profiles set only ps1_band_map; PS1 must keep working."""
    cfg = _config(ps1_band_map={"r": "r", "i": "i"})
    assert template_band_map(cfg, "ps1") == {"r": "r", "i": "i"}


def test_template_band_map_explicit_ps1_entry_wins_over_fallback():
    cfg = _config(
        ps1_band_map={"r": "r"},
        template_band_maps={"ps1": {"r": "r", "i": "i"}},
    )
    assert template_band_map(cfg, "ps1") == {"r": "r", "i": "i"}


def test_template_band_map_unknown_source_is_empty():
    """Empty means 'this instrument has no templates from that survey'."""
    assert template_band_map(_config(), "skymapper") == {}


def test_ctio1m_profile_excludes_skymapper_v_band():
    """SkyMapper v is a 384nm violet filter, NOT Johnson V (551nm)."""
    # NOTE: this loader lives in lsst.obs.stips, NOT stips.core.config.
    # lsst.obs.stips is an editable install and IS importable in the plain venv
    # (only lsst.afw / lsst.daf.butler are absent). See test_config_yaml.py:248
    # for the same import in an existing passing test.
    from lsst.obs.stips.profile_loader import load_profile_from_dir

    prof = load_profile_from_dir("instruments/ctio1m")
    sm = prof.template_band_maps.get("skymapper", {})
    assert sm == {"r": "r", "i": "i"}
    assert "v" not in sm


def test_adapters_do_not_import_lsst():
    """Adapters must stay importable in a plain venv (no lsst dependency).

    Static scan rather than a sys.modules check, for two reasons:
      * `lsst.obs.stips` IS importable in this venv (it is an editable install);
        only the heavy stack pieces (lsst.afw, lsst.daf.butler) are absent. So a
        sys.modules assertion is not the clean signal it looks like.
      * Mutating sys.modules to isolate the check would poison later tests in the
        same session.
    A source scan also catches FUNCTION-SCOPED lsst imports, which an
    import-and-inspect check never exercises.
    """
    import ast
    import pathlib

    import stips.pipeline_tools.external_template.sources as pkg

    offenders = []
    for path in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "lsst" or name.startswith("lsst."):
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, "adapters must not import lsst: " + "; ".join(offenders)


# --- SkyMapper SIA parsing / frame selection ---------------------------------

from stips.pipeline_tools.external_template.sources import skymapper as sm  # noqa: E402
from stips.pipeline_tools.external_template.sources.skymapper import (  # noqa: E402
    DEFAULT_ZEROPOINTS,
)

SIA_CSV = """image_name,band,exptime,mjd_obs,image_type,mean_fwhm,zpapprox,unique_image_id,get_fits
a,r,5.0,56972.69752315,short,2.97565,25.38060,20141111164425-17,http://x/a
b,r,5.0,56987.63652778,short,2.85622,25.44530,20141126151635-17,http://x/b
c,r,5.0,57050.54799769,short,4.51323,25.38400,20150128130907-17,http://x/c
d,r,100.0,58759.69835648,main,2.33374,28.78460,20191003164537-17,http://x/d
e,r,100.0,58928.40702546,main,1.76150,28.73250,20200320094604-17,http://x/e
"""


def test_parse_sia_csv_returns_typed_rows():
    rows = sm.parse_sia_csv(SIA_CSV)
    assert len(rows) == 5
    assert rows[0]["image_type"] == "short"
    assert rows[3]["exptime"] == pytest.approx(100.0)
    assert rows[4]["mean_fwhm"] == pytest.approx(1.76150)


def test_select_frame_ignores_short_exposures():
    """5s 'short' frames are never templates, even when their seeing is best."""
    chosen = sm.select_frame(sm.parse_sia_csv(SIA_CSV), band="r")
    assert chosen["image_type"] == "main"


def test_select_frame_picks_best_seeing_among_main():
    chosen = sm.select_frame(sm.parse_sia_csv(SIA_CSV), band="r")
    assert chosen["unique_image_id"] == "20200320094604-17"
    assert chosen["mean_fwhm"] == pytest.approx(1.76150)


def test_select_frame_honours_mjd_window():
    """MJD windowing exists so template epochs can exclude the transient."""
    chosen = sm.select_frame(sm.parse_sia_csv(SIA_CSV), band="r", mjd_end=58800.0)
    assert chosen["unique_image_id"] == "20191003164537-17"


def test_select_frame_raises_when_no_main_frames_and_counts_shorts():
    rows = [r for r in sm.parse_sia_csv(SIA_CSV) if r["image_type"] == "short"]
    with pytest.raises(sm.TemplateSourceError) as excinfo:
        sm.select_frame(rows, band="r")
    message = str(excinfo.value)
    assert "3" in message  # reports how many short frames were found
    assert "coadd" in message.lower()  # points at the working alternative


def test_select_frame_raises_when_mjd_window_excludes_everything():
    with pytest.raises(sm.TemplateSourceError, match="MJD"):
        sm.select_frame(sm.parse_sia_csv(SIA_CSV), band="r", mjd_start=60000.0)


def test_select_frame_filters_by_band():
    rows = sm.parse_sia_csv(SIA_CSV)
    for row in rows:
        row["band"] = "i"
    with pytest.raises(sm.TemplateSourceError, match="band"):
        sm.select_frame(rows, band="r")


def test_native_fwhm_reads_qafwhm_header_card():
    assert sm.SkyMapperSource().native_fwhm({"QAFWHM": 1.7615}) == pytest.approx(1.7615)


def test_native_fwhm_falls_back_when_card_missing():
    """A sane default, not a crash — the frame is still usable."""
    assert sm.SkyMapperSource().native_fwhm({}) == pytest.approx(2.0)


def test_default_zeropoint_differs_for_main_and_short():
    src = sm.SkyMapperSource()
    assert src.default_zeropoint({"EXPTIME": 100.0}) == pytest.approx(28.75)
    assert src.default_zeropoint({"EXPTIME": 5.0}) == pytest.approx(25.4)


def test_skymapper_band_map_reads_profile():
    cfg = _config(template_band_maps={"skymapper": {"r": "r", "i": "i"}})
    assert sm.SkyMapperSource().band_map(cfg) == {"r": "r", "i": "i"}


# --- Review findings ----------------------------------------------------------


def test_default_zeropoint_handles_non_numeric_exptime(caplog):
    """A malformed EXPTIME must not crash; fall back to the conservative 'short'."""
    src = sm.SkyMapperSource()
    with caplog.at_level("WARNING"):
        result = src.default_zeropoint({"EXPTIME": "bad"})
    assert result == pytest.approx(DEFAULT_ZEROPOINTS["short"])
    assert "bad" in caplog.text


def test_default_zeropoint_handles_missing_exptime():
    """Missing EXPTIME already falls back to 0.0 -> 'short'; pin the behavior."""
    src = sm.SkyMapperSource()
    assert src.default_zeropoint({}) == pytest.approx(DEFAULT_ZEROPOINTS["short"])


def test_select_frame_excludes_undated_row_from_mjd_window():
    """A missing mjd_obs must be excluded when a window is requested, not

    silently included via a 0.0 fallback that happens to satisfy one bound.
    """
    rows = [
        {
            "band": "r",
            "image_type": "main",
            "exptime": 100.0,
            "mjd_obs": None,
            "mean_fwhm": 1.0,
            "unique_image_id": "undated",
        },
        {
            "band": "r",
            "image_type": "main",
            "exptime": 100.0,
            "mjd_obs": 10.0,
            "mean_fwhm": 2.0,
            "unique_image_id": "dated",
        },
    ]
    chosen = sm.select_frame(rows, band="r", mjd_end=50.0)
    assert chosen["unique_image_id"] == "dated"


def test_select_frame_no_window_still_includes_undated_rows():
    """When no MJD window is requested, undated rows remain eligible."""
    rows = [
        {
            "band": "r",
            "image_type": "main",
            "exptime": 100.0,
            "mjd_obs": None,
            "mean_fwhm": 1.0,
            "unique_image_id": "undated",
        },
        {
            "band": "r",
            "image_type": "main",
            "exptime": 100.0,
            "mjd_obs": 100.0,
            "mean_fwhm": 2.0,
            "unique_image_id": "dated",
        },
    ]
    chosen = sm.select_frame(rows, band="r")
    assert chosen["unique_image_id"] == "undated"


def test_select_frame_logs_warning_when_seeing_is_unknown(caplog):
    """All-None seeing picks are arbitrary; the log must say so."""
    rows = [
        {
            "band": "r",
            "image_type": "main",
            "exptime": 100.0,
            "mjd_obs": 100.0,
            "mean_fwhm": None,
            "unique_image_id": "a",
        },
        {
            "band": "r",
            "image_type": "main",
            "exptime": 100.0,
            "mjd_obs": 101.0,
            "mean_fwhm": None,
            "unique_image_id": "b",
        },
    ]
    with caplog.at_level("WARNING"):
        sm.select_frame(rows, band="r")
    assert "fwhm" in caplog.text.lower() or "seeing" in caplog.text.lower()


def test_select_frame_no_main_message_counts_short_specifically():
    """The count/label must specifically describe 'short' frames, not just

    'everything that isn't main' — future-proof against a third image_type.
    """
    rows = [
        {"band": "r", "image_type": "short", "exptime": 5.0},
        {"band": "r", "image_type": "short", "exptime": 5.0},
        {"band": "r", "image_type": "weird", "exptime": 30.0},
    ]
    with pytest.raises(sm.TemplateSourceError) as excinfo:
        sm.select_frame(rows, band="r")
    message = str(excinfo.value)
    assert "2 'short'" in message
    assert "weird" in message.lower() or "1 other" in message.lower()


# --- SkyMapper fetch ----------------------------------------------------------

import logging as _logging  # noqa: E402


class _FakeResponse:
    def __init__(self, *, status_code=200, text="", content=b""):
        self.status_code = status_code
        self.text = text
        self.content = content


class _FakeSession:
    """Records requests and replays canned responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return self._responses.pop(0)


def _ok_session(fits_bytes=b"SIMPLE  =                    T" + b" " * 20000):
    return _FakeSession(
        [
            _FakeResponse(text=SIA_CSV),
            _FakeResponse(content=fits_bytes),
        ]
    )


def test_fetch_writes_fits_and_returns_path(tmp_path):
    session = _ok_session()
    path = sm.SkyMapperSource().fetch(
        102.2475, -36.0053, "r", 0.15, tmp_path, session=session
    )
    assert path.exists()
    assert path.read_bytes().startswith(b"SIMPLE")


def test_fetch_requests_the_selected_frame(tmp_path):
    session = _ok_session()
    sm.SkyMapperSource().fetch(102.2475, -36.0053, "r", 0.15, tmp_path, session=session)
    _, image_params, _ = session.calls[1]
    assert image_params["image"] == "20200320094604-17"
    assert image_params["format"] == "fits"


def test_fetch_clamps_sia_query_size_to_service_limit(tmp_path, caplog):
    """0.4 deg is what the 2023ixf configs use; SkyMapper caps at 0.17.

    The cap is per REQUEST, so a 0.4 deg ask is now served by mosaicking
    several 0.17 deg tiles — but the SIA *query* box itself still has to obey
    the same 0.17 deg limit or the service answers HTTP 400.
    """
    session = _mosaic_session()
    with caplog.at_level(_logging.WARNING):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.4, tmp_path, session=session
        )
    query_params = session.calls[0][1]
    assert query_params["SIZE"] == "0.17"
    assert "0.17" in caplog.text


def test_fetch_warns_when_cutout_is_smaller_than_fov(tmp_path, caplog):
    """Y4KCam is ~20'; a 10.2' template leaves dithers with no kernel candidates."""
    session = _ok_session()
    with caplog.at_level(_logging.WARNING):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.17, tmp_path, session=session, fov_arcmin=20.0
        )
    assert "20.0" in caplog.text
    assert "kernel" in caplog.text.lower()


def test_fetch_raises_on_query_http_error(tmp_path):
    session = _FakeSession([_FakeResponse(status_code=400, text="bad")])
    with pytest.raises(sm.TemplateSourceError, match="400"):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.15, tmp_path, session=session
        )


def test_fetch_raises_on_truncated_image(tmp_path):
    session = _FakeSession(
        [_FakeResponse(text=SIA_CSV), _FakeResponse(content=b"tiny")]
    )
    with pytest.raises(sm.TemplateSourceError, match="too small"):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.15, tmp_path, session=session
        )


def test_fetch_propagates_no_main_frames_error(tmp_path):
    short_only = "\n".join([SIA_CSV.splitlines()[0]] + SIA_CSV.splitlines()[1:4])
    session = _FakeSession([_FakeResponse(text=short_only)])
    with pytest.raises(sm.TemplateSourceError, match="coadd"):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.15, tmp_path, session=session
        )


def test_fetch_passes_nondefault_timeout_to_both_requests(tmp_path):
    """A dropped `timeout=` kwarg in `fetch` must not pass silently.

    `_FakeSession.get` previously accepted `timeout=` but never recorded it,
    so removing `timeout=` from `fetch` entirely would still pass every
    existing test. Record the timeout alongside each call and assert both
    the SIA query and the image download were issued with the non-default
    timeout passed to `fetch`.
    """
    session = _ok_session()
    sm.SkyMapperSource().fetch(
        102.2475, -36.0053, "r", 0.15, tmp_path, session=session, timeout=42
    )
    assert len(session.calls) == 2
    for _, _, timeout in session.calls:
        assert timeout == 42


def test_fetch_raises_template_source_error_on_malformed_row(tmp_path):
    """A row with no unique_image_id must not escape as a bare KeyError:
    ingest.py catches TemplateSourceError only."""
    csv_without_id = SIA_CSV.replace("unique_image_id", "not_the_id_column")
    session = _FakeSession([_FakeResponse(text=csv_without_id)])
    with pytest.raises(sm.TemplateSourceError, match="unique_image_id"):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.15, tmp_path, session=session
        )


# --- Tile mosaicking ----------------------------------------------------------
#
# The 0.17 deg cap is per REQUEST, not per frame: several offset requests against
# the SAME `image=` id return sub-arrays of one CCD pixel grid, sharing CRVAL and
# the CD matrix and differing only in CRPIX. Verified 2026-07-28 against the live
# DR4 SIA (a 3x3 grid returned nine tiles spanning 17.0' x 25.5'). That makes the
# mosaic an exact integer paste, with no reprojection and no interpolation.

import io as _io  # noqa: E402
import math as _math  # noqa: E402

import numpy as _np  # noqa: E402
from astropy.io import fits as _fits  # noqa: E402

#: Real CD matrix from SkyMapper frame 20160417083802-17 (0.4976"/px).
_CD = (
    -1.382355665880e-04,
    -5.948597234549e-07,
    -5.772348044212e-07,
    1.382173542227e-04,
)
_CRVAL = (102.19421, -35.91763)


def _tile_header(*, crpix1, crpix2, crval=_CRVAL, cd=_CD):
    header = _fits.Header()
    header["CTYPE1"] = "RA---TPV"
    header["CTYPE2"] = "DEC--TPV"
    header["CRVAL1"], header["CRVAL2"] = crval
    header["CRPIX1"] = float(crpix1)
    header["CRPIX2"] = float(crpix2)
    header["CD1_1"], header["CD1_2"], header["CD2_1"], header["CD2_2"] = cd
    header["PV1_1"] = 1.0
    header["PV1_7"] = -9.541002961438e-04
    header["PV2_9"] = -1.049513876307e-03
    header["EXPTIME"] = 100.0
    header["QAFWHM"] = 1.68189
    header["ZPAPPROX"] = 27.9474132957843
    return header


def _tile(*, crpix1, crpix2, nx=64, ny=64, value=1.0, crval=_CRVAL, cd=_CD):
    """A (header, data) tile as ``fetch`` reads it off the wire."""
    header = _tile_header(crpix1=crpix1, crpix2=crpix2, crval=crval, cd=cd)
    return header, _np.full((ny, nx), value, dtype=_np.float32)


def _tile_bytes(**kwargs):
    header, data = _tile(**kwargs)
    buffer = _io.BytesIO()
    _fits.PrimaryHDU(data=data, header=header).writeto(buffer)
    return buffer.getvalue()


def _mosaic_session(n=3, nx=64, ny=64):
    """A session serving the SIA CSV then ``n*n`` real single-frame tiles.

    CRPIX steps by the tile size so the pasted grid is contiguous, mimicking
    the live service (larger CRPIX == closer to the CCD origin, because CD1_1
    is negative and the cutout origin enters CRPIX with a minus sign).
    """
    responses = [_FakeResponse(text=SIA_CSV)]
    for row in range(n):
        for col in range(n):
            responses.append(
                _FakeResponse(
                    content=_tile_bytes(
                        crpix1=1024.0 - col * nx,
                        crpix2=2048.0 - row * ny,
                        nx=nx,
                        ny=ny,
                        value=float(row * n + col),
                    )
                )
            )
    return _FakeSession(responses)


# --- grid geometry ---


def test_tile_centers_is_a_single_center_at_or_below_the_cap():
    assert sm.tile_centers(102.0, -36.0, 0.17, 0.17) == [(102.0, -36.0)]
    assert sm.tile_centers(102.0, -36.0, 0.10, 0.17) == [(102.0, -36.0)]


def test_tile_centers_uses_the_smallest_grid_that_covers_the_request():
    """0.4 deg at a 0.17 deg cap needs 3x3 with the 0.85 step — not 4x4."""
    centers = sm.tile_centers(102.0, -36.0, 0.4, 0.17)
    assert len(centers) == 9


def test_tile_centers_span_at_least_the_requested_size():
    size, tile = 0.4, 0.17
    centers = sm.tile_centers(102.0, -36.0, size, tile)
    decs = sorted({d for _, d in centers})
    assert (decs[-1] - decs[0]) + tile >= size - 1e-9


def test_tile_centers_are_symmetric_about_the_target():
    centers = sm.tile_centers(102.0, -36.0, 0.4, 0.17)
    assert (102.0, -36.0) in [
        (round(r, 9), round(d, 9)) for r, d in centers
    ], "the requested position must itself be a tile center"
    ras = sorted({r for r, _ in centers})
    assert ras[0] + ras[-1] == pytest.approx(2 * 102.0)


def test_tile_centers_scale_ra_step_by_cos_dec():
    """RA degrees shrink with declination; a plain RA step would leave seams."""
    centers = sm.tile_centers(102.0, -60.0, 0.4, 0.17)
    ras = sorted({r for r, _ in centers})
    decs = sorted({d for _, d in centers})
    ra_step = ras[1] - ras[0]
    dec_step = decs[1] - decs[0]
    assert ra_step == pytest.approx(dec_step / _math.cos(_math.radians(-60.0)))


# --- assembly ---


def test_assemble_offsets_are_integer_crpix_differences():
    """Tile pixels land at (CRPIX_ref - CRPIX_tile) — an exact integer paste."""
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0, nx=10, ny=10, value=1.0),
        _tile(crpix1=1014.0, crpix2=2048.0, nx=10, ny=10, value=2.0),
        _tile(crpix1=1024.0, crpix2=2038.0, nx=10, ny=10, value=3.0),
        _tile(crpix1=1014.0, crpix2=2038.0, nx=10, ny=10, value=4.0),
    ]
    header, data = sm.assemble_tiles(tiles)
    assert data.shape == (20, 20)
    assert data[0, 0] == 1.0  # ref tile at the origin
    assert data[0, 10] == 2.0  # smaller CRPIX1 -> +10 in x
    assert data[10, 0] == 3.0  # smaller CRPIX2 -> +10 in y
    assert data[10, 10] == 4.0
    assert header["NAXIS1"] == 20 and header["NAXIS2"] == 20


def test_assemble_shifts_crpix_to_the_assembled_origin():
    """CRPIX_out is the max CRPIX, which is what keeps every tile's WCS valid."""
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0, nx=10, ny=10),
        _tile(crpix1=1014.0, crpix2=2038.0, nx=10, ny=10),
    ]
    header, _ = sm.assemble_tiles(tiles)
    assert header["CRPIX1"] == pytest.approx(1024.0)
    assert header["CRPIX2"] == pytest.approx(2048.0)


def test_assemble_preserves_photometry_distortion_and_projection():
    """Downstream ingest reads ZPAPPROX/QAFWHM; the TPV solution needs every PV."""
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0, nx=10, ny=10),
        _tile(crpix1=1014.0, crpix2=2048.0, nx=10, ny=10),
    ]
    header, _ = sm.assemble_tiles(tiles)
    assert header["ZPAPPROX"] == pytest.approx(27.9474132957843)
    assert header["QAFWHM"] == pytest.approx(1.68189)
    assert header["EXPTIME"] == pytest.approx(100.0)
    assert header["CTYPE1"] == "RA---TPV"
    assert header["CD1_1"] == pytest.approx(_CD[0])
    assert header["PV1_7"] == pytest.approx(-9.541002961438e-04)
    assert header["PV2_9"] == pytest.approx(-1.049513876307e-03)


def test_assemble_fills_uncovered_pixels_with_nan():
    """Gaps must be non-finite: the converter masks those as BAD/NO_DATA."""
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0, nx=10, ny=10, value=1.0),
        _tile(crpix1=1014.0, crpix2=2038.0, nx=10, ny=10, value=2.0),
    ]
    _, data = sm.assemble_tiles(tiles)
    assert data.shape == (20, 20)
    assert _np.isnan(data[0, 10])  # the two uncovered corners
    assert _np.isnan(data[10, 0])
    assert _np.isfinite(data[0, 0]) and _np.isfinite(data[10, 10])


def test_assemble_raises_when_tiles_disagree_on_crval():
    """Different frames -> pasting would silently misalign the sky."""
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0),
        _tile(crpix1=1014.0, crpix2=2048.0, crval=(102.5, -35.9)),
    ]
    with pytest.raises(sm.TemplateSourceError, match="CRVAL1"):
        sm.assemble_tiles(tiles)


def test_assemble_raises_when_tiles_disagree_on_cd_matrix():
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0),
        _tile(crpix1=1014.0, crpix2=2048.0, cd=(-1.4e-04,) + _CD[1:]),
    ]
    with pytest.raises(sm.TemplateSourceError, match="CD1_1"):
        sm.assemble_tiles(tiles)


def test_assemble_raises_on_a_sub_pixel_crpix_offset():
    """A fractional offset means the paste is not exact; refuse rather than blur."""
    tiles = [
        _tile(crpix1=1024.0, crpix2=2048.0),
        _tile(crpix1=1013.5, crpix2=2048.0),
    ]
    with pytest.raises(sm.TemplateSourceError, match="integer"):
        sm.assemble_tiles(tiles)


def test_assemble_raises_when_a_tile_has_no_crpix():
    header, data = _tile(crpix1=1024.0, crpix2=2048.0)
    del header["CRPIX2"]
    with pytest.raises(sm.TemplateSourceError, match="CRPIX"):
        sm.assemble_tiles([_tile(crpix1=1024.0, crpix2=2048.0), (header, data)])


# --- fetch: mosaic path ---


def test_fetch_mosaics_when_the_request_exceeds_the_per_request_cap(tmp_path):
    session = _mosaic_session()
    path = sm.SkyMapperSource().fetch(
        102.2475, -36.0053, "r", 0.4, tmp_path, session=session
    )
    assert len(session.calls) == 10  # one SIA query + nine tiles
    with _fits.open(path) as hdul:
        assert hdul[0].data.shape == (192, 192)


def test_fetch_mosaic_tiles_all_come_from_one_frame_at_the_cap(tmp_path):
    """Mixing frames would break the shared-grid, single-PSF, single-ZP premise."""
    session = _mosaic_session()
    sm.SkyMapperSource().fetch(102.2475, -36.0053, "r", 0.4, tmp_path, session=session)
    image_calls = [params for _, params, _ in session.calls[1:]]
    assert {p["image"] for p in image_calls} == {"20200320094604-17"}
    assert {p["size"] for p in image_calls} == {"0.17,0.17"}
    assert len({p["pos"] for p in image_calls}) == 9  # nine distinct centers


def test_fetch_single_request_below_the_cap_is_unchanged(tmp_path):
    """At or under the cap nothing is assembled: one query, one download."""
    session = _ok_session()
    path = sm.SkyMapperSource().fetch(
        102.2475, -36.0053, "r", 0.17, tmp_path, session=session
    )
    assert len(session.calls) == 2
    assert path.read_bytes() == _ok_session()._responses[1].content


def test_fetch_logs_tile_count_assembled_size_and_coverage(tmp_path, caplog):
    session = _mosaic_session()
    with caplog.at_level(_logging.INFO):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.4, tmp_path, session=session
        )
    assert "9 of 9" in caplog.text  # tile count
    assert "192 x 192 px" in caplog.text  # assembled size in pixels
    assert "1.6' x 1.6'" in caplog.text  # ... and in arcmin
    assert "of the requested area" in caplog.text


def test_fetch_says_so_when_the_ccd_edge_truncates_the_mosaic(tmp_path, caplog):
    """The CCD short axis is 17'; asking for 24' must not fail silently small."""
    session = _mosaic_session()
    with caplog.at_level(_logging.WARNING):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.4, tmp_path, session=session
        )
    assert "ccd" in caplog.text.lower()
    assert "smaller than requested" in caplog.text.lower()


def test_fetch_tolerates_a_single_failed_tile(tmp_path, caplog):
    """Tiles off the CCD edge legitimately fail; one bad tile is not fatal."""
    session = _mosaic_session()
    session._responses[5] = _FakeResponse(status_code=400, content=b"")
    with caplog.at_level(_logging.INFO):
        path = sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.4, tmp_path, session=session
        )
    assert path.exists()
    assert "unusable, skipping it" in caplog.text
    assert "8 of 9" in caplog.text


def test_fetch_raises_when_every_tile_fails(tmp_path):
    responses = [_FakeResponse(text=SIA_CSV)] + [
        _FakeResponse(status_code=400, content=b"") for _ in range(9)
    ]
    session = _FakeSession(responses)
    with pytest.raises(sm.TemplateSourceError, match="tile"):
        sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.4, tmp_path, session=session
        )


def test_fetch_mosaic_passes_the_timeout_to_every_tile_request(tmp_path):
    session = _mosaic_session()
    sm.SkyMapperSource().fetch(
        102.2475, -36.0053, "r", 0.4, tmp_path, session=session, timeout=42
    )
    assert len(session.calls) == 10
    for _, _, timeout in session.calls:
        assert timeout == 42


def test_skymapper_declares_the_ccd_limited_assembled_size():
    """2048 px (the CCD short axis) at 0.4976"/px = 17.0' = 0.283 deg."""
    source = sm.SkyMapperSource()
    assert source.max_cutout_deg == 0.17  # still the per-REQUEST cap
    assert source.max_assembled_deg == pytest.approx(0.283, abs=0.002)


def test_tile_centers_wrap_ra_into_the_valid_range():
    """A field near RA=0 must not ask the service for a negative coordinate."""
    centers = sm.tile_centers(0.05, -36.0, 0.4, 0.17)
    assert all(0.0 <= r < 360.0 for r, _ in centers)


def test_fetch_tolerates_a_transient_network_error_on_one_tile(tmp_path, caplog):
    """One flaky connection must not throw away the tiles that did arrive."""
    import requests as _requests

    class _FlakySession(_FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params or {}), timeout))
            response = self._responses.pop(0)
            if response is None:
                raise _requests.RequestException("connection reset")
            return response

    session = _FlakySession(_mosaic_session()._responses)
    session._responses[4] = None
    with caplog.at_level(_logging.INFO):
        path = sm.SkyMapperSource().fetch(
            102.2475, -36.0053, "r", 0.4, tmp_path, session=session
        )
    assert path.exists()
    assert "connection reset" in caplog.text
    assert "8 of 9" in caplog.text
