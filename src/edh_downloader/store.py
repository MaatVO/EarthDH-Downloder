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
              "GRIB_stepType", "GRIB_typeOfLevel", "GRIB_name")
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
        "source_collection": spec.collection,
        "source_updated": ds.info.get("updated", ""),
        "source_institution": ds.meta.attrs.get("institution", ""),
        "provider": "Earth Data Hub (https://earthdatahub.destine.eu)",
        "license": "CC-BY-4.0 (Copernicus ERA5; see the source dataset)",
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


def _time_encoding(plan: Plan):
    units = plan.ds.time_units
    _, _, origin = units.partition(" since ")
    origin = pd.Timestamp(origin)
    hours = (plan.labels - origin) / pd.Timedelta(hours=1)
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
        a = g.create_array(name, shape=lv.shape, dtype=lv.dtype, chunks=lv.shape, dimension_names=[name],
                           attributes={"units": "hPa", "long_name": "pressure level", "positive": "down"},
                           fill_value=0)
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


async def _copy_store(src, dst, concurrency: int = 32, progress=None) -> int:
    from zarr.core.buffer import default_buffer_prototype

    proto = default_buffer_prototype()
    keys = [k async for k in src.list()]
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(k):
        nonlocal done
        async with sem:
            buf = await src.get(k, prototype=proto)
            if buf is not None:
                await dst.set(k, buf)
            done += 1
            if progress and done % 500 == 0:
                progress(done, len(keys))

    await asyncio.gather(*(one(k) for k in keys))
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
