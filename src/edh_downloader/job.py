"""Job specification: everything the user chose, serialisable to ``job.json``.

The GUI only builds this; the engine only reads it. A job file can be created on one machine and run on
another (``edh-dl run job.json``). It never contains the API key itself, only the key's name.
"""

import shutil
from dataclasses import asdict, dataclass, field, fields
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


def _make(cls, d: dict | None, **over):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in known and k not in over}, **over)


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
    target_mode: str = "create"  # "create" or "extend"
    target_store: str | None = None  # path to existing .icechunk (or companion .zarr)
    extend_direction: str = "forward"  # "forward" (after) or "backward" (before)


@dataclass
class DownloadSpec:
    key: str | None = None  # API key name; None = default key
    connections: int = 8
    spool_dir: str | None = None  # temporary raw chunks; None = <output folder>/tmp/<job id>
    max_spool_gb: float = 20.0
    min_free_gb: float = 10.0
    quota_safety: float = 0.9  # stop at this fraction of the monthly quota
    wait_for_quota: bool = True  # when the limit is hit: wait for the monthly reset, then continue
    memory_gb: float = 16.0  # processing memory budget


@dataclass
class CdsSpec:
    """Request options that only the Climate Data Store has (ignored for Earth Data Hub jobs)."""
    grid: list[float] | None = None  # output grid spacing [dlat, dlon] in degrees, regridded by CDS; None = native
    options: dict = field(default_factory=dict)  # other request fields of the dataset form (product_type, ...)
    max_inflight: int = 4  # requests waiting or running on CDS at once (lowered automatically when CDS limits it)
    pack: str = "auto"  # "auto": whole months packed up to the cost limit (fewest requests); "month": one month each
    max_request_gb: float = 10.0  # largest download one request may produce


@dataclass
class JobSpec:
    source: str = "edh"  # data source id (see ``sources``); jobs written before sources existed are Earth Data Hub
    dataset: str = "era5-single-levels-atmosphere"
    collection: str = "era5"
    dataset_url: str | None = None  # filled from the catalogue when empty
    variables: list[str] = field(default_factory=list)
    levels: list[float] | None = None  # for datasets with a vertical dimension; None = all
    time: TimeSpec = field(default_factory=TimeSpec)
    area: AreaSpec = field(default_factory=AreaSpec)
    output: OutputSpec = field(default_factory=OutputSpec)
    download: DownloadSpec = field(default_factory=DownloadSpec)
    cds: CdsSpec = field(default_factory=CdsSpec)
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "JobSpec":
        """Unknown fields are ignored and missing ones take their default: a job written by another version of
        the app (older or newer) still loads."""
        d = dict(d)
        t = _make(TimeSpec, d.pop("time", None))
        a = dict(d.pop("area", None) or {})
        a["points"] = [_make(Point, p) for p in a.get("points") or []]
        return _make(cls, d, time=t, area=_make(AreaSpec, a), output=_make(OutputSpec, d.pop("output", None)),
                     download=_make(DownloadSpec, d.pop("download", None)), cds=_make(CdsSpec, d.pop("cds", None)))

    @classmethod
    def load(cls, path: str | Path) -> "JobSpec":
        d = read_json(path)
        if d is None:
            raise FileNotFoundError(path)
        return cls.from_dict(d.get("spec", d))

    @property
    def output_dir(self) -> Path:
        return Path(self.output.directory).expanduser()

    def spool_path(self, job_id: str, job_dir: Path | None = None) -> Path:
        """Temporary raw chunks of a job: ``<spool_dir>/<job id>``, by default ``<output folder>/tmp/<job id>``.

        A job started before that default existed has its chunks in ``<job folder>/spool``: it keeps using them
        (pass ``job_dir``), so resuming it downloads nothing twice."""
        d = self.download.spool_dir
        if d:
            return Path(d).expanduser() / job_id
        if job_dir is not None and (Path(job_dir) / "spool").is_dir():
            return Path(job_dir) / "spool"
        return self.output_dir / "tmp" / job_id

    def remove_spool(self, job_id: str, job_dir: Path | None = None) -> None:
        """Delete the job's temporary chunks, and the default ``tmp/`` folder once no other job uses it."""
        shutil.rmtree(self.spool_path(job_id, job_dir), ignore_errors=True)
        shutil.rmtree(self.spool_path(job_id), ignore_errors=True)
        if not self.download.spool_dir:
            try:
                (self.output_dir / "tmp").rmdir()  # only succeeds when empty
            except OSError:
                pass

    @property
    def icechunk_path(self) -> Path:
        if self.output.target_mode == "extend" and self.output.target_store:
            p = Path(self.output.target_store).expanduser()
            if p.suffix == ".zarr" and p.with_suffix(".icechunk").exists():
                return p.with_suffix(".icechunk")
            return p if p.suffix == ".icechunk" else p.with_suffix(".icechunk")
        return self.output_dir / f"{self.output.name}.icechunk"

    @property
    def zarr_path(self) -> Path:
        if self.output.target_mode == "extend" and self.output.target_store:
            p = Path(self.output.target_store).expanduser()
            return p if p.suffix == ".zarr" else p.with_suffix(".zarr")
        return self.output_dir / f"{self.output.name}.zarr"
