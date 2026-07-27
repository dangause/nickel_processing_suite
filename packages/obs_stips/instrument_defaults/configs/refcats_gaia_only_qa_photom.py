# ruff: noqa: F821
# instrument_defaults/configs/refcats_gaia_only_qa_photom.py
#
# NEUTRAL FRAMEWORK DEFAULT. Overlay for the visit-level PHOTOMETRIC ref-match
# QA task (analysis_tools PhotometricCatalogMatchVisitTask) switching its
# reference catalog from MONSTER to Gaia DR3. Applied via --config-file by
# science.py ONLY when refcat.mode == "gaia" (southern fields with no PS1
# coverage) — the Gaia-photometry counterpart of refcats_gaia_ps1_qa_photom.py.
# The band->Gaia-flux map matches the calibrateImage overlay
# (refcats_gaia_only.py). Color terms are left off for the neutral QA tier.
config.connections.refCat = "gaia_dr3"
# Gaia band->flux base names (see gaia_dr3_config.py mag_column_list), matching
# refcats_gaia_only.py. The matcher looks up reference fluxes by physical filter
# as well as by band, so map both the lower-case band and its upper-cased spelling.
_gaia_band_map = {
    "b": "phot_bp_mean",
    "v": "phot_g_mean",
    "r": "phot_rp_mean",
    "i": "phot_rp_mean",
    "halpha": "phot_rp_mean",
    "oiii": "phot_g_mean",
    "gp": "phot_g_mean",
    "rp": "phot_rp_mean",
}
config.referenceCatalogLoader.refObjLoader.filterMap = {
    key: flux for band, flux in _gaia_band_map.items() for key in (band, band.upper())
}
config.referenceCatalogLoader.doApplyColorTerms = False
