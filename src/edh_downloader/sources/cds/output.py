"""The output of a CDS job: periods, processing units, space and variables, in the shape the Icechunk layout,
the chunking and the Zarr export expect (the same interface as the Earth Data Hub ``planner.Plan``).

It is built once the first result file is known (the delivered grid and the GRIB parameter names come from it)
and saved in the job folder (``output.json`` and ``grid.npz``), so a resumed job writes into the same layout
even after the files it was built from have been deleted.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ...job import STATS
from ...planner import PlanError, Unit
from .grib import Grid, Message, match_variables
from .planner import CdsPlan

TIME_UNITS = "hours since 1900-01-01 00:00:00"
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
    """Output periods and processing units of a CDS job: known from the plan alone, before any download."""

    def __init__(self, plan: CdsPlan):
        self.cds = plan
        self.spec = plan.spec
        self.warnings: list[str] = []
        t = self.spec.time
        inst = self.cds.instants
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
        # processing units: the periods that start inside the time span of one request block
        bounds = sorted({(pd.Timestamp(r.start), pd.Timestamp(r.end)) for r in self.cds.requests})
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

    def __init__(self, plan: CdsPlan, grid: Grid, variables: list[dict]):
        self.cds = plan
        self.spec = plan.spec
        self.grid = grid
        self.warnings = list(plan.warnings)
        self.license = (plan.desc.get("licences") or [{}])[0].get("label") or "see the source dataset"
        self.level_dim = None
        self.tl = Timeline(plan)
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
            if a.mode == "bbox" and self.cds.area is None:  # the CDS sent more than the box: cut here
                rows = np.where((g.lat >= a.south - 1e-6) & (g.lat <= a.north + 1e-6))[0]
            out_lon = conv(g.lon)
            cols = np.arange(len(g.lon))
            if a.mode == "global" or (len(cols) > 1 and np.any(np.diff(out_lon) <= 0) and a.mode != "bbox"):
                cols = np.argsort(out_lon, kind="stable")
            elif len(cols) > 1 and np.any(np.diff(out_lon) <= 0):
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
            self.level_values = [float(x) if not float(x).is_integer() else int(float(x)) for x in self.cds.levels]
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
            attrs = {"long_name": v["long_name"], "units": v["units"], "GRIB_paramId": v["param"],
                     "GRIB_shortName": v["short_name"], "GRIB_stepType": v["step_type"],
                     "GRIB_typeOfLevel": v["level_type"]}
            if v.get("form_name"):
                attrs["cds_variable"] = v["form_name"]
            self.vars.append(OutVarPlan(OutVar(name, v["kind"], attrs, v["param"], v.get("form_name")), stats,
                                        out_names, list(range(len(self.level_values))) if v["kind"] == "level" else []))
        self.level_pos = {i: i for i in range(len(self.level_values))}

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
                        "kind": "level" if plan.level_field and len(plan.levels) and m.level_type not in
                        ("surface", "heightAboveGround", "meanSea", "entireAtmosphere") else "time",
                        "form_name": mapping.get(p)})
        return out

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
    def save(self, job_dir: Path, variables: list[dict]) -> None:
        np.savez(Path(job_dir) / "grid.npz", lat=self.grid.lat, lon=self.grid.lon)
        (Path(job_dir) / "output.json").write_text(json.dumps(
            {"grid_kind": self.grid.kind, "grid_attrs": self.grid.attrs, "variables": variables}, indent=1))

    @classmethod
    def load(cls, plan: CdsPlan, job_dir: Path) -> "CdsOutput | None":
        p, g = Path(job_dir) / "output.json", Path(job_dir) / "grid.npz"
        if not (p.exists() and g.exists()):
            return None
        d = json.loads(p.read_text())
        arr = np.load(g)
        out = cls(plan, Grid(d["grid_kind"], arr["lat"], arr["lon"], d.get("grid_attrs") or {}), d["variables"])
        out.variables_raw = d["variables"]
        return out
