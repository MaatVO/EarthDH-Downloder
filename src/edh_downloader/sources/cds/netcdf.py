"""Read the NetCDF files the Climate Data Store delivers (the products it has no GRIB for), with bounded memory.

``unpack`` turns a result (a NetCDF file, or a zip / tar of them) into NetCDF files. ``scan`` reads only the
metadata and the time coordinates of each file: which variables are gridded fields, on which grid, at which times
and levels. ``Reader`` then reads runs of time steps of one variable and level, so a large file is read a slab at
a time.

A field this app can store has a time axis (a dimension, or one time per file), a horizontal grid (1-D latitude
and longitude, or a projected grid with 2-D coordinates) and at most one more dimension of size > 1 (pressure
levels, depths, ensemble members ...). Anything else (tables, station series, two products for the same variable
and time) raises ``NotGridded``: the job then keeps the files as the CDS delivered them.

netCDF4 is loaded only here: Earth Data Hub works without it.
"""

import os
import shutil
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .grib import Grid

LAT_UNITS = {"degrees_north", "degree_north", "degree_n", "degrees_n", "degreen", "degreesn"}
LON_UNITS = {"degrees_east", "degree_east", "degree_e", "degrees_e", "degreee", "degreese"}
LAT_NAMES = {"latitude", "lat", "nav_lat", "lats"}
LON_NAMES = {"longitude", "lon", "nav_lon", "lons", "long"}
BOUNDS = ("bnds", "bounds", "vertices", "nv")
REAL_CALENDARS = {"standard", "gregorian", "proleptic_gregorian"}
KEEP_ATTRS = ("long_name", "standard_name", "units", "comment", "cell_methods")
MAX_REASONS = 6


class NotGridded(Exception):
    """The files are not gridded fields this app can store: they are kept as delivered."""


def _netcdf4():
    try:
        import netCDF4
    except Exception as e:  # noqa: BLE001  (missing wheel or library)
        raise RuntimeError(f"reading NetCDF needs the netCDF4 package, which could not be loaded: {e}") from e
    return netCDF4


def available() -> None:
    """Raise RuntimeError when NetCDF files cannot be read here (checked before any request is queued)."""
    _netcdf4()
    import cftime  # noqa: F401  (installed with netCDF4)


# ------------------------------------------------------------------ results → NetCDF files

def kind_of(path: Path) -> str:
    """``netcdf``, ``zip``, ``tar``, ``gzip`` or ``other``, from the first bytes (names lie: ``.nc`` zips exist)."""
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:3] == b"CDF" or head[:4] == b"\x89HDF":
        return "netcdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        pass
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    return "other"


