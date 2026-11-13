"""Weather Dataset Downloader: plan, download and store weather and climate dataset subsets (Earth Data Hub, Copernicus CDS)."""

try:
    from ._version import __version__  # written by packaging/build.sh (CI sets the release number)
except ImportError:
    __version__ = "1.0.0"
