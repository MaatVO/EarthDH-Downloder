"""Icechunk output repository: layout, per-unit commits and export to a plain Zarr v3 store."""

import os

os.environ.setdefault("ICECHUNK_LOG", "error")

import asyncio
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path

import icechunk as ic
import numpy as np
import pandas as pd
import zarr
from zarr.codecs import BloscCodec

from . import __version__
from .planner import Plan



def compressors(o) -> list:
    from zarr.codecs import GzipCodec, ZstdCodec

    if o.compressor == "none":
        return []
    if o.compressor.startswith("blosc-"):
        return [BloscCodec(cname=o.compressor.split("-", 1)[1], clevel=int(_clip(o.clevel, 0, 9)), shuffle=o.shuffle)]
    if o.compressor == "zstd":
        return [ZstdCodec(level=int(_clip(o.clevel, 1, 22)))]
    if o.compressor == "gzip":
        return [GzipCodec(level=int(_clip(o.clevel, 1, 9)))]
    raise ValueError(f"unknown compressor {o.compressor}")


def _clip(v, lo, hi):
    return max(lo, min(hi, v))
KEEP_ATTRS = ("long_name", "units", "standard_name", "GRIB_paramId", "GRIB_shortName", "GRIB_cfVarName",
              "GRIB_stepType", "GRIB_typeOfLevel", "GRIB_name", "cds_variable")
CELL = {"hours": "{h} hours", "day": "1 day", "month": "1 month", "year": "1 year"}


def open_repo(path: Path) -> ic.Repository | None:
    st = ic.local_filesystem_storage(str(path))
    return ic.Repository.open(st) if ic.Repository.exists(st) else None


def create_repo(path: Path, plan: Plan, job_id: str) -> ic.Repository:
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = ic.Repository.create(ic.local_filesystem_storage(str(path)))
    s = repo.writable_session("main")
    g = zarr.open_group(s.store, mode="w", zarr_format=3, attributes=root_attrs(plan, job_id))
    _write_layout(g, plan)
    s.commit("initialize layout")
    return repo


def root_attrs(plan: Plan, job_id: str, status: str = "incomplete", extra: dict | None = None) -> dict:
    spec, ds = plan.spec, plan.ds
    a = {
        "title": f"{ds.info.get('title', spec.dataset)} - subset",
        "source": ds.url,
        "source_dataset": spec.dataset,
        "source_collection": spec.collection if spec.source == "edh" else spec.source,
        "source_updated": ds.info.get("updated", ""),
        "source_institution": ds.meta.attrs.get("institution", ""),
        "provider": getattr(plan, "provider", "Earth Data Hub (https://earthdatahub.destine.eu)"),
        "license": getattr(plan, "license", "CC-BY-4.0 (Copernicus ERA5; see the source dataset)"),
        "Conventions": "CF-1.8",
        "edh_job_id": job_id,
        "edh_status": status,
        "edh_downloader_version": __version__,
        "edh_job": spec.to_dict(),
        "edh_plan": {k: v for k, v in plan.summary().items() if k != "eta_s"},
        "history": f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} created by edh-downloader {__version__}",
    }
    for k, v in (spec.output.extra_attrs or {}).items():
        if not k.startswith("edh_"):
            a[k] = v
    if extra:
        a.update(extra)
    return a


def _time_encoding(plan: Plan, base_units: str | None = None):
    units = base_units or plan.ds.time_units
    _, _, origin = units.partition(" since ")
    origin = pd.Timestamp(origin)
    hours = (plan.labels - origin) / pd.Timedelta(hours=1)
    if "minutes" in units:
        minutes = (plan.labels - origin) / pd.Timedelta(minutes=1)
        return np.round(minutes).astype("i8"), f"minutes since {origin:%Y-%m-%d %H:%M:%S}"
    if np.allclose(hours, np.round(hours)):
        return np.round(hours).astype("i8"), f"hours since {origin:%Y-%m-%d %H:%M:%S}"
    minutes = (plan.labels - origin) / pd.Timedelta(minutes=1)
    return np.round(minutes).astype("i8"), f"minutes since {origin:%Y-%m-%d %H:%M:%S}"


