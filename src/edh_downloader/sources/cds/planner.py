"""Turn a CDS job spec into the fewest Climate Data Store requests that deliver exactly the selection.

On the CDS the price of a request is mostly its turn in the queue (a 1-field request was seen waiting almost
three hours, then running for two seconds), so the planner minimises the *number* of requests:

* every selected variable and level goes into the same request;
* whole months are packed together (several months of one year, then whole years) as long as a request stays
  under the dataset's cost limit (``costing``: number of fields) and under ``max_request_gb``;
* a request is split only when one month alone is too big: by variables first, then levels, then days;
* only the hours the job needs are requested (all of them when aggregating; every N hours for "first" values);
* the area is cut and the grid changed on the CDS side when the dataset allows it (``grid`` as a string: a list
  would count twice in the cost).

A request is a cartesian product (years × months × days × times × variables × levels), so packed months share one
day list: days a month does not have (31 April) are ignored by the CDS, and not counted in its cost. Months only partly selected (the first and
last of the period) are requested on their own, with just their days.

``CdsSpec.pack = "month"`` keeps one month per request instead (ECMWF's general advice for tape-archived data).
"""

import math
from dataclasses import dataclass, field

import pandas as pd

from ...job import JobSpec
from ...planner import PlanError
from .client import canonical, request_hash

DEFAULT_LIMIT = 60_000.0  # until costing tells the dataset's own (ERA5 single levels: 121 000; pressure: 60 000)
BYTES_PER_POINT = 2  # GRIB, 16-bit simple packing (as delivered by CDS for ERA5 and CERRA)
FIELD_OVERHEAD = 200  # GRIB headers per field
# whole-domain grid points of datasets without an area field (cut locally after download)
NATIVE_POINTS = {"reanalysis-cerra": 1069 * 1069}


def _lon180(lon: float) -> float:
    return lon if -180 <= lon <= 180 else round(((lon + 180) % 360) - 180, 6)


def _num(x: float):
    """A coordinate as the CDS form writes it: 47 rather than 47.0 (the same request, the same cache entry)."""
    x = round(float(x), 6)
    return int(x) if x.is_integer() else x


def _level_str(v) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else str(v)


@dataclass
class CdsRequest:
    index: int
    inputs: dict  # canonical: exactly what is submitted
    hash: str
    start: str  # first and last UTC instant it covers
    end: str
    fields: int  # local cost estimate (number of fields)
    variables: list[str]
    levels: list[str]
    bytes_est: int | None = None
    cost: float | None = None  # from the CDS costing, when checked

    def to_dict(self) -> dict:
        return {"index": self.index, "inputs": self.inputs, "hash": self.hash, "start": self.start,
                "end": self.end, "fields": self.fields, "variables": self.variables, "levels": self.levels,
                "bytes_est": self.bytes_est, "cost": self.cost}


@dataclass
class _Block:
    """One time slice of the request space: years × months × days × times (a cartesian product)."""
    years: list[str]
    months: list[str] | None
    days: list[str] | None
    times: list[str] | None
    start: pd.Timestamp
    end: pd.Timestamp
    full: bool = True  # a whole month (or year): can be packed with its neighbours

    def n(self) -> int:
        """Time steps delivered: like the CDS costing, only real dates count (no 31 April)."""
        nt = len(self.times or [None])
        if not self.days or not self.months:
            return len(self.years) * len(self.months or [None]) * nt
        days = [int(d) for d in self.days]
        total = 0
        for y in self.years:
            for m in self.months:
                last = pd.Period(f"{y}-{m}").days_in_month
                total += sum(1 for d in days if d <= last)
        return total * nt


