"""Earth Data Hub dataset downloader: plan, download and store ERA5 (and other EDH Zarr) subsets."""

try:
    from ._version import __version__  # written by packaging/build.sh (CI sets the release number)
except ImportError:
    __version__ = "0.2.0"
