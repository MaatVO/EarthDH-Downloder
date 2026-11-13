"""The eckit libraries that the ecCodes library from the ``eccodeslib`` wheel links to (GRIB decoding for Climate
Data Store jobs). pyinstaller-hooks-contrib collects ``eccodeslib`` and ``gribapi``, and lists ``eckitlib`` as a
hidden import, but does not collect its shared libraries; ``findlibs`` loads them from the package directory at
run time, so they keep their place under ``eckitlib/``."""

from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("eckitlib")
