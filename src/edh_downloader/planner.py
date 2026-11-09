"""Turn a job spec into an exact download plan.

The plan lists every source chunk the selection touches (each is fetched exactly once), groups output
time periods into *units* aligned with the source time chunks (one Icechunk commit per unit), and splits
space into processing *groups* that fit the memory budget.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .catalog import Dataset, Variable
from .job import STATS, JobSpec

DEFAULT_COMPRESSION = 0.165  # compressed / raw size of ERA5 chunks on EDH (measured)


class PlanError(ValueError):
    pass


@dataclass
class VarPlan:
    var: Variable
    stats: list[str]
    out_names: list[str]
    level_idx: list[int]  # source indices along the level dim (empty for no level dim)
    tc: int  # source time chunk length (0 for static)
    cy: int
    cx: int
    tiles: list[tuple[int, int]]
    groups: list[list[tuple[int, int]]] = field(default_factory=list)

    def chunk_url(self, base: str, t: int | None, li: int | None, ty: int, tx: int) -> str:
        parts = [self.var.name, "c"]
        if t is not None:
            parts.append(str(t))
        if li is not None:
            parts.append(str(li))
        parts += [str(ty), str(tx)]
        return f"{base}/{'/'.join(parts)}"


@dataclass
class Unit:
    index: int
    k0: int  # output periods [k0, k1)
    k1: int
    s: int  # source time indices [s, e)
    e: int
    static: bool = False

    @property
    def span(self) -> int:
        return self.e - self.s


class Plan:
    def __init__(self, spec: JobSpec, ds: Dataset):
        self.spec = spec
        self.ds = ds
        self.warnings: list[str] = []
        self._select_vars()
        self._select_space()
        self._select_time()
        self._tiles_and_groups()
        self._chunks()

    # ------------------------------------------------------------ variables / levels
    def _select_vars(self):
        spec, ds = self.spec, self.ds
        if not spec.variables:
            raise PlanError("select at least one variable")
        unknown = [v for v in spec.variables if v not in ds.variables]
        if unknown:
            raise PlanError(f"unknown variables: {', '.join(unknown)}")
        self.vars: list[VarPlan] = []
        freq = spec.time.freq
        for name in spec.variables:
            v = ds.variables[name]
            if v.kind == "unsupported":
                raise PlanError(f"variable {name} has an unsupported layout {v.dims}")
            if v.kind == "static" or freq == "none":
                stats = ["none"]
            else:
                stats = spec.time.stats.get(name) or [v.default_stat()]
                bad = [s for s in stats if s not in STATS]
                if bad:
                    raise PlanError(f"unknown statistic {bad} for {name}")
            out_names = [name] if len(stats) == 1 else [f"{name}_{s}" for s in stats]
            level_idx = []
            if v.kind == "level":
                levels = ds.levels.tolist()
                wanted = spec.levels if spec.levels else levels
                missing = [l for l in wanted if l not in levels]
                if missing:
                    raise PlanError(f"levels {missing} not available; available: {levels}")
                level_idx = sorted(levels.index(l) for l in wanted)
            tc = v.chunks[0] if v.kind != "static" else 0
            cy, cx = v.chunks[-2], v.chunks[-1]
            if v.kind == "level" and v.chunks[1] != 1:
                raise PlanError(f"{name}: level chunk size {v.chunks[1]} != 1 is not supported")
            self.vars.append(VarPlan(v, stats, out_names, level_idx, tc, cy, cx, tiles=[]))
        self.has_levels = any(vp.var.kind == "level" for vp in self.vars)
        if self.has_levels:
            all_idx = sorted({i for vp in self.vars for i in vp.level_idx})
            self.level_values = [self.ds.levels[i].item() for i in all_idx]
            self.level_pos = {i: n for n, i in enumerate(all_idx)}  # source level idx -> output position
        self.time_vars = [vp for vp in self.vars if vp.var.kind != "static"]
        if len({vp.tc for vp in self.time_vars}) > 1:
            self.warnings.append("selected variables use different time chunking; some chunks span two units")

    # ------------------------------------------------------------ space
    def _select_space(self):
        a, ds = self.spec.area, self.ds
        lat = ds.lat
        lon180 = (ds.lon + 180.0) % 360.0 - 180.0
        conv360 = a.lon_convention == "0..360"
        out_lon_all = ds.lon % 360.0 if conv360 else lon180
        eps = 1e-6
        if a.mode == "points":
            if not a.points:
                raise PlanError("add at least one point")
            self.points = a.points
            pi, pj = [], []
            for p in a.points:
                if not -90 <= p.lat <= 90:
                    raise PlanError(f"point {p.name}: latitude {p.lat} out of range")
                pi.append(int(np.argmin(np.abs(lat - p.lat))))
                d = np.abs(((ds.lon - p.lon) + 180.0) % 360.0 - 180.0)
                pj.append(int(np.argmin(d)))
            self.point_i, self.point_j = np.array(pi), np.array(pj)
            self.lat_idx = self.lon_idx = None
            self.out_lat = lat[self.point_i]
            self.out_lon = out_lon_all[self.point_j]
            self.shape_space = (len(a.points),)
            return
        self.points = None
        if a.mode == "global":
            lat_idx = np.arange(len(lat))
            order = np.argsort(out_lon_all, kind="stable")
            lon_idx = order
        elif a.mode == "bbox":
            if a.south > a.north:
                raise PlanError("south must be <= north")
            lat_idx = np.where((lat >= a.south - eps) & (lat <= a.north + eps))[0]
            w = (a.west + 180.0) % 360.0 - 180.0 if a.west != 180 else 180.0
            e = (a.east + 180.0) % 360.0 - 180.0 if a.east != 180 else 180.0
            if a.east - a.west >= 360:
                mask = np.ones(len(lon180), bool)
            elif w <= e:
                mask = (lon180 >= w - eps) & (lon180 <= e + eps)
            else:  # crosses the antimeridian
                mask = (lon180 >= w - eps) | (lon180 <= e + eps)
            lon_idx = np.where(mask)[0]
            if w <= e or conv360:
                key = out_lon_all[lon_idx]
            else:
                key = (lon180[lon_idx] - w) % 360.0
            lon_idx = lon_idx[np.argsort(key, kind="stable")]
            if len(lon_idx) > 1 and np.any(np.diff(out_lon_all[lon_idx]) <= 0):
                self.warnings.append("the box crosses the longitude seam of the chosen convention: longitudes "
                                     "are not monotonic (switch the longitude convention to avoid this)")
        else:
            raise PlanError(f"unknown area mode {a.mode}")
        if len(lat_idx) == 0 or len(lon_idx) == 0:
            raise PlanError("the selected box contains no grid points")
        self.lat_idx, self.lon_idx = lat_idx, lon_idx
        self.out_lat, self.out_lon = lat[lat_idx], out_lon_all[lon_idx]
        self.shape_space = (len(lat_idx), len(lon_idx))

    # ------------------------------------------------------------ time
    def _select_time(self):
        t, ds = self.spec.time, self.ds
        if not self.time_vars:
            self.labels = pd.DatetimeIndex([])
            self.p_start = self.p_end = np.array([], dtype=int)
            self.units = [Unit(0, 0, 0, 0, 0, static=True)]
            return
        times = ds.time
        if not (0 <= t.start_hour <= 23 and 0 <= t.end_hour <= 23):
            raise PlanError("the hours must be between 0 and 23")
        if not -23 <= t.tz_offset <= 23:
            raise PlanError("the UTC offset must be between -23 and 23 hours")
        start = pd.Timestamp(t.start) + pd.Timedelta(hours=t.start_hour)
        end = pd.Timestamp(t.end) + pd.Timedelta(hours=t.end_hour + 1)  # end exclusive
        if start >= end:
            raise PlanError("start must be before end")
        off = pd.Timedelta(hours=t.tz_offset)
        local = times + off
        if t.freq == "none":
            lab = local
        elif t.freq == "hours":
            if t.hours < 1:
                raise PlanError("hours must be >= 1")
            lab = local.floor(f"{t.hours}h")
        elif t.freq == "day":
            lab = local.floor("D")
        elif t.freq == "month":
            lab = local.to_period("M").to_timestamp()
        elif t.freq == "year":
            lab = local.to_period("Y").to_timestamp()
        else:
            raise PlanError(f"unknown time step {t.freq}")
        sel = np.where((lab >= start) & (lab < end))[0]
        if len(sel) == 0:
            t0, t1 = ds.time_range
            raise PlanError(f"no data in the selected period; dataset covers {t0} .. {t1}")
        i0, i1 = int(sel[0]), int(sel[-1]) + 1
        labv = lab[i0:i1]
        change = np.r_[True, labv[1:] != labv[:-1]]
        starts = np.where(change)[0]
        self.labels = pd.DatetimeIndex(labv[starts])
        self.p_start = starts + i0
        self.p_end = np.r_[starts[1:], i1 - i0] + i0
        # incomplete edge periods (dataset ends mid-period, or tz offset at the dataset start)
        if t.freq != "none" and len(starts) > 2:
            lens = self.p_end - self.p_start
            typical = np.median(lens)
            if lens[-1] < typical * 0.9 or lens[0] < typical * 0.9:
                self.warnings.append("the first/last period is incomplete (data available only for part of it)")
        step = ds.time_step() or pd.Timedelta(hours=1)
        if local[0] > start or local[-1] + step < end:
            t0, t1 = ds.time_range
            self.warnings.append(f"the period was clipped to the available data ({t0} .. {t1} UTC)")
        tc = self.time_vars[0].tc
        units, k, P = [], 0, len(self.labels)
        while k < P:
            boundary = (self.p_start[k] // tc + 1) * tc
            j = k + 1
            while j < P and self.p_start[j] < boundary:  # periods starting inside this source chunk
                j += 1
            units.append(Unit(len(units), k, j, int(self.p_start[k]), int(self.p_end[j - 1])))
            k = j
        self.units = units

    # ------------------------------------------------------------ tiles, groups, chunks
    def _tiles_and_groups(self):
        budget = self.spec.download.memory_gb * 1e9 / 3  # a few groups are in memory at once
        span = max((u.span for u in self.units), default=1) or 1
        for vp in self.vars:
            if self.points is not None:
                vp.tiles = sorted({(int(i // vp.cy), int(j // vp.cx)) for i, j in zip(self.point_i, self.point_j)})
                vp.groups = [vp.tiles]
                continue
            rows = sorted(set((self.lat_idx // vp.cy).tolist()))
            cols = sorted(set((self.lon_idx // vp.cx).tolist()))
            vp.tiles = [(r, c) for r in rows for c in cols]
            per_tile = (span if vp.var.kind != "static" else 1) * vp.cy * vp.cx * 4 * 2
            k = max(1, int(budget // per_tile))
            groups = []
            if k >= len(cols):  # whole rows (several rows per group if they fit)
                nrow = max(1, k // len(cols))
                for i in range(0, len(rows), nrow):
                    groups.append([(r, c) for r in rows[i:i + nrow] for c in cols])
            else:
                for r in rows:
                    for i in range(0, len(cols), k):
                        groups.append([(r, c) for c in cols[i:i + k]])
            vp.groups = groups

    def unit_chunks(self, u: Unit) -> list[tuple[str, int, int | None, int | None, int, int]]:
        """Chunks unit ``u`` needs: (var, var_index, time_chunk, level_idx, ty, tx)."""
        out = []
        for n, vp in enumerate(self.vars):
            if vp.var.kind == "static":
                if u.index == 0:
                    out += [(vp.var.name, n, None, None, ty, tx) for ty, tx in vp.tiles]
                continue
            if u.static:
                continue
            for t in range(u.s // vp.tc, (u.e - 1) // vp.tc + 1):
                for li in (vp.level_idx or [None]):
                    out += [(vp.var.name, n, t, li, ty, tx) for ty, tx in vp.tiles]
        return out

    def _chunks(self):
        self.chunk_last_unit: dict[tuple, int] = {}
        self.unit_needs: list[list[tuple]] = []
        for u in self.units:
            ch = self.unit_chunks(u)
            self.unit_needs.append(ch)
            for c in ch:
                self.chunk_last_unit[c] = u.index
        self.n_requests = len(self.chunk_last_unit)
        raw = 0
        for c in self.chunk_last_unit:
            vp = self.vars[c[1]]
            raw += int(np.prod(vp.var.chunks)) * vp.var.decoder().dtype.itemsize
        self.raw_bytes = raw

    # ------------------------------------------------------------ output layout
    @property
    def n_periods(self) -> int:
        return len(self.labels)

    def out_shape(self, vp: VarPlan) -> tuple:
        lev = (len(self.level_values),) if vp.var.kind == "level" else ()
        if vp.var.kind == "static":
            return self.shape_space
        return (self.n_periods, *lev, *self.shape_space)

    def out_dims(self, vp: VarPlan) -> tuple:
        space = ("point",) if self.points is not None else ("latitude", "longitude")
        if vp.var.kind == "static":
            return space
        lev = (self.ds.meta.level_dim,) if vp.var.kind == "level" else ()
        return ("valid_time", *lev, *space)

    def out_chunks(self, vp: VarPlan) -> tuple:
        """Chunks of the Icechunk repository (download-aligned: each chunk written once)."""
        from .chunking import staging_shape

        return staging_shape(self, vp)

    def export_chunks(self, vp: VarPlan) -> tuple:
        """Chunks of the exported Zarr (the user's layout)."""
        from .chunking import chunk_shape

        return chunk_shape(self, vp, self.spec.output.chunk_layout, self.spec.output)

    def export_shards(self, vp: VarPlan) -> tuple | None:
        from .chunking import shard_shape

        return shard_shape(self, vp, self.export_chunks(vp), self.spec.output)

    def out_bytes(self) -> int:
        n = 0
        for vp in self.vars:
            n += int(np.prod(self.out_shape(vp))) * (8 if self.spec.output.dtype == "float64" else 4) * len(vp.out_names)
        return n

    # ------------------------------------------------------------ summary
    def summary(self, bytes_per_chunk: float | None = None, throughput: float | None = None) -> dict:
        dl = self.n_requests * bytes_per_chunk if bytes_per_chunk else self.raw_bytes * DEFAULT_COMPRESSION
        d = {
            "requests": self.n_requests,
            "download_bytes_est": dl,
            "raw_bytes": self.raw_bytes,
            "output_bytes_uncompressed": self.out_bytes(),
            "periods": self.n_periods,
            "first": str(self.labels[0]) if self.n_periods else None,
            "last": str(self.labels[-1]) if self.n_periods else None,
            "units": len(self.units),
            "space": list(self.shape_space),
            "levels": self.level_values if self.has_levels else None,
            "outputs": [n for vp in self.vars for n in vp.out_names],
            "warnings": self.warnings,
            "eta_s": dl / throughput if throughput else None,
        }
        return d