def _write_layout(g: zarr.Group, plan: Plan) -> None:
    spec = plan.spec
    t = spec.time
    if plan.n_periods:
        vals, units = _time_encoding(plan)
        attrs = {"units": units, "calendar": "proleptic_gregorian", "standard_name": "time",
                 "axis": "T"}
        if t.freq != "none":
            attrs["long_name"] = "start of the aggregation period"
            if t.tz_offset:
                attrs["comment"] = (f"periods are cut in UTC{t.tz_offset:+d}; values are period starts in that "
                                    f"local time")
        a = g.create_array("valid_time", shape=vals.shape, dtype="i8", chunks=(len(vals),),
                           dimension_names=["valid_time"], attributes=attrs, fill_value=0)
        a[:] = vals
    if plan.has_levels:
        lv = np.asarray(plan.level_values)
        name = plan.ds.meta.level_dim
        level_attrs = getattr(plan, "level_attrs", None) or (
            {"units": "hPa", "long_name": "pressure level", "positive": "down"} if name == "isobaricInhPa"
            else {"long_name": name})
        a = g.create_array(name, shape=lv.shape, dtype=lv.dtype, chunks=lv.shape, dimension_names=[name],
                           attributes=level_attrs, fill_value=0)
        a[:] = lv
    lat_attrs = {"units": "degrees_north", "standard_name": "latitude", "long_name": "latitude"}
    lon_attrs = {"units": "degrees_east", "standard_name": "longitude", "long_name": "longitude"}
    if plan.points is not None:
        n = len(plan.points)
        a = g.create_array("point", shape=(n,), dtype="i4", chunks=(n,), dimension_names=["point"],
                           fill_value=0)
        a[:] = np.arange(n)
        names = np.array([p.name for p in plan.points], dtype=str)
        a = g.create_array("point_name", shape=(n,), dtype=str, chunks=(n,), dimension_names=["point"])
        a[:] = names
        for nm, vals, at in (("latitude", plan.out_lat, lat_attrs), ("longitude", plan.out_lon, lon_attrs),
                             ("requested_latitude", [p.lat for p in plan.points], lat_attrs),
                             ("requested_longitude", [p.lon for p in plan.points], lon_attrs)):
            a = g.create_array(nm, shape=(n,), dtype="f8", chunks=(n,), dimension_names=["point"],
                               attributes=dict(at, comment="nearest grid cell" if nm in ("latitude", "longitude") else "as requested"),
                               fill_value=np.nan)
            a[:] = np.asarray(vals, dtype="f8")
        coords = "latitude longitude point_name requested_latitude requested_longitude"
    elif getattr(plan, "curvilinear", False):  # projected grid (CERRA, CARRA): 2-D coordinates over (y, x)
        ny, nx = plan.shape_space
        for nm, n in (("y", ny), ("x", nx)):
            a = g.create_array(nm, shape=(n,), dtype="i4", chunks=(n,), dimension_names=[nm], fill_value=0,
                               attributes={"long_name": f"{nm} index of the source grid"})
            a[:] = np.arange(n)
        grid_attrs = {f"grid_{k}": v for k, v in (getattr(plan.grid, "attrs", None) or {}).items()}
        for nm, vals, at in (("latitude", plan.out_lat, lat_attrs), ("longitude", plan.out_lon, lon_attrs)):
            a = g.create_array(nm, shape=vals.shape, dtype="f8", chunks=vals.shape, dimension_names=["y", "x"],
                               attributes=dict(at, **grid_attrs), fill_value=np.nan)
            a[:] = vals
        coords = "latitude longitude"
    else:
        for nm, vals, at in (("latitude", plan.out_lat, lat_attrs), ("longitude", plan.out_lon, lon_attrs)):
            a = g.create_array(nm, shape=vals.shape, dtype="f8", chunks=vals.shape, dimension_names=[nm],
                               attributes=at, fill_value=np.nan)
            a[:] = vals
        coords = None
    for vp in plan.vars:
        src = vp.var.attrs
        base = {k: src[k] for k in KEEP_ATTRS if k in src}
        for stat, name in zip(vp.stats, vp.out_names):
            attrs = dict(base, source_variable=vp.var.name)
            if stat not in ("none",):
                interval = CELL[t.freq].format(h=t.hours)
                method = "point" if stat == "first" else ("minimum" if stat == "min" else
                                                           "maximum" if stat == "max" else stat)
                attrs["cell_methods"] = f"valid_time: {method} (interval: {interval})"
                if stat != "first":
                    attrs["long_name"] = f"{base.get('long_name', vp.var.name)}, {stat} over {interval}"
            if coords:
                attrs["coordinates"] = coords
            g.create_array(name, shape=plan.out_shape(vp), chunks=plan.out_chunks(vp),
                           dtype="f8" if spec.output.dtype == "float64" else "f4", fill_value=np.nan,
                           compressors=compressors(spec.output), dimension_names=list(plan.out_dims(vp)),
                           attributes=attrs)


