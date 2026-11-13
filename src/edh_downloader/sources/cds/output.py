"""The output of a CDS job: periods, processing units, space and variables, in the shape the Icechunk layout,
the chunking and the Zarr export expect (the same interface as the Earth Data Hub ``planner.Plan``).

It is built once the first result file is known (the delivered grid and the GRIB parameter names come from it)
and saved in the job folder (``output.json`` and ``grid.npz``), so a resumed job writes into the same layout
even after the files it was built from have been deleted.

NetCDF products are built from all their files at once (``from_scan``): their time axis, levels and variable
names come from the files rather than from the request.
"""

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ...job import STATS
from ...planner import PlanError, Unit
from ...paths import cache_dir
from ...util import read_json, write_json_atomic
from .grib import Grid, Message, match_variables
from .planner import NATIVE_POINTS, CdsPlan, grid_step

TIME_UNITS = "hours since 1900-01-01 00:00:00"
LEVEL_DIMS = {"pressure_level": "isobaricInhPa", "model_level": "hybrid", "height_level": "heightAboveGround",
              "soil_level": "depthBelowLandLayer"}
# accumulated fields (ERA5, CERRA forecasts): the default statistic is their sum; a guess until a file is read
ACCUM = re.compile(r"precipitation|accumulated|runoff|evaporation|snowfall|radiation|_flux|sunshine|dissipation|"
                   r"_stress|snowmelt")
LEVEL_ATTRS = {"isobaricInhPa": {"units": "hPa", "long_name": "pressure level", "positive": "down"}}


@dataclass
class OutVar:
    """What the layout code reads as ``vp.var``."""
    name: str  # GRIB parameter name in the output (cfVarName)
    kind: str  # "time" or "level"
    attrs: dict
    param: int
    form_name: str | None = None
    chunks: tuple = ()


@dataclass
class OutVarPlan:
    var: OutVar
    stats: list[str]
    out_names: list[str]
    level_idx: list[int] = field(default_factory=list)


class _Meta:
    def __init__(self, level_dim, attrs):
        self.level_dim = level_dim
        self.attrs = attrs


class _Ds:
    def __init__(self, url, info, level_dim):
        self.url = url
        self.info = info
        self.meta = _Meta(level_dim, {"institution": "ECMWF / Copernicus Climate Change Service"})
        self.time_units = TIME_UNITS


class Timeline:
    """Output periods and processing units of a CDS job: known from the plan alone, before any download (GRIB),
    or from the times the files hold (NetCDF: ``instants`` and the time ``bounds`` of each file)."""

    def __init__(self, plan: CdsPlan, instants: pd.DatetimeIndex | None = None, bounds: list | None = None):
        self.cds = plan
        self.spec = plan.spec
        self.warnings: list[str] = []
        t = self.spec.time
        inst = self.cds.instants if instants is None else pd.DatetimeIndex(instants)
        if inst is None or not len(inst):
            raise PlanError("no time steps to store")
        off = pd.Timedelta(hours=t.tz_offset)
        local = inst + off
        if t.freq == "none":
            lab = local
        elif t.freq == "hours":
            lab = local.floor(f"{t.hours}h")
        elif t.freq == "day":
            lab = local.floor("D")
        elif t.freq == "month":
            lab = local.to_period("M").to_timestamp()
        elif t.freq == "year":
            lab = local.to_period("Y").to_timestamp()
        else:
            raise PlanError(f"unknown time step {t.freq}")
        change = np.r_[True, lab[1:] != lab[:-1]]
        starts = np.where(change)[0]
        self.instants = inst
        self.labels = pd.DatetimeIndex(lab[starts])
        self.p_start = starts
        self.p_end = np.r_[starts[1:], len(inst)]
        # processing units: the periods that start inside the time span of one request block (or one file)
        bounds = sorted({(pd.Timestamp(a), pd.Timestamp(b)) for a, b in bounds} if bounds is not None else
                        {(pd.Timestamp(r.start), pd.Timestamp(r.end)) for r in self.cds.requests})
        units, k, P = [], 0, len(self.labels)
        for b0, b1 in bounds:
            j = k
            while j < P and inst[self.p_start[j]] <= b1:
                j += 1
            if j > k:
                units.append(Unit(len(units), k, j, int(self.p_start[k]), int(self.p_end[j - 1])))
                k = j
        if k < P:
            units.append(Unit(len(units), k, P, int(self.p_start[k]), int(self.p_end[P - 1])))
        self.units = units
        if t.freq != "none" and len(starts) > 2:
            lens = self.p_end - self.p_start
            typical = np.median(lens)
            if lens[-1] < typical * 0.9 or lens[0] < typical * 0.9:
                self.warnings.append("the first/last period is incomplete (data available only for part of it)")

    def unit_requests(self, u: Unit) -> list[int]:
        """Indices of the requests holding the source steps of unit ``u``."""
        t0, t1 = self.instants[u.s], self.instants[u.e - 1]
        return [r.index for r in self.cds.requests
                if pd.Timestamp(r.start) <= t1 and pd.Timestamp(r.end) >= t0]


