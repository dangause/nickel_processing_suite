# ruff: noqa: F821
"""Configuration for subtractImages when using SkyMapper templates.

FRAMEWORK DEFAULT: resolved instrument-dir-first, so a fork can override it by
shipping its own configs/dia/subtractImages_skymapper.py.

WHY THIS DIFFERS FROM subtractImages_ps1.py
-------------------------------------------
The PS1 config hardcodes mode="convolveTemplate" because PS1 stacks (~1" seeing)
are always SHARPER than the science images they template. That assumption does
NOT hold for SkyMapper.

Verified against the DR4 SIA at NGC2298 on 2026-07-27: the two available 100 s
"main" r-band frames have FWHM 1.76" and 2.33". Those are comparable to, or
worse than, typical CTIO 1.0m/Y4KCam science seeing. Convolving the template
UP to match the science when the template is already blurrier produces a
kernel that must deconvolve — numerically unstable and biased.

Hence mode is selected automatically from the measured PSF widths.

STACK CHECK (2026-07-27, this worktree's activated lsst_distrib):
    from lsst.ip.diffim.subtractImages import AlardLuptonSubtractConfig as C
    sorted(k for k in C.mode.allowed if k is not None)
    -> ['auto', 'convolveScience', 'convolveTemplate']
"auto" is available on this stack, so it is used directly rather than falling
back to a hardcoded convolveTemplate.
"""

# Let the task pick the convolution direction from the measured PSFs rather
# than assuming the template is sharper. See module docstring.
config.mode = "auto"

# Kernel settings mirror the PS1/CTIO tuning: a reduced AL basis keeps the
# condition number manageable on sparse fields (see the CTIO cycle-2 work that
# cut the basis 27 -> 9 and brought condnum ~3e6 -> ~1e3).
config.makeKernel.kernel["AL"].kernelSize = 21
config.makeKernel.kernel["AL"].scaleByFwhm = False
config.makeKernel.kernel["AL"].spatialKernelOrder = 0
config.makeKernel.kernel["AL"].spatialBgOrder = 0
config.makeKernel.kernel["AL"].sizeCellX = 2048
config.makeKernel.kernel["AL"].sizeCellY = 2048
config.makeKernel.kernel["AL"].nStarPerCell = 50

config.allowKernelSourceDetection = True
config.makeKernel.selectDetection.thresholdValue = 1.5
config.makeKernel.selectDetection.minPixels = 3

config.makeKernel.checkConditionNumber = True
config.makeKernel.maxConditionNumber = 1e5
config.makeKernel.kernelSumClipping = True
config.makeKernel.maxKsumSigma = 3.0

config.doSubtractBackground = True
config.doDecorrelation = True
