"""Sample estimate: download a few of the job's own chunks to measure what the plan can only guess.

From ~a dozen real chunks of the selection we measure
  * the compressed size of a source chunk per variable  -> download size,
  * single-connection and parallel throughput           -> time and a suggested connection count,
  * the compression of *your* output encoding on the real values (cut to the area, aggregated, rounded,
    encoded with the chosen codec)                       -> Zarr size.

The decoded samples are kept in the cache so the Zarr size is re-estimated offline whenever the output
settings change; only a new variable selection needs a new sample.
"""

import asyncio
import hashlib
import math
import time
from collections import defaultdict

import numcodecs
import numpy as np

from .aggregate import bitround, reduce_periods
from .http import EDHClient
from .paths import cache_dir
from .util import read_json, write_json_atomic

DEFAULT_REQUESTS = 12
SEQUENTIAL = 2  # first requests one at a time: single-connection speed and latency
MAX_SAMPLES_PER_VAR = 4  # decoded chunks kept per variable for the size estimate
MAX_CONNECTIONS = 32


def _key(ds_url: str) -> str:
    return hashlib.sha256(ds_url.encode()).hexdigest()[:16]


def _dir(ds_url: str):
    d = cache_dir() / "samples" / _key(ds_url)
    d.mkdir(parents=True, exist_ok=True)
    return d