class CdsOutput:
    """Built with ``CdsOutput.create`` (from the first files) or ``CdsOutput.load`` (resume)."""

    provider = "Copernicus Climate Data Store (https://cds.climate.copernicus.eu)"

    def __init__(self, plan: CdsPlan, grid: Grid, variables: list[dict], instants=None, bounds=None,
                 levels: list | None = None, level_attrs: dict | None = None):
        self.cds = plan
        self.spec = plan.spec
        self.grid = grid
        self.warnings = list(plan.warnings)
        self.license = "; ".join(l.get("label") or "" for l in plan.desc.get("licences") or [] if l.get("label")) \
            or "see the source dataset"
        self.level_dim = None
        self.file_levels = levels  # NetCDF: the levels the files hold (else the requested ones)
        self.level_attrs = level_attrs
        self.timeline_args = (None if instants is None else [str(x) for x in pd.DatetimeIndex(instants)],
                              None if bounds is None else [[str(a), str(b)] for a, b in bounds])
        self.tl = Timeline(plan, instants, bounds)
        self.warnings += self.tl.warnings
        self.instants, self.labels, self.units = self.tl.instants, self.tl.labels, self.tl.units
        self.p_start, self.p_end = self.tl.p_start, self.tl.p_end
        self.unit_requests = self.tl.unit_requests
        self._space()
        self._variables(variables)
        self.ds = _Ds(f"https://cds.climate.copernicus.eu/datasets/{plan.dataset}",
                      {"title": plan.desc.get("title", plan.dataset), "updated": ""}, self.level_dim)
        self.n_requests = len(plan.requests)

    # ------------------------------------------------------------ space
    def _space(self):
        a, g = self.spec.area, self.grid
        conv360 = a.lon_convention == "0..360"

        def conv(lon):
            return np.asarray(lon) % 360.0 if conv360 else (np.asarray(lon) + 180.0) % 360.0 - 180.0

        self.curvilinear = g.kind == "curvilinear"
        self.points = None
        self.rows = self.cols = None  # source rows/columns kept (slices or index arrays)
        if a.mode == "points":
            self.points = a.points
            pi, pj = [], []
            for p in a.points:
                if g.kind == "regular":
                    pi.append(int(np.argmin(np.abs(g.lat - p.lat))))
                    pj.append(int(np.argmin(np.abs((g.lon - p.lon + 180.0) % 360.0 - 180.0))))
                else:
                    dlat = np.radians(g.lat - p.lat)
                    dlon = np.radians((g.lon - p.lon + 180.0) % 360.0 - 180.0)
                    d = dlat ** 2 + (np.cos(np.radians(p.lat)) * dlon) ** 2
                    i, j = np.unravel_index(int(np.argmin(d)), d.shape)
                    pi.append(int(i))
                    pj.append(int(j))
            self.point_i, self.point_j = np.array(pi), np.array(pj)
            if g.kind == "regular":
                self.out_lat, self.out_lon = g.lat[self.point_i], conv(g.lon[self.point_j])
            else:
                self.out_lat = g.lat[self.point_i, self.point_j]
                self.out_lon = conv(g.lon[self.point_i, self.point_j])
            self.shape_space = (len(a.points),)
            return
        if g.kind == "regular":
            rows = np.arange(len(g.lat))
            out_lon = conv(g.lon)
            cols = np.arange(len(g.lon))
            cut_here = a.mode == "bbox" and self.cds.area is None  # the CDS sent more than the box: cut here
            if cut_here:
                rows = np.where((g.lat >= a.south - 1e-6) & (g.lat <= a.north + 1e-6))[0]
                width = a.east - a.west if a.east >= a.west else a.east - a.west + 360.0
                if width < 360.0 - 1e-6:
                    rel = (np.asarray(g.lon, dtype=float) - a.west) % 360.0  # eastwards from the west edge
                    rel = np.where(rel > 360.0 - 1e-6, 0.0, rel)
                    cols = np.where(rel <= width + 1e-6)[0]
                    cols = cols[np.argsort(rel[cols], kind="stable")]
            if a.mode == "global" or (len(cols) > 1 and np.any(np.diff(out_lon[cols]) <= 0) and a.mode != "bbox"):
                cols = cols[np.argsort(out_lon[cols], kind="stable")]
            elif len(cols) > 1 and np.any(np.diff(out_lon[cols]) <= 0):
                self.warnings.append("the box crosses the longitude seam of the chosen convention: longitudes "
                                     "are not monotonic (switch the longitude convention to avoid this)")
            self.rows, self.cols = rows, cols
            self.out_lat, self.out_lon = g.lat[rows], out_lon[cols]
            if not len(rows) or not len(cols):
                raise PlanError("the selected box contains no grid points")
            self.shape_space = (len(rows), len(cols))
            return
        # curvilinear: keep the smallest rectangle of the grid that covers the box
        ny, nx = g.lat.shape
        r0, r1, c0, c1 = 0, ny, 0, nx
        if a.mode == "bbox":
            lon180 = (g.lon + 180.0) % 360.0 - 180.0
            w, e = a.west, a.east
            inlon = (lon180 >= w) & (lon180 <= e) if w <= e else (lon180 >= w) | (lon180 <= e)
            mask = (g.lat >= a.south) & (g.lat <= a.north) & inlon
            if not mask.any():
                raise PlanError("the selected box does not intersect the dataset's domain")
            rr, cc = np.where(mask.any(axis=1))[0], np.where(mask.any(axis=0))[0]
            r0, r1, c0, c1 = int(rr[0]), int(rr[-1]) + 1, int(cc[0]), int(cc[-1]) + 1
        self.rows, self.cols = slice(r0, r1), slice(c0, c1)
        self.out_lat = g.lat[r0:r1, c0:c1]
        self.out_lon = conv(g.lon[r0:r1, c0:c1])
        self.shape_space = (r1 - r0, c1 - c0)

    def cut(self, slab: np.ndarray) -> np.ndarray:
        """Source field(s) ``(..., nj, ni)`` -> output space."""
        if self.points is not None:
            return slab[..., self.point_i, self.point_j]
        if self.curvilinear:
            return slab[..., self.rows, self.cols]
        return slab[..., self.rows[:, None], self.cols[None, :]]

    # ------------------------------------------------------------ variables
    def _variables(self, variables: list[dict]):
        t = self.spec.time
        self.has_levels = any(v["kind"] == "level" for v in variables)
        if self.has_levels:
            src = self.cds.levels if self.file_levels is None else self.file_levels
            self.level_values = [float(x) if not float(x).is_integer() else int(float(x)) for x in src]
            self.level_dim = next(v["level_type"] for v in variables if v["kind"] == "level") or "level"
        else:
            self.level_values = []
        self.vars: list[OutVarPlan] = []
        used = set()
        for v in variables:
            if t.freq == "none":
                stats = ["none"]
            else:
                stats = (t.stats.get(v["form_name"]) if v.get("form_name") else None) or t.stats.get(v["name"]) \
                    or ["sum" if v["step_type"] == "accum" else "mean"]
                bad = [s for s in stats if s not in STATS]
                if bad:
                    raise PlanError(f"unknown statistic {bad} for {v['name']}")
            name = v["name"]
            if name in used:
                name = f"{name}_{v['param']}"
            used.add(name)
            out_names = [name] if len(stats) == 1 else [f"{name}_{s}" for s in stats]
            if v.get("attrs") is not None:  # NetCDF: the file's own attributes
                attrs = dict(v["attrs"])
                attrs.setdefault("long_name", v["long_name"])
                attrs["source_variable"] = v["short_name"]
            else:
                attrs = {"long_name": v["long_name"], "units": v["units"], "GRIB_paramId": v["param"],
                         "GRIB_shortName": v["short_name"], "GRIB_stepType": v["step_type"],
                         "GRIB_typeOfLevel": v["level_type"]}
            if v.get("form_name"):
                attrs["cds_variable"] = v["form_name"]
            self.vars.append(OutVarPlan(OutVar(name, v["kind"], attrs, v["param"], v.get("form_name")), stats,
                                        out_names, list(range(len(self.level_values))) if v["kind"] == "level" else []))
        self.level_pos = {i: i for i in range(len(self.level_values))}

    # ------------------------------------------------------------ preview (before any download)
    @classmethod
    def preview(cls, plan: CdsPlan) -> "CdsOutput | None":
        """The output as it will most likely be, from the plan alone: the grid from the area and spacing (or as
        delivered by an earlier job of this dataset), the names of the variables from an earlier job (else the
        form's names). ``exact`` tells what is only a guess. None when the grid cannot be told before a file
        arrives (a projected grid never downloaded here)."""
        notes = []
        nc = plan.desc.get("tier") == "netcdf"
        if plan.instants is None:  # no time selection: the time axis is only known from the files
            return None
        known = learned(plan.dataset)
        g = _cached_grid(plan.dataset, plan.grid) if plan.area is None else None
        if g is None and plan.desc.get("area") is not None and (not nc or grid_step(plan.desc)):
            g = _regular_grid(plan)
            if nc:
                notes.append("grid guessed from the dataset's spacing; the files tell the real one")
        if g is None and nc:
            return None
        whole = False
        if g is None:
            n = next((p for k, p in NATIVE_POINTS.items() if plan.dataset.startswith(k)), None)
            if not n:
                return None
            side = math.isqrt(n)
            zero = np.broadcast_to(np.float32(0), (side, side))
            g = Grid("curvilinear", zero, zero, {})
            whole = plan.spec.area.mode == "bbox"
            notes.append("projected grid: its coordinates are read from the first file" +
                         ("; the shape shown is the whole domain, the box is cut from it then" if whole else ""))
        if whole:  # the box cannot be cut without the coordinates: show the whole domain
            plan = copy.copy(plan)
            plan.spec = copy.deepcopy(plan.spec)
            plan.spec.area.mode = "global"
        lv_dim = LEVEL_DIMS.get(plan.level_field or "", plan.level_field or "level")
        variables, guessed = [], []
        for i, name in enumerate(plan.variables):
            v = known.get(name)
            if v is None:
                guessed.append(name)
                label = next((x["label"] for x in plan.desc.get("variables", []) if x["name"] == name), name)
                v = {"param": -1 - i, "name": name, "short_name": name, "long_name": label, "units": "",
                     "step_type": "accum" if ACCUM.search(name) and "instantaneous" not in name and not nc
                     else "instant",
                     "level_type": lv_dim if plan.levels else "surface"}
            kind = "level" if plan.level_field and plan.levels else "time"
            if kind == "level" and v.get("level_type") in ("surface", "meanSea", "entireAtmosphere"):
                kind = "time"  # learned from an earlier job: this one has no levels
            variables.append({**v, "form_name": name, "kind": kind})
        if guessed and nc:
            notes.append("names shown as in the form; the stored names are the NetCDF files' own, known once they "
                         "arrive")
        elif guessed:
            notes.append("names shown as in the form; the stored names are the GRIB short names (t2m, tp …), "
                         "known after the first file")
        if nc:
            notes.append("time steps shown as requested; the store keeps the files' own times (monthly means are "
                         "often stamped mid-month)")
        out = cls(plan, g, variables)
        if g.lat.ndim == 2 and not g.lat.any() and plan.spec.area.mode == "points":
            out.out_lat = np.array([p.lat for p in plan.spec.area.points])
            out.out_lon = np.array([p.lon for p in plan.spec.area.points])
        out.preview_notes = notes
        out.exact = not notes
        return out

    @staticmethod
    def describe_variables(messages: list[Message], plan: CdsPlan) -> list[dict]:
        """One entry per GRIB parameter found in the first files, matched to the form's variable names."""
        first = {}
        for m in messages:
            first.setdefault(m.param, m)
        mapping = {p: f for f, p in match_variables(plan.variables, list(first.values())).items()}
        out = []
        for p, m in sorted(first.items(), key=lambda x: x[1].cf_name or x[1].short_name):
            out.append({"param": p, "name": m.cf_name if m.cf_name not in ("", "unknown") else m.short_name,
                        "short_name": m.short_name, "long_name": m.name, "units": m.units,
                        "step_type": m.step_type, "level_type": m.level_type,
                        "kind": "level" if plan.level_field and len(plan.levels) and (
                            m.level_type == LEVEL_DIMS.get(plan.level_field) or m.level_type not in
                            ("surface", "heightAboveGround", "meanSea", "entireAtmosphere")) else "time",
                        "form_name": mapping.get(p)})
        return out

    @classmethod
    def from_scan(cls, plan: CdsPlan, sc) -> tuple["CdsOutput", list[dict]]:
        """The output of a NetCDF job, from what its files hold (``netcdf.scan``): their grid, times, levels and
        variables (matched to the form's names for the per-variable statistics)."""
        from types import SimpleNamespace

        params = {name: i for i, name in enumerate(sorted(sc.fields))}
        like = [SimpleNamespace(param=params[n], name=f[0].attrs.get("long_name", n), short_name=n, cf_name=n)
                for n, f in sc.fields.items()]
        mapping = {p: f for f, p in match_variables(plan.variables, like).items()}
        variables = []
        for name in sorted(sc.fields):
            f0 = sc.fields[name][0]
            variables.append({"param": params[name], "name": name, "short_name": name,
                              "long_name": str(f0.attrs.get("long_name", name)), "units": str(f0.attrs.get("units", "")),
                              "step_type": "instant", "level_type": f0.level or "surface",
                              "kind": "level" if f0.level is not None else "time",
                              "form_name": mapping.get(params[name]), "attrs": f0.attrs})
        bounds = sorted({(f.times.min(), f.times.max()) for fl in sc.fields.values() for f in fl if len(f.times)})
        out = cls(plan, sc.grid, variables, instants=sc.times, bounds=bounds,
                  levels=sc.levels if sc.level_dim else None, level_attrs=sc.level_attrs or None)
        out.warnings += sc.notes
        return out, variables

    # ------------------------------------------------------------ layout interface (as planner.Plan)
    @property
    def n_periods(self) -> int:
        return len(self.labels)

    def out_shape(self, vp) -> tuple:
        lev = (len(self.level_values),) if vp.var.kind == "level" else ()
        return (self.n_periods, *lev, *self.shape_space)

    def out_dims(self, vp) -> tuple:
        space = ("point",) if self.points is not None else ("y", "x") if self.curvilinear else ("latitude", "longitude")
        lev = (self.level_dim,) if vp.var.kind == "level" else ()
        return ("valid_time", *lev, *space)

    def out_chunks(self, vp) -> tuple:
        from ...chunking import staging_shape

        return staging_shape(self, vp)

    def export_chunks(self, vp) -> tuple:
        from ...chunking import chunk_shape

        return chunk_shape(self, vp, self.spec.output.chunk_layout, self.spec.output)

    def export_shards(self, vp) -> tuple | None:
        from ...chunking import shard_shape

        return shard_shape(self, vp, self.export_chunks(vp), self.spec.output)

    def summary(self) -> dict:
        s = self.cds.summary()
        s.update({"periods": self.n_periods, "units": len(self.units), "space": list(self.shape_space),
                  "outputs": [n for vp in self.vars for n in vp.out_names]})
        return s

    # ------------------------------------------------------------ persistence
    def remember(self, variables: list[dict]) -> None:
        """Keep what the first files told about this dataset (the variables' GRIB names; the whole-domain grid),
        so later previews of the same dataset show the real output before anything is downloaded."""
        try:
            path = cache_dir() / "cds_variables.json"
            d = read_json(path, {}, dict)
            mine = d.setdefault(self.cds.dataset, {})
            for v in variables:
                if v.get("form_name"):
                    mine[v["form_name"]] = {k: v[k] for k in ("param", "name", "short_name", "long_name", "units",
                                                              "step_type", "level_type")}
            write_json_atomic(path, d)
            if self.cds.area is None:
                p = _grid_path(self.cds.dataset, self.cds.grid)
                p.parent.mkdir(parents=True, exist_ok=True)
                np.savez(p, kind=self.grid.kind, lat=self.grid.lat, lon=self.grid.lon)
        except Exception:  # noqa: BLE001  (a cache only)
            pass

    def save(self, job_dir: Path, variables: list[dict]) -> None:
        """Atomically, the grid first: ``output.json`` exists only once both are complete."""
        job_dir = Path(job_dir)
        tmp = job_dir / "grid.tmp.npz"
        np.savez(tmp, lat=self.grid.lat, lon=self.grid.lon)
        os.replace(tmp, job_dir / "grid.npz")
        instants, bounds = self.timeline_args
        write_json_atomic(job_dir / "output.json",
                          {"grid_kind": self.grid.kind, "grid_attrs": self.grid.attrs, "variables": variables,
                           "instants": instants, "bounds": bounds, "levels": self.file_levels,
                           "level_attrs": self.level_attrs})

    @classmethod
    def load(cls, plan: CdsPlan, job_dir: Path) -> "CdsOutput | None":
        p, g = Path(job_dir) / "output.json", Path(job_dir) / "grid.npz"
        if not (p.exists() and g.exists()):
            return None
        d = json.loads(p.read_text())
        arr = np.load(g)
        out = cls(plan, Grid(d["grid_kind"], arr["lat"], arr["lon"], d.get("grid_attrs") or {}), d["variables"],
                  instants=d.get("instants"), bounds=d.get("bounds"), levels=d.get("levels"),
                  level_attrs=d.get("level_attrs"))
        out.variables_raw = d["variables"]
        return out