def _extract(path: Path, into: Path) -> None:
    """Members of an archive as numbered flat files (never the archive's own paths: no file lands outside)."""
    k = kind_of(path)
    if k == "zip":
        with zipfile.ZipFile(path) as z:
            for i, info in enumerate(m for m in z.infolist() if not m.is_dir()):
                with z.open(info) as src, open(into / f"{i:04d}_{Path(info.filename).name}", "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    elif k == "tar":
        with tarfile.open(path, "r:*") as t:
            for i, m in enumerate(m for m in t.getmembers() if m.isfile()):
                src = t.extractfile(m)
                if src is None:
                    continue
                with src, open(into / f"{i:04d}_{Path(m.name).name}", "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    elif k == "gzip":
        import gzip

        with gzip.open(path, "rb") as src, open(into / Path(path).stem, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    # archives inside the archive (a zip of yearly zips)
    for p in sorted(into.iterdir()):
        if p.is_file() and kind_of(p) in ("zip", "tar", "gzip"):
            sub = into / f"{p.name}.d"
            sub.mkdir()
            _extract(p, sub)
            p.unlink()


def unpack(path: Path, dest: Path) -> list[Path]:
    """The NetCDF files of one result: the file itself, or the members of an archive extracted into ``dest``
    (once: a complete extraction is reused on resume). Other members (README, CSV, PDF) are left out."""
    path = Path(path)
    k = kind_of(path)
    if k == "netcdf":
        return [path]
    if k == "other":
        return []
    dest = Path(dest)
    if not (dest / ".complete").exists():
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.mkdir(parents=True)
        _extract(path, tmp)
        (tmp / ".complete").touch()
        os.replace(tmp, dest)
    return sorted(p for p in dest.rglob("*") if p.is_file() and p.name != ".complete" and kind_of(p) == "netcdf")


# ------------------------------------------------------------------ what a file holds

@dataclass
class Field:
    """One variable of one file."""
    file: Path
    var: str
    dims: tuple  # the variable's dimensions, in file order
    tdim: str | None  # None: one time for the whole file (a scalar time coordinate)
    ydim: str
    xdim: str
    level: str | None  # the one other dimension of size > 1
    times: pd.DatetimeIndex
    levels: np.ndarray | None
    attrs: dict
    level_attrs: dict = field(default_factory=dict)
    grid_key: tuple = ()  # the latitude and longitude variables of its grid


@dataclass
class Scan:
    grid: Grid
    fields: dict[str, list[Field]]  # per variable, in file order
    times: pd.DatetimeIndex  # every time of every variable, sorted
    level_dim: str | None
    levels: list
    level_attrs: dict
    skipped: dict[str, str]  # variable -> why it is not stored
    notes: list[str]


def _attr(v, name: str, default=""):
    try:
        return v.getncattr(name)
    except (AttributeError, KeyError):
        return default


def _attrs(v) -> dict:
    out = {}
    for k in v.ncattrs():
        if k.startswith("_") or k in ("missing_value", "scale_factor", "add_offset", "valid_range", "valid_min",
                                      "valid_max", "coordinates", "grid_mapping", "bounds", "chunksizes"):
            continue
        val = v.getncattr(k)
        if isinstance(val, np.ndarray):
            val = val.tolist()
        elif isinstance(val, np.generic):
            val = val.item()
        if isinstance(val, (str, int, float, list, bool)):
            out[k] = val
    return out


def _is_lat(name: str, v) -> bool:
    return (_attr(v, "standard_name") == "latitude" or str(_attr(v, "units")).lower().replace(" ", "") in LAT_UNITS
            or name.lower() in LAT_NAMES)


def _is_lon(name: str, v) -> bool:
    return (_attr(v, "standard_name") == "longitude" or str(_attr(v, "units")).lower().replace(" ", "") in LON_UNITS
            or name.lower() in LON_NAMES)


def _is_time(name: str, v) -> bool:
    return " since " in str(_attr(v, "units")) and v.ndim <= 1 and not any(b in name.lower() for b in BOUNDS)


def decode_times(v) -> tuple[pd.DatetimeIndex, str]:
    """CF times as timestamps, and the calendar. Other calendars (noleap, 360_day) are mapped date by date onto
    the standard one (day 30 of February becomes its last day)."""
    import cftime

    raw = np.ma.getdata(v[:]).ravel()
    cal = str(_attr(v, "calendar", "standard") or "standard").lower()
    dates = cftime.num2date(raw, str(_attr(v, "units")), cal, only_use_cftime_datetimes=True)
    out = []
    for d in np.atleast_1d(dates):
        last = pd.Period(year=d.year, month=d.month, freq="M").days_in_month
        out.append(pd.Timestamp(d.year, d.month, min(d.day, last), d.hour, d.minute, d.second))
    return pd.DatetimeIndex(out), cal


def _file_fields(path: Path) -> tuple[list[Field], dict[str, str], dict[str, Grid], set[str]]:
    nc4 = _netcdf4()
    fields, skipped, grids, calendars = [], {}, {}, set()
    with nc4.Dataset(path) as ds:
        vs = ds.variables
        plain = {n: v for n, v in vs.items() if not any(b in n.lower() for b in BOUNDS)}
        lats = {n: v for n, v in plain.items() if _is_lat(n, v) and v.ndim in (1, 2)}
        lons = {n: v for n, v in plain.items() if _is_lon(n, v) and v.ndim in (1, 2)}
        times = {n: v for n, v in vs.items() if _is_time(n, v)}
        coord_like = set(lats) | set(lons) | set(times) | set(ds.dimensions)
        for name, v in vs.items():
            dims = tuple(v.dimensions)
            if name in coord_like or v.ndim < 2 or any(b in d.lower() for d in dims for b in BOUNDS) \
                    or getattr(v.dtype, "kind", "O") not in "fiu":
                continue
            # the horizontal grid: 1-D latitude and longitude along two of its dimensions, or 2-D coordinates
            ydim = xdim = None
            gkey = None
            for ln, lv in lats.items():
                for on, ov in lons.items():
                    if lv.ndim == 1 and ov.ndim == 1 and lv.dimensions[0] in dims and ov.dimensions[0] in dims \
                            and lv.dimensions[0] != ov.dimensions[0]:
                        ydim, xdim, gkey = lv.dimensions[0], ov.dimensions[0], (ln, on)
                    elif lv.ndim == 2 and tuple(lv.dimensions) == tuple(ov.dimensions) \
                            and set(lv.dimensions) <= set(dims):
                        ydim, xdim, gkey = lv.dimensions[0], lv.dimensions[1], (ln, on)
                    if gkey:
                        break
                if gkey:
                    break
            if gkey is None:
                skipped[name] = "no latitude/longitude grid"
                continue
            if gkey not in grids:
                lv, ov = vs[gkey[0]], vs[gkey[1]]
                lat = np.asarray(np.ma.filled(lv[:].astype("f8"), np.nan))
                lon = np.asarray(np.ma.filled(ov[:].astype("f8"), np.nan))
                if lat.ndim == 1:
                    grids[gkey] = Grid("regular", np.round(lat, 6), np.round(lon, 6))
                else:
                    gm = _attr(v, "grid_mapping")
                    attrs = {"grid_type": "projected"}
                    if gm and gm in vs:
                        attrs.update({k: val for k, val in _attrs(vs[gm]).items()
                                      if isinstance(val, (int, float, str))})
                    grids[gkey] = Grid("curvilinear", lat, lon, attrs)
            # time: a dimension of the variable, or one time for the whole file
            tdim = next((d for d in dims if d in times), None)
            if tdim is not None:
                tv = times[tdim]
            else:
                tv = next((times[c] for c in str(_attr(v, "coordinates")).split() if c in times
                           and times[c].size == 1), None)
                tv = tv if tv is not None else next((t for t in times.values() if t.size == 1), None)
            if tv is None:
                skipped[name] = "no time axis"
                continue
            t, cal = decode_times(tv)
            calendars.add(cal)
            rest = [d for d in dims if d not in (tdim, ydim, xdim)]
            big = [d for d in rest if len(ds.dimensions[d]) > 1]
            if len(big) > 1:
                skipped[name] = f"more dimensions than a store holds ({', '.join(big)})"
                continue
            level = big[0] if big else None
            levels, level_attrs = None, {}
            if level is not None:
                if level in vs:
                    levels = np.asarray(np.ma.getdata(vs[level][:])).ravel()
                    level_attrs = _attrs(vs[level])
                else:
                    levels = np.arange(len(ds.dimensions[level]))
            attrs = {k: val for k, val in _attrs(v).items() if k in KEEP_ATTRS}
            fields.append(Field(Path(path), name, dims, tdim, ydim, xdim, level, t, levels, attrs, level_attrs,
                                gkey))
    return fields, skipped, grids, calendars


def _same_grid(a: Grid, b: Grid) -> bool:
    return a.kind == b.kind and a.lat.shape == b.lat.shape and a.lon.shape == b.lon.shape and \
        np.allclose(a.lat, b.lat, atol=1e-5, equal_nan=True) and np.allclose(a.lon, b.lon, atol=1e-5, equal_nan=True)


def scan(files: list[Path], progress=None) -> Scan:
    """What a set of files holds, checked to fit one store: one grid, one level axis, one value per variable,
    level and time. Variables that do not fit are left out (``skipped`` says why); none fitting raises
    ``NotGridded``."""
    per_file, skipped, calendars = [], {}, set()
    for n, p in enumerate(files):
        if progress:
            progress(n, len(files))
        try:
            fl, sk, grids, cals = _file_fields(p)
        except OSError as e:
            raise NotGridded(f"{Path(p).name} cannot be read as NetCDF ({e})") from None
        per_file.append((fl, grids))
        skipped.update(sk)
        calendars |= cals
    candidates = [(f, grids[f.grid_key]) for fl, grids in per_file for f in fl]
    if not candidates:
        why = "; ".join(f"{k}: {v}" for k, v in list(skipped.items())[:MAX_REASONS])
        raise NotGridded("the files hold no gridded field" + (f" ({why})" if why else ""))
    # one grid: the one most fields are on
    shapes = Counter((g.kind, g.lat.shape) for _, g in candidates)
    main = next(g for _, g in candidates if (g.kind, g.lat.shape) == shapes.most_common(1)[0][0])
    off_grid = {f.var for f, g in candidates if not _same_grid(g, main)}
    for v in off_grid:
        skipped[v] = "on another grid than the other variables"
    kept = [f for f, _ in candidates if f.var not in off_grid]
    # one level axis: the first one seen
    lv = next((f for f in kept if f.level is not None), None)
    level_dim = lv.level if lv else None
    levels = lv.levels.tolist() if lv else []
    for f in kept:
        if f.level is not None and (f.level != level_dim or len(f.levels) != len(levels)
                                    or not np.allclose(f.levels, levels)):
            skipped[f.var] = f"levels ({f.level}) differ from {level_dim}"
    bad_level = {f.var for f in kept if f.level is not None and skipped.get(f.var, "").startswith("levels")}
    kept = [f for f in kept if f.var not in bad_level]
    if not kept:
        raise NotGridded("no variable fits one store: " + "; ".join(f"{k}: {v}" for k, v in
                                                                   list(skipped.items())[:MAX_REASONS]))
    by_var: dict[str, list[Field]] = {}
    for f in kept:
        by_var.setdefault(f.var, []).append(f)
    for var, fl in by_var.items():
        seen = Counter(t for f in fl for t in f.times)
        dup = [t for t, n in seen.items() if n > 1]
        if dup:
            raise NotGridded(f"the files hold {seen[dup[0]]} values of {var} for {dup[0]} (several products, "
                             "members or versions): one store holds one value per variable and time")
    times = pd.DatetimeIndex(sorted({t for fl in by_var.values() for f in fl for t in f.times}))
    notes = []
    odd = calendars - REAL_CALENDARS
    if odd:
        notes.append(f"the files use the {', '.join(sorted(odd))} calendar: dates are stored on the standard "
                     "calendar (the 30th of February becomes its last day)")
    for var, why in skipped.items():
        if var not in by_var:
            notes.append(f"{var} not stored: {why}")
    return Scan(main, by_var, times, level_dim, levels, lv.level_attrs if lv else {}, skipped, notes)


# ------------------------------------------------------------------ reading

class Reader:
    """Read runs of time steps through files kept open while a unit is processed."""

    def __init__(self):
        self.nc4 = _netcdf4()
        self.open: dict[Path, object] = {}

    def _ds(self, path: Path):
        ds = self.open.get(path)
        if ds is None:
            ds = self.open[path] = self.nc4.Dataset(path)
        return ds

    def read(self, f: Field, p0: int, p1: int, level_index: int | None) -> np.ndarray:
        """Steps ``[p0, p1)`` of a field (one level) as float32 ``(steps, ny, nx)``, missing values as NaN."""
        v = self._ds(f.file).variables[f.var]
        idx, kept = [], []
        for d in f.dims:
            if d == f.tdim:
                idx.append(slice(p0, p1))
                kept.append(d)
            elif d == f.level:
                idx.append(int(level_index))
            elif d in (f.ydim, f.xdim):
                idx.append(slice(None))
                kept.append(d)
            else:
                idx.append(0)  # a dimension of size 1
        a = np.ma.asarray(v[tuple(idx)])
        a = np.ma.filled(a.astype(np.float32), np.nan)
        order = [kept.index(d) for d in (f.tdim, f.ydim, f.xdim) if d is not None]
        a = np.transpose(a, order)
        if f.tdim is None:
            a = a[None]
        return a

    def close(self):
        for ds in self.open.values():
            try:
                ds.close()
            except Exception:  # noqa: BLE001
                pass
        self.open.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
