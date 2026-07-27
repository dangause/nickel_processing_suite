"""SkyMapper Southern Survey DR4 external-template adapter."""

from __future__ import annotations


class SkyMapperSource:
    name = "skymapper"
    #: Verified 2026-07-27: the DR4 SIA returns HTTP 400 above 0.17 deg.
    max_cutout_deg = 0.17
    zeropoint_keywords = ["ZPAPPROX"]