# ------------------------------------------------------------------ what earlier jobs taught

def _grid_path(dataset: str, grid: str | None) -> Path:
    return cache_dir() / "cds_grids" / f"{dataset}_{(grid or 'native').replace('/', 'x')}.npz"


def _cached_grid(dataset: str, grid: str | None) -> Grid | None:
    p = _grid_path(dataset, grid)
    if not p.exists():
        return None
    try:
        a = np.load(p)
        return Grid(str(a["kind"]), a["lat"], a["lon"])
    except Exception:  # noqa: BLE001
        return None


def learned(dataset: str) -> dict:
    """``{form name: GRIB variable}`` seen in earlier jobs of a dataset."""
    return read_json(cache_dir() / "cds_variables.json", {}, dict).get(dataset, {})


def _regular_grid(plan: CdsPlan) -> Grid:
    """The latitude/longitude grid the CDS will deliver for the plan's area and spacing: north to south, and
    for the whole globe 0 … 360 (as ERA5 does)."""
    native = grid_step(plan.desc) or [0.25, 0.25]
    if plan.grid:
        dlon, dlat = (float(x) for x in plan.grid.split("/"))
    else:
        dlat, dlon = float(native[0] or 0.25), float(native[-1] or 0.25)
    n, w, s, e = plan.area or (90, 0, -90, 360 - dlon)
    lat = n - np.arange(math.floor((n - s) / dlat + 1e-9) + 1) * dlat
    lon = w + np.arange(math.floor((e - w) / dlon + 1e-9) + 1) * dlon
    return Grid("regular", np.round(lat, 6), np.round(lon, 6))
