"""Pan-STARRS1 external-template adapter."""

from __future__ import annotations


class PS1Source:
    name = "ps1"
    max_cutout_deg = None
    zeropoint_keywords = ["ZPT", "FPA.ZP", "MAGZERO", "MAGZPT"]
