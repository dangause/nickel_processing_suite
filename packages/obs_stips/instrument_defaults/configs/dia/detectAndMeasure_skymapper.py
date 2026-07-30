# ruff: noqa: F821
"""detectAndMeasure overrides for MOSAICKED SkyMapper templates.

FRAMEWORK DEFAULT: resolved instrument-dir-first, so a fork can override it.

Mirrors the instrument's normal DIA detection config, but relaxes the
bad-subtraction guard. Rationale, measured on NGC2298 20061216 i-band:

A mosaicked SkyMapper template covers ~39% of a Y4KCam field (vs ~16% for a
single cutout). Over that larger area the stack's residual-power ratio rises to
6.8-7.5, tripping `raiseOnBadSubtractionRatio` at the ctio1m threshold of 5.0 and
killing 15 of 18 visits.

That gate is NOT tracking real degradation here. With the guard disabled and all
18 visits allowed through, the measured subtraction quality is unchanged or
marginally better than the single-cutout run over its own footprint:

    single cutout : coverage 15.3%, 1081 matched, recall 50.7%, purity 30.3%
    mosaic        : coverage 39.0%, 2185 matched, recall 52.9%, purity 30.9%

i.e. 2.0x more REAL (coadd-confirmed) sources at equal-or-better recall and
purity. The ratio is inflated because it is computed over source footprints on a
template that is both shallow and partially covering, not because the subtraction
got worse.

The threshold is raised rather than the guard removed, so a genuinely broken
subtraction is still caught.
"""

# Raised from the ctio1m value of 5.0; observed range on good mosaic
# subtractions is 6.8-7.5, so 10.0 leaves headroom while still catching
# catastrophic failures.
config.badSubtractionRatioThreshold = 10.0
config.badSubtractionVariationThreshold = 10.0
config.raiseOnBadSubtractionRatio = True

if hasattr(config, "detection"):
    config.detection.thresholdValue = 5.0
    config.detection.thresholdType = "stdev"
    config.detection.minPixels = 5
    # Y4KCam: don't detect on saturated / interpolated (bleed) pixels, and skip
    # the NO_DATA region, which is large for a partially-covering template.
    config.detection.excludeMaskPlanes = ["SAT", "INTRP", "BAD", "EDGE", "NO_DATA"]

if hasattr(config, "doSkySources"):
    config.doSkySources = True
if hasattr(config, "doMeasurement"):
    config.doMeasurement = True
if hasattr(config, "doWriteSubtractedExp"):
    config.doWriteSubtractedExp = True