def pick(plan, n: int) -> list[tuple]:
    """``n`` chunks the job itself needs, spread over variables and over the period."""
    by_var = defaultdict(list)
    for c in plan.chunk_last_unit:
        by_var[c[1]].append(c)
    lists = [v for _, v in sorted(by_var.items())]
    # time-dependent variables get the budget first; a static field needs one chunk
    lists.sort(key=lambda lst: lst[0][2] is None)
    out = []
    per = max(1, n // max(1, len(lists)))
    for lst in lists:
        k = 1 if lst[0][2] is None else min(len(lst), per)
        idx = np.unique(np.linspace(0, len(lst) - 1, k + 2)[1:-1].round().astype(int)) if len(lst) > k else range(len(lst))
        out += [lst[i] for i in idx]
    # fill the remaining budget from the largest lists
    taken = set(out)
    rest = [c for lst in lists for c in lst if c not in taken]
    out += rest[:max(0, n - len(out))]
    return out[:n]


async def run(plan, secret: str, counter, n: int = DEFAULT_REQUESTS, progress=None) -> dict:
    chunks = pick(plan, n)
    if not chunks:
        raise ValueError("nothing to sample")
    seq, par = chunks[:SEQUENTIAL], chunks[SEQUENTIAL:]
    conc = max(1, len(par))
    got: list[tuple[tuple, bytes | None, float]] = []

    async with EDHClient(secret, counter=counter, connections=max(1, conc), retries=2) as client:
        async def one(c):
            vp = plan.vars[c[1]]
            t = time.perf_counter()
            data = await client.get(vp.chunk_url(plan.ds.url, c[2], c[3], c[4], c[5]))
            got.append((c, data, time.perf_counter() - t))
            if progress:
                progress(len(got), len(chunks))
            return len(data or b"")

        t0 = time.perf_counter()
        b1 = 0
        for c in seq:
            b1 += await one(c)
        t_seq = time.perf_counter() - t0
        t1 = time.perf_counter()
        bn = sum(await asyncio.gather(*(one(c) for c in par))) if par else 0
        t_par = time.perf_counter() - t1

    single_bps = b1 / t_seq if t_seq > 0 and b1 else None
    par_bps = bn / t_par if t_par > 0 and bn else None
    latency = sum(dt for c, _, dt in got[:len(seq)]) / max(1, len(seq))

    # suggestion: the line saturates where parallel throughput stops growing with connections
    saturated = bool(single_bps and par_bps and par_bps < 0.7 * conc * single_bps)
    if single_bps and par_bps:
        if saturated:
            suggested = int(min(conc, max(2, math.ceil(1.25 * par_bps / single_bps))))
        else:
            suggested = int(min(MAX_CONNECTIONS, max(conc, 2 * conc)))
    else:
        suggested = 8

    # per-variable source chunk size and decoded samples (cut to the selection)
    sizes = defaultdict(list)
    d = _dir(plan.ds.url)
    kept = defaultdict(int)
    for c, data, _ in got:
        if not data:
            continue
        vp = plan.vars[c[1]]
        sizes[vp.var.name].append(len(data))
        if kept[vp.var.name] < MAX_SAMPLES_PER_VAR:
            arr = vp.var.decoder().decode(data)
            np.save(d / f"{vp.var.name}.{kept[vp.var.name]}.npy", _cut(plan, vp, c, arr))
            kept[vp.var.name] += 1
    for name, k in kept.items():  # drop older samples beyond what this run wrote
        for old in d.glob(f"{name}.*.npy"):
            if int(old.name.split(".")[-2]) >= k:
                old.unlink()

    result = {
        "dataset": plan.ds.url, "time": time.time(), "requests": len(got),
        "variables": sorted(sizes),
        "bytes_per_chunk": {k: sum(v) / len(v) for k, v in sizes.items()},
        "single_bps": single_bps, "parallel_bps": par_bps, "parallel_connections": conc,
        "latency_s": latency, "saturated": saturated, "suggested": suggested,
    }
    saved = read_json(cache_dir() / "samples" / "index.json", {}) or {}
    saved[plan.ds.url] = result
    write_json_atomic(cache_dir() / "samples" / "index.json", saved)
    return result


def last_result(ds_url: str) -> dict | None:
    return (read_json(cache_dir() / "samples" / "index.json", {}) or {}).get(ds_url)


def _cut(plan, vp, c, arr: np.ndarray) -> np.ndarray:
    """The part of a source chunk that ends up in the output: (time, cells...) float32, level squeezed."""
    if vp.var.kind == "level":
        arr = arr[:, 0]
    ty, tx = c[4], c[5]
    if plan.points is not None:
        sel = [(i - ty * vp.cy, j - tx * vp.cx) for i, j in zip(plan.point_i, plan.point_j)
               if i // vp.cy == ty and j // vp.cx == tx] or [(arr.shape[-2] // 2, arr.shape[-1] // 2)]
        ii, jj = np.array(sel).T
        return np.ascontiguousarray(arr[..., ii, jj], dtype=np.float32)
    rows = plan.lat_idx[(plan.lat_idx // vp.cy) == ty] - ty * vp.cy
    cols = plan.lon_idx[(plan.lon_idx // vp.cx) == tx] - tx * vp.cx
    rows, cols = rows[rows < arr.shape[-2]], cols[cols < arr.shape[-1]]
    if not len(rows) or not len(cols):
        return np.ascontiguousarray(arr, dtype=np.float32)
    return np.ascontiguousarray(arr[..., rows.min():rows.max() + 1, cols.min():cols.max() + 1], dtype=np.float32)


def _codec(o):
    if o.compressor == "none":
        return None
    if o.compressor.startswith("blosc-"):
        sh = {"shuffle": numcodecs.Blosc.SHUFFLE, "bitshuffle": numcodecs.Blosc.BITSHUFFLE,
              "noshuffle": numcodecs.Blosc.NOSHUFFLE}[o.shuffle]
        return numcodecs.Blosc(cname=o.compressor.split("-", 1)[1], clevel=int(max(0, min(9, o.clevel))), shuffle=sh)
    if o.compressor == "zstd":
        return numcodecs.Zstd(level=int(max(1, min(22, o.clevel))))
    if o.compressor in ("gzip", "zlib"):
        return numcodecs.GZip(level=int(max(1, min(9, o.clevel))))
    return None


def output_ratio(plan) -> dict[str, float] | None:
    """Measured compressed/uncompressed ratio per output variable, from the cached samples. None if no
    sample covers any selected variable."""
    d = cache_dir() / "samples" / _key(plan.ds.url)
    if not d.exists():
        return None
    o, t = plan.spec.output, plan.spec.time
    codec = _codec(o)
    dtype = np.float64 if o.dtype == "float64" else np.float32
    if t.freq != "none" and plan.n_periods:
        L = int(max(1, np.median(plan.p_end - plan.p_start)))
    else:
        L = 1
    out = {}
    for vp in plan.vars:
        files = sorted(d.glob(f"{vp.var.name}.*.npy"))
        if not files:
            continue
        arrs = [np.load(f) for f in files]
        for stat, name in zip(vp.stats, vp.out_names):
            raw = enc = 0
            for a in arrs:
                if vp.var.kind != "static" and t.freq != "none":
                    starts = np.arange(0, a.shape[0], L)
                    a = reduce_periods(a, starts, stat)
                a = np.ascontiguousarray(a, dtype=np.float32)
                if o.keepbits is not None:
                    a = bitround(a, o.keepbits)
                a = np.ascontiguousarray(a, dtype=dtype)
                raw += a.nbytes
                enc += len(codec.encode(a)) if codec else a.nbytes
            if raw:
                out[name] = min(1.0, enc / raw)
    return out or None


def throughput(result: dict | None, connections: int) -> float | None:
    """Expected bytes/s at ``connections``: linear in connections up to where the sample saw the line saturate."""
    if not result or not result.get("single_bps"):
        return None
    lin = connections * result["single_bps"]
    par, pc = result.get("parallel_bps"), result.get("parallel_connections", 1)
    if not par:
        return lin
    cap = par if result.get("saturated") else max(par, 2 * par * min(1.0, connections / max(1, pc)))
    return min(lin, cap) if connections >= 1 else lin


def estimate(plan, result: dict | None, connections: int, heuristic_ratio: float) -> dict:
    """Download size, Zarr size and time, measured where the sample covers the selection."""
    from collections import Counter

    from .planner import DEFAULT_COMPRESSION

    per_var = Counter(c[0] for c in plan.chunk_last_unit)
    bpc = (result or {}).get("bytes_per_chunk", {}) if result and result.get("dataset") == plan.ds.url else {}
    dl, dl_measured = 0.0, bool(per_var)
    for vp in plan.vars:
        n = per_var.get(vp.var.name, 0)
        if vp.var.name in bpc:
            dl += n * bpc[vp.var.name]
        else:
            dl_measured = False
            dl += n * int(np.prod(vp.var.chunks)) * vp.var.decoder().dtype.itemsize * DEFAULT_COMPRESSION
    ratios = output_ratio(plan) or {}
    item = 8 if plan.spec.output.dtype == "float64" else 4
    raw = zarr = 0.0
    size_measured = True
    per_out = {}
    for vp in plan.vars:
        nb = math.prod(plan.out_shape(vp)) * item
        for name in vp.out_names:
            r = ratios.get(name)
            if r is None:
                size_measured, r = False, heuristic_ratio
            per_out[name] = r
            raw += nb
            zarr += nb * r
    thr = throughput(result, connections) if bpc else None
    return {"download": dl, "download_measured": dl_measured, "raw": raw, "zarr": zarr,
            "zarr_measured": size_measured and bool(ratios), "ratios": per_out,
            "eta_s": dl / thr if thr else None, "throughput": thr}
