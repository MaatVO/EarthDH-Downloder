"""Job specification: everything the user chose, serialisable to ``job.json``.

The GUI only builds this; the engine only reads it. A job file can be created on one machine and run on
another (``edh-dl run job.json``). It never contains the API key itself, only the key's name.
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .util import read_json

FREQS = {
    "none": "as stored",
    "hours": "every N hours",
    "day": "daily",
    "month": "monthly",
    "year": "yearly",
}
STATS = ("mean", "min", "max", "sum", "first")
COMPRESSORS = {
    "blosc-zstd": "Blosc + Zstandard (default: good ratio, fast)",
    "blosc-lz4": "Blosc + LZ4 (fastest, larger files)",
    "blosc-lz4hc": "Blosc + LZ4HC",
    "blosc-zlib": "Blosc + zlib",
    "zstd": "Zstandard (no shuffle)",
    "gzip": "gzip (most compatible, slow)",
    "none": "No compression",
}
CHUNK_LAYOUTS = {
    "auto": "Automatic (balanced)",
    "timeseries": "Time series: long in time, small in space",
    "maps": "Maps: whole area, few time steps",
    "custom": "Custom sizes",
}


@dataclass
class TimeSpec:
    start: str = "2020-01-01"  # first day (inclusive), YYYY-MM-DD
    end: str = "2020-01-31"  # last day (inclusive)
    freq: str = "none"  # one of FREQS
    hours: int = 6  # period length when freq == "hours"
    tz_offset: int = 0  # hours added to UTC before cutting days/months (e.g. 1 for CET days)
    stats: dict[str, list[str]] = field(default_factory=dict)  # variable -> statistics; missing = default
    start_hour: int = 0  # first hour (0-23, local time incl. tz_offset) on the first day
    end_hour: int = 23  # last hour (0-23, inclusive) on the last day


@dataclass
class Point:
    name: str
    lat: float
    lon: float


@dataclass
class AreaSpec:
    mode: str = "global"  # "global", "bbox" or "points"
    west: float = -180.0
    south: float = -90.0
    east: float = 180.0
    north: float = 90.0
    points: list[Point] = field(default_factory=list)
    lon_convention: str = "-180..180"  # or "0..360"


@dataclass
class OutputSpec:
    directory: str = "~/edh-data"
    name: str = "era5_subset"
    export_zarr: bool = True  # also write a plain Zarr v3 store when finished
    keep_icechunk: bool = True  # keep the Icechunk repository after exporting
    keepbits: int | None = None  # float32 mantissa bits kept (None = lossless; ERA5 on EDH itself keeps 13)
    chunk_layout: str = "auto"  # "auto", "timeseries", "maps" or "custom" (uses chunk_* below)
    chunk_time: int | None = None  # explicit sizes override the layout; None = from the layout
    chunk_lat: int | None = None
    chunk_lon: int | None = None
    chunk_points: int | None = None
    # Zarr encoding
    dtype: str = "float32"  # "float32" or "float64"
    compressor: str = "blosc-zstd"  # see COMPRESSORS
    clevel: int = 5  # compression level (blosc 1-9, zstd 1-22, gzip 1-9)
    shuffle: str = "shuffle"  # blosc only: "shuffle", "bitshuffle" or "noshuffle"
    sharding: bool = False  # group chunks into larger shard files (fewer files on disk)
    shard_auto: bool = True  # pick chunks-per-shard automatically (~512 MB shards)
    shard_time: int | None = None  # chunks per shard along time (when not automatic)
    shard_space: int | None = None  # chunks per shard along each spatial dimension
    consolidated: bool = True  # write consolidated metadata in the exported Zarr
    extra_attrs: dict = field(default_factory=dict)  # added to the global attributes


@dataclass
class DownloadSpec:
    key: str | None = None  # API key name; None = default key
    connections: int = 8
    spool_dir: str | None = None  # temporary raw chunks; None = inside the job folder
    max_spool_gb: float = 20.0
    min_free_gb: float = 10.0
    quota_safety: float = 0.9  # stop at this fraction of the monthly quota
    wait_for_quota: bool = True  # when the limit is hit: wait for the monthly reset, then continue
    memory_gb: float = 16.0  # processing memory budget


@dataclass
class JobSpec:
    dataset: str = "era5-single-levels-atmosphere"
    collection: str = "era5"
    dataset_url: str | None = None  # filled from the catalogue when empty
    variables: list[str] = field(default_factory=list)
    levels: list[float] | None = None  # for datasets with a vertical dimension; None = all
    time: TimeSpec = field(default_factory=TimeSpec)
    area: AreaSpec = field(default_factory=AreaSpec)
    output: OutputSpec = field(default_factory=OutputSpec)
    download: DownloadSpec = field(default_factory=DownloadSpec)
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobSpec":
        d = dict(d)
        t = TimeSpec(**d.pop("time", {}))
        a = dict(d.pop("area", {}))
        a["points"] = [Point(**p) for p in a.get("points", [])]
        return cls(time=t, area=AreaSpec(**a), output=OutputSpec(**d.pop("output", {})),
                   download=DownloadSpec(**d.pop("download", {})), **d)

    @classmethod
    def load(cls, path: str | Path) -> "JobSpec":
        d = read_json(path)
        if d is None:
            raise FileNotFoundError(path)
        return cls.from_dict(d.get("spec", d))

    @property
    def output_dir(self) -> Path:
        return Path(self.output.directory).expanduser()

    @property
    def icechunk_path(self) -> Path:
        return self.output_dir / f"{self.output.name}.icechunk"

    @property
    def zarr_path(self) -> Path:
        return self.output_dir / f"{self.output.name}.zarr"