@dataclass
class CdsPlan:
    spec: JobSpec
    desc: dict  # catalog.describe()
    table: list = field(default_factory=list)  # catalog.constraints()
    limit: float = DEFAULT_LIMIT
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self):
        s, d = self.spec, self.desc
        if d.get("tier") == "table":
            raise PlanError("in-situ observation tables cannot be converted to a gridded store")
        self.dataset = s.dataset
        self._variables()
        self._options()
        self._space()
        self.instants = self._instants()
        self.requests: list[CdsRequest] = []
        self._build()

    # ------------------------------------------------------------ selection
    def _variables(self):
        s, d = self.spec, self.desc
        names = {v["name"] for v in d["variables"]}
        if names and not s.variables:
            raise PlanError("select at least one variable")
        unknown = [v for v in s.variables if names and v not in names]
        if unknown:
            raise PlanError(f"unknown variables for {s.dataset}: {', '.join(unknown)}")
        self.variables = sorted(set(s.variables))
        self.level_field = (d.get("levels") or {}).get("field")
        self.levels: list[str] = []
        if self.level_field:
            avail = d["levels"]["values"]
            wanted = [_level_str(l) for l in s.levels] if s.levels else list(avail)
            missing = [l for l in wanted if l not in avail]
            if missing:
                raise PlanError(f"levels {missing} not available; available: {avail}")
            self.levels = sorted(set(wanted), key=lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0)

    def _options(self):
        """Dataset options: the job's choices, else the form default, else the only value. A required option
        without a value is an error (the CDS would refuse the request)."""
        chosen = dict(self.spec.cds.options or {})
        self.options: dict = {}
        self.option_mult = 1
        for o in self.desc.get("options", []):
            n = o["name"]
            v = chosen.pop(n, None)
            if v in (None, [], ""):
                if not self._needed(n, chosen):  # a default for another kind of request (soil layer, lead time)
                    continue
                v = o["default"] or (o["values"][:1] if len(o["values"]) == 1 else [])
            if v in (None, [], ""):
                if o["required"] and self._needed(n, chosen):
                    raise PlanError(f"choose {o['label'].lower()} ({n})")
                continue
            vals = v if isinstance(v, list) else [v]
            bad = [x for x in vals if o["values"] and str(x) not in o["values"]]
            if bad:
                raise PlanError(f"{o['label']}: {bad} not available; choose from {o['values'][:12]}")
            if o["multiple"]:
                self.options[n] = sorted({str(x) for x in vals})
                self.option_mult *= len(self.options[n])
            else:
                if len(vals) > 1:
                    raise PlanError(f"{o['label']}: choose one value")
                self.options[n] = str(vals[0])
        if chosen:
            raise PlanError(f"unknown options for {self.dataset}: {', '.join(chosen)}")
        if self.desc.get("format_field"):
            fmts = self.desc["formats"]
            self.options[self.desc["format_field"]] = "grib" if "grib" in fmts else fmts[0]
        if self.desc.get("download_format"):
            self.options["download_format"] = "unarchived"

    def _needed(self, field_name: str, chosen: dict) -> bool:
        """Whether a field matters for this selection: the constraints entries that fit the variables and the
        options chosen so far mention it (a forecast lead time is not asked for analyses). Without
        constraints, every required field is needed."""
        if not self.table:
            return True
        sel = {"variable": self.variables, **{k: v for k, v in {**self.options, **chosen}.items() if v}}

        def fits(e):
            for k, v in sel.items():
                if k in e:
                    want = {str(x) for x in (v if isinstance(v, list) else [v])}
                    if not want & {str(x) for x in e[k]}:
                        return False
            return True
        return any(field_name in e for e in self.table if fits(e))

    def _space(self):
        a, d = self.spec.area, self.desc
        self.area: list[float] | None = None  # N, W, S, E as sent to CDS
        self.grid: str | None = None
        grid = self.spec.cds.grid
        native = (d.get("area") or {}).get("step") or [None, None]
        if a.mode == "points":
            if not a.points:
                raise PlanError("add at least one point")
            lats, lons = [p.lat for p in a.points], [_lon180(p.lon) for p in a.points]
            pad = max(x or 0 for x in (grid or native)) or 0.25
            box = (max(lats) + pad, min(lons) - pad, min(lats) - pad, max(lons) + pad)
        elif a.mode == "bbox":
            if a.south > a.north:
                raise PlanError("south must be <= north")
            west, east = a.west, a.east
            if east < west:  # crosses the antimeridian: CDS accepts east beyond 180
                east += 360
            box = (a.north, west, a.south, east)
        elif a.mode == "global":
            box = None
        else:
            raise PlanError(f"unknown area mode {a.mode}")
        if box is not None and d.get("area") is not None:
            # on the grid, as the CDS does: inward for a box (only the points inside it), outward around points
            sy, sx = (float(grid[0]), float(grid[-1])) if grid else (native[0] or 0, native[1] or 0)
            if sy and sx:
                inward = a.mode == "bbox"
                lo, hi = (math.ceil, math.floor) if inward else (math.floor, math.ceil)
                box = (hi(box[0] / sy + 1e-9) * sy if inward else hi(box[0] / sy - 1e-9) * sy,
                       lo(box[1] / sx - 1e-9) * sx if inward else lo(box[1] / sx + 1e-9) * sx,
                       lo(box[2] / sy - 1e-9) * sy if inward else lo(box[2] / sy + 1e-9) * sy,
                       hi(box[3] / sx + 1e-9) * sx if inward else hi(box[3] / sx - 1e-9) * sx)
                if box[0] < box[2] or box[3] < box[1]:
                    raise PlanError("the box is smaller than one grid cell: make it larger")
            self.area = [_num(x) for x in box]
        elif box is not None:
            self.warnings.append("this dataset is delivered for its whole domain: the area is cut after the "
                                 "download, so the download size does not depend on it")
        if grid:
            if d.get("area") is None or d.get("tier") != "grib":
                raise PlanError("this dataset cannot be regridded by the CDS (only its native grid)")
            dlat, dlon = (float(grid[0]), float(grid[-1]))
            if dlat <= 0 or dlon <= 0:
                raise PlanError("grid spacing must be positive")
            self.grid = f"{dlon:g}/{dlat:g}"  # MARS order: east-west increment first
        # grid points per field (for the size estimate); None when unknown
        self.points = None
        step = [float(grid[0]), float(grid[-1])] if grid else native
        if d.get("area") is not None and step[0] and step[1]:
            n, w, s_, e = self.area or (90, -180, -90, 180 - step[1])
            self.points = (math.floor((n - s_) / step[0] + 1e-9) + 1) * (math.floor((e - w) / step[1] + 1e-9) + 1)
        else:
            self.points = next((p for k, p in NATIVE_POINTS.items() if self.dataset.startswith(k)), None)

    # ------------------------------------------------------------ time
    def _instants(self) -> pd.DatetimeIndex | None:
        """Every UTC instant the job needs (None for datasets without a time selection)."""
        t, d = self.spec.time, self.desc
        kind = d["time"]["kind"]
        if kind in ("none", "date"):
            return None
        if not (0 <= t.start_hour <= 23 and 0 <= t.end_hour <= 23):
            raise PlanError("the hours must be between 0 and 23")
        off = pd.Timedelta(hours=t.tz_offset)
        start = pd.Timestamp(t.start) + pd.Timedelta(hours=t.start_hour)
        end = pd.Timestamp(t.end) + pd.Timedelta(hours=t.end_hour + 1)
        period = {"day": "D", "month": "MS", "year": "YS"}.get(t.freq)
        if period == "D":
            start, end = start.floor("D"), end.ceil("D")
        elif period:
            start = start.to_period(period[0]).to_timestamp()
            end = (end - pd.Timedelta(seconds=1)).to_period(period[0]).to_timestamp() + pd.tseries.frequencies.to_offset(period)
        if start >= end:
            raise PlanError("start must be before end")
        if kind == "ymd":  # every step of the dataset whose local time is in the period
            times = d["time"]["times"] or ["00:00"]
            days = pd.date_range((start - off).floor("D"), (end - off).ceil("D"), freq="D")
            inst = pd.DatetimeIndex([day + pd.Timedelta(hours=int(h[:2]), minutes=int(h[3:5]))
                                     for day in days for h in times])
            inst = inst[(inst + off >= start) & (inst + off < end)]
        elif kind == "ym":  # monthly products: the months the period touches
            inst = pd.date_range(start.to_period("M").to_timestamp(), end, freq="MS", inclusive="left")
        else:  # yearly products
            inst = pd.date_range(f"{start.year}-01-01", end, freq="YS", inclusive="left")
        if t.freq == "hours" and kind == "ymd":
            if t.hours < 1:
                raise PlanError("hours must be >= 1")
            firsts = all(set(self.spec.time.stats.get(v) or ["mean"]) == {"first"} for v in self.variables)
            if firsts:  # only the first value of each period is kept: fetch only those instants
                lab = (inst + off).floor(f"{t.hours}h")
                keep = pd.Series(range(len(inst))).groupby(lab.values).first().values
                inst = inst[keep]
        # the catalogue's coverage
        cov0 = pd.Timestamp(self.desc.get("start")) if self.desc.get("start") else None
        cov1 = pd.Timestamp(self.desc.get("end")) if self.desc.get("end") else None
        if cov0 is not None:
            cov0 = cov0.tz_localize(None) if cov0.tzinfo else cov0
        if cov1 is not None:
            cov1 = cov1.tz_localize(None) if cov1.tzinfo else cov1
            cov1 = cov1 + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        n = len(inst)
        if cov0 is not None:
            inst = inst[inst >= cov0]
        if cov1 is not None:
            inst = inst[inst <= cov1]
        if len(inst) < n:
            self.warnings.append(f"the period was clipped to the available data ({str(cov0)[:10]} .. {str(cov1)[:10]})")
        if not len(inst):
            raise PlanError("no data in the selected period")
        years = set(d["time"].get("years") or [])
        if years and not {str(y) for y in inst.year} <= years:
            missing = sorted({str(y) for y in inst.year} - years)
            raise PlanError(f"years {missing[:5]} are not available for this dataset")
        return inst

    def _blocks(self) -> list[_Block]:
        kind = self.desc["time"]["kind"]
        inst = self.instants
        if kind == "ymd":
            out = []
            for (y, m), g in pd.Series(inst, index=inst).groupby([inst.year, inst.month]):
                idx = pd.DatetimeIndex(g.values)
                days = sorted({f"{x.day:02d}" for x in idx})
                times = sorted({f"{x.hour:02d}:{x.minute:02d}" for x in idx})
                ndays = pd.Period(f"{y}-{m:02d}").days_in_month
                full = len(days) == ndays and len(idx) == ndays * len(times)  # every day, every time
                out.append(_Block([str(y)], [f"{m:02d}"], days, times, idx[0], idx[-1], full))
            return out
        if kind == "ym":
            out = []
            for y, g in pd.Series(inst, index=inst).groupby(inst.year):
                idx = pd.DatetimeIndex(g.values)
                months = sorted({f"{x.month:02d}" for x in idx})
                times = (self.desc["time"]["times"] or [None])[:1] if self.desc["time"]["times"] else None
                out.append(_Block([str(y)], months, None, times, idx[0], idx[-1], len(months) == 12))
            return out
        years = sorted({str(x.year) for x in inst})
        return [_Block([y], None, None, None, pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31"), True)
                for y in years]

    # ------------------------------------------------------------ packing
    def _cap_fields(self) -> int:
        cap = self.limit
        per = self.bytes_per_field()
        if per:
            cap = min(cap, self.spec.cds.max_request_gb * 1e9 / per)
        return max(1, int(cap))

    def _cap_steps(self) -> int:
        """Time steps one request may cover: one variable and level of it, decoded to float32 over the whole
        delivered grid, must fit a third of the job's memory budget (the processor holds one such slab)."""
        if not self.points:
            return 10 ** 9
        return max(1, int(self.spec.download.memory_gb * 1e9 / 3 // (self.points * 4)))

    def bytes_per_field(self) -> int | None:
        if self.points is None or self.desc.get("tier") != "grib":
            return None
        return self.points * BYTES_PER_POINT + FIELD_OVERHEAD

    def _var_groups(self, per_var_level: int) -> list[tuple[list[str], list[str]]]:
        """Variables and levels that fit one request of ``per_var_level`` fields per variable and level."""
        cap = self._cap_fields()
        nlev = max(1, len(self.levels))
        vars_ = self.variables or [None]
        if per_var_level * nlev * len(vars_) <= cap:
            return [(self.variables, self.levels)]
        if per_var_level * nlev <= cap:  # whole variables, as many as fit
            k = max(1, cap // (per_var_level * nlev))
            return [(vars_[i:i + k], self.levels) for i in range(0, len(vars_), k)]
        k = max(1, cap // per_var_level)  # one variable at a time, levels in groups
        return [([v], self.levels[i:i + k]) for v in vars_ for i in range(0, nlev, k)]

    def _build(self):
        s = self.spec
        base = dict(self.options)
        kind = self.desc["time"]["kind"]
        if self.area:
            base["area"] = self.area
        if self.grid:
            base["grid"] = self.grid
        if kind in ("none", "date") or self.instants is None:
            if kind == "date":
                base["date"] = f"{s.time.start}/{s.time.end}"
            if self.desc.get("location"):
                # time-series datasets: one request per point. Format as in the CDS examples (not yet checked live)
                if s.area.mode != "points" or not s.area.points:
                    raise PlanError("this dataset gives time series for points: choose points as the area")
                ndays = (pd.Timestamp(s.time.end) - pd.Timestamp(s.time.start)).days + 1
                for p in s.area.points:
                    req = dict(base, location={"latitude": p.lat, "longitude": _lon180(p.lon)})
                    self._add(req, self.variables, self.levels, s.time.start, s.time.end,
                              len(self.variables or [None]) * ndays)
                return
            self._add(dict(base), self.variables, self.levels, s.time.start, s.time.end, 1)
            return
        blocks = self._blocks()
        per_block = max(b.n() for b in blocks) * self.option_mult
        groups = self._var_groups(per_block)
        cap = self._cap_fields()
        for vars_, levs in groups:
            per_vl = len(vars_ or [None]) * max(1, len(levs)) * self.option_mult
            for packed in self._pack(blocks, per_vl, cap):
                for b in self._split_days(packed, per_vl, cap):
                    req = dict(base, year=b.years)
                    if b.months:
                        req["month"] = b.months
                    if b.days:
                        req["day"] = b.days
                    if b.times:
                        req["time"] = b.times
                    self._add(req, vars_, levs, str(b.start), str(b.end), b.n() * per_vl)
        self.requests.sort(key=lambda r: (r.start, r.variables, r.levels))
        for i, r in enumerate(self.requests):
            r.index = i

    def _pack(self, blocks: list[_Block], per_vl: int, cap: int) -> list[_Block]:
        """Merge neighbouring whole months (then whole years) while the request stays under ``cap``."""
        if self.spec.cds.pack == "month" or self.desc["time"]["kind"] != "ymd":
            merged = []
            for b in blocks:  # ym / y: whole years merge when they fit
                if (merged and b.full and merged[-1].full and self.desc["time"]["kind"] != "ymd"
                        and merged[-1].months == b.months
                        and (len(merged[-1].years) + 1) * len(b.months or [None]) * len(b.times or [None])
                        * per_vl <= cap):
                    merged[-1] = _Block(merged[-1].years + b.years, b.months, None, b.times, merged[-1].start,
                                        b.end, True)
                else:
                    merged.append(b)
            return merged
        out: list[_Block] = []
        for b in blocks:
            last = out[-1] if out else None
            if last is not None and b.full and last.full and b.times == last.times:
                days = sorted(set(last.days) | set(b.days))
                if last.years == b.years:  # months of one year
                    cand = _Block(last.years, last.months + b.months, days, b.times, last.start, b.end, True)
                elif last.months == [f"{m:02d}" for m in range(1, 13)] and b.months == ["01"]:
                    cand = None  # a new year starts: handled below by whole-year merging
                else:
                    cand = None
                if cand is not None and cand.n() * per_vl <= cap and cand.n() <= self._cap_steps():
                    out[-1] = cand
                    continue
            out.append(b)
        # whole years with the same months, days and times merge too
        years: list[_Block] = []
        for b in out:
            last = years[-1] if years else None
            if (last is not None and b.full and last.full and last.months == b.months == [f"{m:02d}" for m in range(1, 13)]
                    and last.days == b.days and last.times == b.times
                    and int(b.years[0]) == int(last.years[-1]) + 1):
                cand = _Block(last.years + b.years, b.months, b.days, b.times, last.start, b.end, True)
                if cand.n() * per_vl <= cap and cand.n() <= self._cap_steps():
                    years[-1] = cand
                    continue
            years.append(b)
        return years

    def _split_days(self, b: _Block, per_vl: int, cap: int) -> list[_Block]:
        if (b.n() * per_vl <= cap and b.n() <= self._cap_steps()) or not b.days:
            return [b]
        if len(b.years) > 1 or len(b.months or []) > 1:  # cannot happen: packing respects the cap
            return [b]
        per_day = len(b.times or [None]) * per_vl
        k = max(1, min(cap // per_day, self._cap_steps() // len(b.times or [None])))
        out = []
        for i in range(0, len(b.days), k):
            days = b.days[i:i + k]
            out.append(_Block(b.years, b.months, days, b.times,
                              b.start if i == 0 else pd.Timestamp(f"{b.years[0]}-{b.months[0]}-{days[0]}"),
                              b.end if i + k >= len(b.days) else
                              pd.Timestamp(f"{b.years[0]}-{b.months[0]}-{days[-1]}") + pd.Timedelta(hours=23), False))
        return out

    def _add(self, req: dict, vars_: list[str], levs: list[str], start: str, end: str, fields: int):
        if vars_:
            req["variable"] = vars_
        if self.level_field and levs:
            req[self.level_field] = levs
        inputs = canonical(req)
        per = self.bytes_per_field()
        self.requests.append(CdsRequest(len(self.requests), inputs, request_hash(self.dataset, inputs), start, end,
                                        int(fields), list(vars_), list(levs),
                                        bytes_est=fields * per if per else None))

    # ------------------------------------------------------------ checks and summary
    def invalid(self) -> dict[int, dict]:
        """Requests that ask for values the dataset does not have (local check, free): ``{index: {field: values}}``."""
        from .catalog import invalid

        out = {}
        for r in self.requests:
            # ranges and places are not single values: the constraints cannot check them one by one
            bad = invalid(self.table, {k: v for k, v in r.inputs.items() if k not in ("area", "grid", "date", "location")})
            if bad:
                out[r.index] = bad
        return out

    def summary(self) -> dict:
        n = len(self.requests)
        fields = sum(r.fields for r in self.requests)
        sizes = [r.bytes_est for r in self.requests]
        biggest = max(self.requests, key=lambda r: r.cost if r.cost is not None else r.fields)
        return {
            "source": "cds",
            "tier": self.desc.get("tier"),
            "requests": n,
            "fields": fields,
            "download_bytes_est": sum(sizes) if all(s is not None for s in sizes) else None,
            "largest_request": {"fields": biggest.fields, "cost": biggest.cost, "limit": self.limit,
                                "bytes_est": biggest.bytes_est},
            "first": str(self.instants[0]) if self.instants is not None else None,
            "last": str(self.instants[-1]) if self.instants is not None else None,
            "steps": len(self.instants) if self.instants is not None else None,
            "area": self.area,
            "grid": self.grid,
            "points_per_field": self.points,
            "variables": self.variables,
            "levels": self.levels or None,
            "options": {k: v for k, v in self.options.items() if k not in ("download_format",)},
            "warnings": self.warnings,
            "pack": self.spec.cds.pack,
        }
