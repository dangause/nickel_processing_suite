# ruff: noqa: F821
"""CTIO Y4KCam coadd-template DIA (subtractImages), Alard-Lupton kernel.

Overrides the Nickel-tuned framework default, which is wrong for Y4KCam:
  - Y4KCam is 4104x4104 @ 0.289"/px (~20' FOV) vs Nickel's 2048 @ 0.37" (~6').
    The Nickel config sets sizeCell=2048 (half the Y4KCam frame) and
    spatialKernelOrder=0 (a single constant kernel). A constant kernel cannot
    track PSF variation across Y4KCam's large field, so every star leaves a
    dipole -> thousands of spurious detections.
  - Y4KCam fields here (Landolt standards) are DENSE (thousands of stars), so —
    unlike sparse Nickel SN fields — there are plenty of kernel candidates to
    constrain a spatially varying kernel.

So: a spatially varying AL kernel (order 2) on a fine cell grid (8x8 cells of
~512 px across 4104), a few candidates per cell. Retune cell size / orders as
the residual-vs-detections tradeoff is measured.

Config hierarchy (see framework default for the full note):
  config.makeKernel.kernel["AL"].* -> PsfMatchConfigAL (active AL kernel).
"""

# --- AL kernel: spatially varying across the large dense field ---
config.makeKernel.kernel["AL"].kernelSize = 25  # ~5x FWHM at ~1.5" seeing / 0.289"/px
config.makeKernel.kernel["AL"].scaleByFwhm = False
# Reduced Alard-Lupton basis: 2 Gaussians, degrees [2, 1] -> 6 + 3 = 9 basis
# functions (vs the stack default 27). On clean, well-sampled candidates (e.g.
# NGC2298's unsaturated cluster stars) the full 27-function basis is over-rich and
# leaves the spatial fit at condition number ~1e6; the reduced basis drops it by
# ~3 orders of magnitude (cf. investigate/dia-kernel 791f6c7, condnum 2.2e6->700)
# with no loss of subtraction quality. Sigmas (px) bracket the ~2-5 px PSF.
config.makeKernel.kernel["AL"].alardNGauss = 2
config.makeKernel.kernel["AL"].alardDegGauss = [2, 1]
config.makeKernel.kernel["AL"].alardSigGauss = [1.0, 2.5]
config.makeKernel.kernel["AL"].spatialKernelOrder = 2  # track PSF variation (was 0)
config.makeKernel.kernel["AL"].spatialBgOrder = 1
# 8x8 grid of ~512 px cells over 4104 px, a few candidates per cell -> ~200
# kernel stars spread across the field to constrain the order-2 spatial fit.
config.makeKernel.kernel["AL"].sizeCellX = 512
config.makeKernel.kernel["AL"].sizeCellY = 512
config.makeKernel.kernel["AL"].nStarPerCell = 4

# --- Kernel source detection: bright, well-isolated candidates ---
config.allowKernelSourceDetection = True
config.makeKernel.selectDetection.thresholdValue = 5.0  # cleaner candidates (was 1.5)
config.makeKernel.selectDetection.nSigmaForKernel = 5.0
config.makeKernel.selectDetection.minPixels = 5

# --- Kernel quality gates (keep) ---
config.makeKernel.checkConditionNumber = True
config.makeKernel.maxConditionNumber = 1e5
config.makeKernel.kernelSumClipping = True
config.makeKernel.maxKsumSigma = 3.0

# --- Background + decorrelation ---
config.doSubtractBackground = True
# Decorrelate the matched difference so the noise is ~stationary (reduces the
# detection of correlated-noise blobs). Guarded for stack-version field name.
if hasattr(config, "doDecorrelation"):
    config.doDecorrelation = True
