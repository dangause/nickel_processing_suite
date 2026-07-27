# ruff: noqa: F821
# Gaia-DR3-ONLY refcat overlay: astrometry AND photometry from Gaia DR3.
#
# For fields SOUTH of PS1's -30deg limit — i.e. most of CTIO's sky. The
# refcats_gaia_ps1.py overlay takes photometry from PS1 (Dec > -30 only), so it
# cannot calibrate southern fields; this variant takes photometry from Gaia
# (BP/G/RP) transformed to B/V/R/I via the "gaia*" block in colorterms.py.
# Selected by refcat.mode == "gaia" (see stips.core.refcat.refcat_overlay_config).
#
# Astrometry is identical to refcats_gaia_ps1.py. The Gaia refcat already carries
# BP/G/RP fluxes (see nickel_refcats.gaia COLS_SQL + gaia_dr3_config.py
# mag_column_list), so no new refcat data is needed — only this wiring.
import os

# ---- Astrometry: Gaia DR3 (single-flux astrometric reference) ----
config.connections.astrometry_ref_cat = "gaia_dr3"
config.astrometry_ref_loader.anyFilterMapsToThis = "phot_g_mean"
config.astrometry_ref_loader.filterMap = {}
config.astrometry.referenceSelector.magLimit.fluxField = "phot_g_mean_flux"

# ---- Photometry: Gaia DR3 (BP/G/RP -> B/V/R/I via colorterms) ----
config.connections.photometry_ref_cat = "gaia_dr3"
# Map each science band to a Gaia flux field (base names from gaia_dr3_config.py
# mag_column_list). The "gaia*" colorterms then refine each with a Gaia colour.
config.photometry_ref_loader.filterMap = {
    "b": "phot_bp_mean",
    "v": "phot_g_mean",
    "r": "phot_rp_mean",
    "i": "phot_rp_mean",
    "halpha": "phot_rp_mean",
    "oiii": "phot_g_mean",
    "gp": "phot_g_mean",
    "rp": "phot_rp_mean",
}
config.photometry.applyColorTerms = True
config.photometry.photoCatName = "gaia"  # matches the "gaia*" block in colorterms.py
_config_dir = os.path.dirname(os.path.abspath(__file__))
config.photometry.colorterms.load(os.path.join(_config_dir, "colorterms.py"))
