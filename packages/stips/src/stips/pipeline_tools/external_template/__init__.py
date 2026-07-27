"""Generic external-template ingestion framework.

Survey-specific behavior lives in ``sources/``; ``imaging`` holds astropy-only
helpers (importable in a plain venv) and ``core`` holds the LSST-dependent
Exposure/Butler work (importable only inside the stack).
"""
