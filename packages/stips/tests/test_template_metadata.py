"""Source-agnosticism tests for template_metadata.py.

This module has no LSST dependency (stdlib only), so unlike
test_ps1_templates.py (gated behind ``pytest.importorskip("lsst.afw.image")``)
these run in a plain venv as well as in-stack.
"""

from stips.pipeline_tools.template_metadata import (
    EXTERNAL_DATE_SENTINELS,
    TemplateMetadata,
    _source_choices,
)


def test_external_sentinel_does_not_raise(tmp_path):
    """A source-agnostic "EXTERNAL" sentinel (used for non-PS1 external
    surveys, e.g. SkyMapper) must skip date validation exactly like "PS1"."""
    mgr = TemplateMetadata(str(tmp_path))
    mgr.record_template(
        collection="templates/skymapper/i",
        start_date="EXTERNAL",
        end_date="EXTERNAL",
        band="i",
        source="skymapper",
    )
    assert (
        mgr.metadata["templates"]["templates/skymapper/i"]["start_date"] == "EXTERNAL"
    )


def test_ps1_sentinel_still_works(tmp_path):
    """Back-compat: the historical literal "PS1" sentinel still skips
    validation (existing recorded metadata used this value)."""
    mgr = TemplateMetadata(str(tmp_path))
    mgr.record_template(
        collection="templates/ps1/r",
        start_date="PS1",
        end_date="PS1",
        band="r",
        source="ps1",
    )
    assert mgr.metadata["templates"]["templates/ps1/r"]["start_date"] == "PS1"


def test_genuine_bad_date_range_still_raises(tmp_path):
    mgr = TemplateMetadata(str(tmp_path))
    try:
        mgr.record_template(
            collection="templates/deep/r",
            start_date="20210201",
            end_date="20210101",  # end before start
            band="r",
            source="nickel",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for start_date > end_date")


def test_sentinels_are_a_set_containing_ps1_and_external():
    assert EXTERNAL_DATE_SENTINELS == {"PS1", "EXTERNAL"}


def test_skymapper_geometry_filed_under_its_own_source_key(tmp_path):
    """A SkyMapper template must not be recorded as if it were PS1: the
    geometry block is keyed by the actual source name."""
    mgr = TemplateMetadata(str(tmp_path))
    mgr.record_template(
        collection="templates/skymapper/i",
        start_date="EXTERNAL",
        end_date="EXTERNAL",
        band="i",
        source="skymapper",
        ps1_filter="i",
        ps1_ra=102.2,
        ps1_dec=-36.0,
        ps1_cutout_size=0.4,
    )
    meta = mgr.metadata["templates"]["templates/skymapper/i"]
    assert "ps1" not in meta
    assert meta["skymapper"]["filter"] == "i"
    assert meta["skymapper"]["ra"] == 102.2


def test_ps1_geometry_still_filed_under_ps1_key(tmp_path):
    """Back-compat: PS1 templates keep the "ps1" geometry key."""
    mgr = TemplateMetadata(str(tmp_path))
    mgr.record_template(
        collection="templates/ps1/r",
        start_date="PS1",
        end_date="PS1",
        band="r",
        source="ps1",
        ps1_filter="r",
        ps1_ra=210.9,
        ps1_dec=54.3,
        ps1_cutout_size=0.2,
    )
    meta = mgr.metadata["templates"]["templates/ps1/r"]
    assert meta["ps1"]["filter"] == "r"


def test_source_choices_include_registered_adapters():
    """--source choices come from the source registry (plus nickel/hybrid),
    so --source skymapper is accepted rather than being rejected as an
    unknown argparse choice."""
    choices = _source_choices()
    assert "nickel" in choices
    assert "hybrid" in choices
    assert "ps1" in choices
    assert "skymapper" in choices
