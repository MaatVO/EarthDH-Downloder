"""Output chunking: candidate layouts, their costs, and a recommendation for a given selection.

A chunk is the unit Zarr reads and decompresses. Good chunks are a few MB to a few tens of MB; their *shape*
decides which reads are cheap: long-in-time chunks make time series at a point cheap, wide-in-space chunks
make maps at one date cheap. The download costs the same either way.

The Icechunk repository is always written with the *staging* layout ("auto": one download unit of time per
chunk), so every chunk is written exactly once and resuming is safe. Any other layout, and sharding, is
applied to the exported Zarr in one local rechunking pass at the end.
"""

import math

TARGET = 4_000_000  # values per chunk: ~16 MB of float32 before compression
TS_MAX_STEPS = 87_660  # time-series layout keeps up to ~10 years of hourly steps in one chunk
LAYOUTS = ("auto", "timeseries", "maps", "custom")
TITLES = {"auto": "Automatic", "timeseries": "Time series", "maps": "Maps", "custom": "Custom"}
BLURB = {
    "auto": "Balanced; aligned with the download units, so it is the fastest to write.",
    "timeseries": "Long in time, small in space: cheap to read many years at a point or a small region.",
    "maps": "The whole area for a few time steps: cheap to read or plot one date over a large area.",
    "custom": "Your own sizes; empty fields keep the automatic value.",
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def chunk_shape(plan, vp, layout: str, output) -> tuple:
    """Chunk shape for output variable ``vp`` (dims as ``plan.out_dims(vp)``)."""
    n = max(1, plan.n_periods)
    per_unit = max((u.k1 - u.k0 for u in plan.units if not u.static), default=1)
    lay = layout if layout in LAYOUTS else "auto"
    base = "auto" if lay == "custom" else lay
    if plan.points is not None:
        npts = plan.shape_space[0]
        if base == "timeseries":
            ct = min(n, TS_MAX_STEPS)
            space = (int(_clamp(TARGET // ct, 1, npts)),)
        elif base == "maps":
            pc = min(npts, TARGET)
            ct = int(_clamp(TARGET // pc, 1, min(n, per_unit)))
            space = (pc,)
        else:
            ct = min(n, per_unit)
            space = (min(npts, 1000),)
        if lay == "custom" and output.chunk_points:
            space = (min(npts, output.chunk_points),)
    else:
        ny, nx = plan.shape_space
        if base == "timeseries":
            ct = min(n, TS_MAX_STEPS)
            side = int(_clamp(math.sqrt(TARGET / ct), 4, 64))
            if ny * nx <= 2 * side * side:  # small area: keep it whole
                sy, sx = ny, nx
            else:
                sy, sx = min(ny, side), min(nx, side)
        elif base == "maps":
            if ny * nx <= TARGET:
                sy, sx = ny, nx
            else:
                side = int(math.sqrt(TARGET))
                sy, sx = min(ny, side), min(nx, side)
            ct = int(_clamp(TARGET // (sy * sx), 1, min(n, 24)))
        else:
            ct = min(n, per_unit)
            side = int(_clamp(math.sqrt(TARGET / ct), 30, 360))
            sy, sx = min(ny, side), min(nx, side)
        if lay == "custom":
            sy = min(ny, output.chunk_lat or sy)
            sx = min(nx, output.chunk_lon or sx)
        space = (sy, sx)
    if lay == "custom" and output.chunk_time:
        ct = output.chunk_time
    ct = min(n, max(1, ct))
    if vp.var.kind == "static":
        return space
    lev = (1,) if vp.var.kind == "level" else ()
    return (ct, *lev, *space)


def staging_shape(plan, vp) -> tuple:
    """Chunks of the Icechunk repository: the automatic layout, independent of the user's export choice."""
    from .job import OutputSpec

    return chunk_shape(plan, vp, "auto", OutputSpec())


def export_needs_rechunk(plan) -> bool:
    o = plan.spec.output
    if o.sharding:
        return True
    return any(chunk_shape(plan, vp, o.chunk_layout, o) != staging_shape(plan, vp) for vp in plan.vars)


def shard_shape(plan, vp, chunks: tuple, output) -> tuple | None:
    """Shard shape (a multiple of the chunk shape) or None when sharding is off."""
    if not output.sharding:
        return None
    f = suggest_shard_factors(plan, vp, chunks) if output.shard_auto else (
        output.shard_time or 1, output.shard_space or 1, output.shard_space or 1)
    ft, fy, fx = f
    dims = plan.out_dims(vp)
    out = []
    for d, c in zip(dims, chunks):
        if d == "valid_time":
            out.append(c * ft)
        elif d in ("latitude", "point"):
            out.append(c * fy)
        elif d == "longitude":
            out.append(c * fx)
        else:
            out.append(c)
    return tuple(out)


def suggest_shard_factors(plan, vp, chunks: tuple) -> tuple[int, int, int]:
    """Chunks per shard along (time, lat|point, lon), aiming at ~512 MB shards; space first, since every
    download unit writes the whole area for its time slab."""
    dims = plan.out_dims(vp)
    size = dict(zip(dims, plan.out_shape(vp)))
    ch = dict(zip(dims, chunks))
    chunk_mb = 4 * math.prod(chunks) / 1e6
    k = max(1, int(512 / max(chunk_mb, 0.01)))
    if "point" in ch:
        ny = math.ceil(size["point"] / ch["point"])
        return 1, int(_clamp(k, 1, ny)), 1
    if "latitude" not in ch:
        return 1, 1, 1
    ny = math.ceil(size["latitude"] / ch["latitude"])
    nx = math.ceil(size["longitude"] / ch["longitude"])
    side = int(math.sqrt(k))
    fy, fx = int(_clamp(side, 1, ny)), int(_clamp(side, 1, nx))
    ft = 1
    if "valid_time" in ch:
        nt = math.ceil(size["valid_time"] / ch["valid_time"])
        ft = int(_clamp(k // (fy * fx), 1, nt))
    return ft, fy, fx


def compression_ratio(plan, measured: float | None = None) -> float:
    """Estimated compressed / uncompressed size of the output data.

    Hourly values keep the source's 13-bit precision even when stored "lossless", so they compress about as
    well as the source chunks on Earth Data Hub (``measured``, from the connection test, when available).
    Statistics have full mantissas and compress much less unless bit-rounded."""
    o = plan.spec.output
    if o.compressor == "none":
        return 1.0
    rounded = o.keepbits is not None and o.keepbits <= 13
    if plan.spec.time.freq == "none" or rounded:
        r = measured if measured else 0.2
        if o.keepbits is not None and o.keepbits < 13:
            r *= 0.6 + 0.4 * o.keepbits / 13
    else:
        r = 0.55
    if o.dtype == "float64":
        r = min(1.0, r * 1.15)  # the extra mantissa bytes are mostly noise
    if "lz4" in o.compressor and o.compressor != "blosc-lz4hc":
        r = min(1.0, r * 1.25)
    return r


def _compression_ratio(plan) -> float:
    return compression_ratio(plan)


def analyze(plan, vp=None) -> list[dict]:
    """Numbers for each layout, for the first time-dependent output variable."""
    vp = vp or next((v for v in plan.vars if v.var.kind != "static"), plan.vars[0])
    shape = plan.out_shape(vp)
    dims = plan.out_dims(vp)
    size = dict(zip(dims, shape))
    itemsize = 8 if plan.spec.output.dtype == "float64" else 4
    ratio = _compression_ratio(plan)
    per_unit = max((u.k1 - u.k0 for u in plan.units if not u.static), default=1)
    rows = []
    for lay in LAYOUTS:
        ch = chunk_shape(plan, vp, lay, plan.spec.output)
        c = dict(zip(dims, ch))
        raw = math.prod(ch) * itemsize
        counts = {d: math.ceil(size[d] / c[d]) for d in dims}
        n_chunks = math.prod(counts.values())
        t_chunks = counts.get("valid_time", 1)
        space_chunks = math.prod(v for d, v in counts.items() if d in ("latitude", "longitude", "point"))
        ct = c.get("valid_time", 1)
        rows.append({
            "layout": lay, "title": TITLES[lay], "blurb": BLURB[lay],
            "chunks": dict(c), "shape": dict(size),
            "chunk_mb": raw / 1e6, "chunk_mb_compressed": raw * ratio / 1e6,
            "n_chunks": n_chunks * max(1, len(vp.out_names)) * (1 if vp.var.kind != "level" else 1),
            "point_series_chunks": t_chunks, "point_series_mb": t_chunks * raw / 1e6,
            "map_chunks": space_chunks, "map_mb": space_chunks * raw / 1e6,
            "export": "direct copy" if (ch == staging_shape(plan, vp) and not plan.spec.output.sharding)
                      else "rechunk pass",
        })
    return rows


def recommend(plan) -> tuple[str, str]:
    """(layout, short reason) for the current selection."""
    n = plan.n_periods
    if plan.points is not None:
        return "timeseries", f"{plan.shape_space[0]} points × {n:,} steps: point data is read as time series."
    ny, nx = plan.shape_space
    if n <= 48:
        return "maps", f"only {n} time steps: whole-area chunks."
    if ny * nx <= 100 * 100 and n > 2000:
        return "timeseries", f"small area ({ny}×{nx}), long period ({n:,} steps)."
    if ny * nx >= 400 * 400 and n <= 2000:
        return "maps", f"large area ({ny}×{nx}), few time steps ({n:,})."
    return "auto", f"{ny}×{nx} cells × {n:,} steps: no dominant access pattern."