def _block_shape(src_chunks, dst_unit, shape, budget_bytes, itemsize) -> list[int]:
    """Read/write block: aligned to the destination's write unit (shard or chunk) so no destination chunk is
    written twice, and to the source chunks where affordable so no source chunk is decoded twice."""
    blk = []
    for s, d, n in zip(src_chunks, dst_unit, shape):
        b = s * d // math.gcd(s, d)
        blk.append(min(n, b))
    # shrink along the dimension with most bytes, in multiples of the destination unit
    while math.prod(blk) * itemsize > budget_bytes:
        i = max(range(len(blk)), key=lambda k: blk[k] / dst_unit[k])
        if blk[i] <= dst_unit[i]:
            break
        blk[i] = max(dst_unit[i], (blk[i] // dst_unit[i] // 2) * dst_unit[i])
    return blk


def _rechunk_export(src_store, out: str, plan: Plan, memory_gb: float, progress=None) -> int:
    import itertools

    src = zarr.open_group(src_store, mode="r")
    dst = zarr.open_group(out, mode="w", zarr_format=3, attributes=dict(src.attrs))
    o = plan.spec.output
    data_vars = {n: vp for vp in plan.vars for n in vp.out_names}
    # coordinates: small, copied whole
    for name, arr in src.arrays():
        if name in data_vars:
            continue
        a = dst.create_array(name, shape=arr.shape, dtype=arr.dtype, chunks=arr.shape or (1,),
                             fill_value=arr.fill_value, dimension_names=arr.metadata.dimension_names,
                             attributes=dict(arr.attrs))
        a[...] = arr[...]
    budget = memory_gb * 1e9 / 3
    total = 0
    blocks_all = []
    for name, vp in data_vars.items():
        arr = src[name]
        chunks, shards = plan.export_chunks(vp), plan.export_shards(vp)
        d = dst.create_array(name, shape=arr.shape, dtype=arr.dtype, chunks=chunks, shards=shards,
                             fill_value=arr.fill_value, compressors=compressors(o),
                             dimension_names=arr.metadata.dimension_names, attributes=dict(arr.attrs))
        blk = _block_shape(arr.chunks, shards or chunks, arr.shape, budget, arr.dtype.itemsize)
        ranges = [range(0, n, b) for n, b in zip(arr.shape, blk)]
        blocks_all.append((arr, d, blk, list(itertools.product(*ranges))))
    n_blocks = sum(len(b[3]) for b in blocks_all)
    done = 0
    for arr, d, blk, starts in blocks_all:
        for st in starts:
            sl = tuple(slice(s, min(s + b, n)) for s, b, n in zip(st, blk, arr.shape))
            d[sl] = arr[sl]
            done += 1
            if progress:
                progress(done, n_blocks)
        total += 1
    return n_blocks


def set_attrs(repo: ic.Repository, attrs: dict, message: str) -> None:
    s = repo.writable_session("main")
    g = zarr.open_group(s.store, mode="r+")
    g.attrs.update(attrs)
    s.commit(message)


def update_extended_attrs(repo: ic.Repository, plan: Plan, job_id: str, extra: dict | None = None) -> None:
    s = repo.writable_session("main")
    g = zarr.open_group(s.store, mode="r+")
    hist = g.attrs.get("history", "")
    direction = plan.spec.output.extend_direction or "forward"
    entry = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} extended ({direction}) by edh-downloader {__version__}"
    attrs = {
        "edh_last_job_id": job_id,
        "edh_status": "complete",
        "history": f"{hist}\n{entry}" if hist else entry,
    }
    if extra:
        attrs.update(extra)
    g.attrs.update(attrs)
    s.commit("job extension complete")


def inspect_icechunk_for_extend(target_path: Path | str) -> dict:
    p = Path(target_path).expanduser()
    if not p.exists():
        return {"ok": False, "error": f"path does not exist: {p}"}
    ic_path = p
    if p.suffix == ".zarr":
        comp = p.with_suffix(".icechunk")
        if comp.exists() and open_repo(comp):
            ic_path = comp
        else:
            return {
                "ok": False,
                "error": f"Only plain Zarr store found at {p}. Time extension is supported only for Icechunk "
                         f"repositories (or Zarr with companion .icechunk). Plain Zarr has no transaction "
                         f"manifests and would require remaking the dataset from scratch.",
            }
    repo = open_repo(ic_path)
    if repo is None:
        return {"ok": False, "error": f"{ic_path} is not an Icechunk repository"}
    try:
        s = repo.readonly_session("main")
        g = zarr.open_group(s.store, mode="r")
        job_meta = g.attrs.get("edh_job") or {}
        if not isinstance(job_meta, dict):
            job_meta = {}
        if "valid_time" not in g:
            return {"ok": False, "error": f"{ic_path} does not contain a 'valid_time' coordinate array"}
        vt = g["valid_time"]
        vt_units = vt.attrs.get("units", "")
        _, _, origin_str = vt_units.partition(" since ")
        origin = pd.Timestamp(origin_str) if origin_str else pd.Timestamp("1970-01-01")
        is_minutes = "minutes" in vt_units
        unit_delta = pd.Timedelta(minutes=1) if is_minutes else pd.Timedelta(hours=1)
        v0 = int(vt[0])
        v1 = int(vt[-1])
        t_start = origin + (v0 * unit_delta)
        t_end = origin + (v1 * unit_delta)
        time_len = int(vt.shape[0])
        step_delta = unit_delta
        if time_len > 1:
            step_delta = (int(vt[1]) - v0) * unit_delta

        vars_info = {}
        for name, a in g.arrays():
            dims = list(a.metadata.dimension_names or ())
            if "valid_time" in dims:
                vars_info[name] = {"shape": list(a.shape), "chunks": list(a.chunks), "dims": dims}

        return {
            "ok": True,
            "path": str(p),
            "icechunk_path": str(ic_path),
            "job": job_meta,
            "time_start": t_start.isoformat(),
            "time_end": t_end.isoformat(),
            "time_len": time_len,
            "time_units": vt_units,
            "step_seconds": int(step_delta.total_seconds()),
            "variables": list(vars_info.keys()),
            "vars_info": vars_info,
        }
    except Exception as e:
        return {"ok": False, "error": f"failed to read {ic_path}: {e}"}


def check_extension(repo: ic.Repository, plan: Plan, direction: str) -> str | None:
    """Why ``plan`` cannot be appended to ``repo`` (None when it can). Checked before anything is resized."""
    g = zarr.open_group(repo.readonly_session("main").store, mode="r")
    if direction not in ("forward", "backward"):
        return f"unknown extension direction {direction!r}"
    if "valid_time" not in g:
        return "the target has no 'valid_time' coordinate"
    missing = [n for vp in plan.vars for n in vp.out_names if n not in g]
    if missing:
        return (f"the target store has no variable {', '.join(missing)}: extend with the variables it already "
                f"holds")
    if plan.n_periods == 0:
        return "the new period is empty"
    if plan.points is not None:
        n = len(plan.points)
        if "point" not in g or g["point"].shape[0] != n:
            return "the target store was not made with the same list of points"
    for name, want in (("latitude", plan.out_lat), ("longitude", plan.out_lon)):
        if name not in g:
            return f"the target store has no '{name}' coordinate"
        have = np.asarray(g[name][:], dtype="f8")
        want = np.asarray(want, dtype="f8")
        if have.shape != want.shape or not np.allclose(have, want, equal_nan=True):
            return (f"the area differs from the target store ({name}: {have.size} values in the store, "
                    f"{want.size} in this job): use the same area and grid as the store")
    if plan.has_levels:
        name = plan.ds.meta.level_dim
        if name not in g or not np.array_equal(np.asarray(g[name][:], dtype="f8"),
                                               np.asarray(plan.level_values, dtype="f8")):
            return "the pressure levels differ from the target store"
    vt = g["valid_time"]
    vals, _ = _time_encoding(plan, base_units=vt.attrs.get("units"))
    old = vt[:]
    if len(old):
        if direction == "forward" and vals[0] <= old[-1]:
            return "the new period starts before the end of the store (it would repeat times): pick later dates"
        if direction == "backward" and vals[-1] >= old[0]:
            return "the new period ends after the start of the store (it would repeat times): pick earlier dates"
    return None


def extension_done(repo: ic.Repository, job_id: str) -> dict | None:
    """The extension a job already applied to this repository (so a resumed job never extends it twice)."""
    g = zarr.open_group(repo.readonly_session("main").store, mode="r")
    return (g.attrs.get("edh_extensions") or {}).get(job_id)


def extend_repo(repo: ic.Repository, plan: Plan, direction: str, job_id: str | None = None) -> dict:
    s = repo.writable_session("main")
    g = zarr.open_group(s.store, mode="r+")
    vt = g["valid_time"]
    old_len = vt.shape[0]
    new_len = len(plan.labels)
    total_len = old_len + new_len

    vals, _ = _time_encoding(plan, base_units=vt.attrs.get("units"))

    vt.resize((total_len,))
    if direction == "forward":
        vt[old_len:total_len] = vals
        offset = old_len
        for vp in plan.vars:
            for name in vp.out_names:
                if name in g:
                    arr = g[name]
                    arr.resize((total_len, *arr.shape[1:]))
    elif direction == "backward":
        old_vals = vt[:old_len]
        vt[:new_len] = vals
        vt[new_len:total_len] = old_vals
        offset = 0
        for vp in plan.vars:
            for name in vp.out_names:
                if name in g:
                    arr = g[name]
                    arr.resize((total_len, *arr.shape[1:]))
                    ct = arr.chunks[0]
                    if new_len % ct == 0:
                        chunks_to_shift = new_len // ct
                        shift = [chunks_to_shift] + [0] * (arr.ndim - 1)
                        s.shift_array(f"/{name}", shift)
                    else:
                        for start in range(old_len, 0, -ct):
                            s_idx = max(0, start - ct)
                            arr[s_idx + new_len : start + new_len] = arr[s_idx : start]
    else:
        raise ValueError(f"unknown extension direction {direction!r}")

    result = {"old_len": old_len, "new_len": new_len, "offset": offset, "direction": direction}
    if job_id:  # same commit as the resize, so it is all-or-nothing
        g.attrs["edh_extensions"] = {**(g.attrs.get("edh_extensions") or {}), job_id: result}
    s.commit(f"initialize {direction} time extension ({new_len} periods)")
    return result


async def _copy_store(src, dst, concurrency: int = 32, progress=None) -> int:
    from zarr.core.buffer import default_buffer_prototype

    proto = default_buffer_prototype()
    keys = [k async for k in src.list()]
    it = iter(keys)
    done = 0

    async def worker():  # a fixed pool: millions of chunks must not become millions of pending tasks
        nonlocal done
        for k in it:
            buf = await src.get(k, prototype=proto)
            if buf is not None:
                await dst.set(k, buf)
            done += 1
            if progress and done % 500 == 0:
                progress(done, len(keys))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return len(keys)


def export_zarr(repo: ic.Repository, out: Path, progress=None, consolidated: bool = True, plan: Plan | None = None,
                memory_gb: float = 16.0) -> int:
    """Write the committed state of ``main`` as a plain Zarr v3 directory.

    With the staging layout and no sharding, chunk bytes are copied as-is; otherwise the data variables are
    rechunked (and sharded) into the layout chosen in the job, one memory-bounded block at a time."""
    from .chunking import export_needs_rechunk

    tmp = out.with_name(out.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    src = repo.readonly_session("main").store
    dst = zarr.storage.LocalStore(str(tmp))
    if plan is not None and export_needs_rechunk(plan):
        n = _rechunk_export(src, str(tmp), plan, memory_gb, progress)
    else:
        n = asyncio.run(_copy_store(src, dst, progress=progress))
    if consolidated:
        zarr.consolidate_metadata(zarr.storage.LocalStore(str(tmp)))
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    return n
