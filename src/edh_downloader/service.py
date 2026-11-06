"""Everything the UI needs from the machine that holds the data, as plain functions with JSON in and out.

The UI never touches keys, quota, datasets, plans, jobs or output stores directly: it calls these functions,
either in process ("This computer") or over SSH through ``agent`` (a server). That is what lets the interface
run on your computer while the work and the files stay on the server.
"""

import asyncio
import math
import platform
import threading
from pathlib import Path

from . import __version__, catalog, chunking, keys, quota, runner, sample
from .gui import structure
from .http import RequestCounter
from .job import JobSpec
from .planner import Plan, PlanError

_DATASETS: dict[str, catalog.Dataset] = {}
_STORE_CACHE: dict = {}
_LOCK = threading.Lock()


def _key(name):
    try:
        return keys.get_key(name)
    except KeyError:
        return None


def hello() -> dict:
    return {"version": __version__, "default_dir": str(Path("~/edh-data").expanduser()),
            "platform": f"{platform.system()} {platform.machine()}", "python": platform.python_version()}


# ------------------------------------------------------------------ keys and quota

def _key_row(k: keys.ApiKey) -> dict:
    return {"name": k.name, "quota": k.quota, "source": k.source, "fingerprint": k.fingerprint,
            "usage": quota.info(k.fingerprint)}


def keys_list() -> dict:
    return {"default": keys.default_name(), "keys": [_key_row(k) for k in keys.list_keys()]}


def key_status(name=None) -> dict | None:
    k = _key(name)
    return _key_row(k) if k else None


def key_add(name: str, secret: str, quota_: int, check: bool = True) -> dict:
    if check:
        ok, msg = asyncio.run(keys.validate(secret))
        if not ok:
            return {"ok": False, "msg": msg}
    try:
        k = keys.add_key(name, secret, int(quota_))
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    if check:
        quota.add(k.fingerprint, 1)
    return {"ok": True, "name": k.name}


def key_test(name: str) -> dict:
    k = keys.get_key(name)
    ok, msg = asyncio.run(keys.validate(k.secret))
    quota.add(k.fingerprint, 1)
    return {"ok": ok, "msg": msg}


def key_remove(name: str) -> None:
    keys.remove_key(name)


def key_default(name: str) -> None:
    keys.set_default(name)


def key_set_quota(name: str, quota_: int) -> None:
    keys.set_quota(name, int(quota_))


def key_calibrate(name: str, used: int) -> None:
    quota.calibrate(keys.get_key(name).fingerprint, int(used))


# ------------------------------------------------------------------ catalogue and datasets

def collections() -> list[dict]:
    return catalog.collections()


def datasets(collection: str) -> list[dict]:
    return catalog.datasets(collection)


def dataset_load(info: dict, key: str, refresh: bool = False) -> dict:
    k = keys.get_key(key)
    url = info["url"]
    with _LOCK:
        ds = _DATASETS.get(url)
    if ds is None or refresh:
        ds = catalog.load_dataset(url, k.secret, counter=RequestCounter(k.fingerprint), refresh=refresh, info=info)
        with _LOCK:
            _DATASETS[url] = ds
    return {"summary": ds.summary(),
            "variables": [{"name": v.name, "long_name": v.long_name, "units": v.units, "kind": v.kind,
                           "default_stat": v.default_stat()} for v in ds.variables.values() if v.kind != "unsupported"],
            "levels": None if ds.levels is None else ds.levels.tolist()}


def _plan(spec: dict) -> Plan:
    s = JobSpec.from_dict(spec)
    ds = _DATASETS.get(s.dataset_url or "")
    if ds is None:
        raise PlanError("the dataset is not loaded: reload it")
    return Plan(s, ds)


# ------------------------------------------------------------------ plan, sample

def plan(spec: dict, connections: int, follow: bool) -> dict:
    """Everything the form shows about a spec: summary, chunk layouts, size estimates, dataset structure."""
    try:
        p = _plan(spec)
    except (PlanError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    rec = chunking.recommend(p)
    if follow:
        p.spec.output.chunk_layout = rec[0]
    smp = sample.last_result(p.ds.url)
    if not (smp and smp.get("dataset") == p.ds.url):
        smp = None
    est = sample.estimate(p, smp, connections, chunking.compression_ratio(p))
    o = p.spec.output
    shards = None
    vp = next((v for v in p.vars if v.var.kind != "static"), p.vars[0]) if p.vars else None
    if vp is not None:
        ch = p.export_chunks(vp)
        dims = p.out_dims(vp)
        size = dict(zip(dims, p.out_shape(vp)))
        sh = p.export_shards(vp)
        shards = {"chunks": math.prod(math.ceil(size[d] / c) for d, c in zip(dims, ch)),
                  "files": math.prod(math.ceil(size[d] / c) for d, c in zip(dims, sh)) if sh else None}
    k = _key((spec.get("download") or {}).get("key"))
    return {"ok": True, "summary": p.summary(), "rec": list(rec), "layout": o.chunk_layout,
            "rows": chunking.analyze(p), "est": est, "shards": shards,
            "struct": structure.describe(p, chunking.compression_ratio(p), est.get("ratios")),
            "paths": {"zarr": str(p.spec.zarr_path) if o.export_zarr else None,
                      "icechunk": str(p.spec.icechunk_path) if o.keep_icechunk else None},
            "n_requests": p.n_requests, "sample": smp,
            "sample_n": min(sample.DEFAULT_REQUESTS, p.n_requests),
            "sample_requests": sample.DEFAULT_REQUESTS,
            "key": _key_row(k) if k else None}


def sample_run(spec: dict, key: str, n: int, emit=None) -> dict:
    p = _plan(spec)
    k = keys.get_key(key)
    prog = (lambda i, t: emit({"i": i, "total": t})) if emit else None
    asyncio.run(sample.run(p, k.secret, RequestCounter(k.fingerprint), n=n, progress=prog))
    return {"n": n}


# ------------------------------------------------------------------ jobs and stores

def job_start(spec: dict) -> dict:
    s = JobSpec.from_dict(spec)
    if not _key(s.download.key):
        raise ValueError("add an API key first")
    if s.icechunk_path.exists() or s.zarr_path.exists():
        raise ValueError(f"output {s.output.name} already exists in {s.output.directory}; choose another name")
    Path(s.output.directory).expanduser().mkdir(parents=True, exist_ok=True)
    d = runner.create_job(s)
    return {"id": d.name, "pid": runner.launch(d)}


def jobs_list() -> list[dict]:
    out = runner.list_jobs()
    for j in out:
        j["outputs_exist"] = [o for o in j.get("outputs") or [] if Path(o).exists()]
    return out


def job_resume(jid: str) -> dict:
    return {"pid": runner.launch(runner.resolve(jid))}


def job_stop(jid: str, action: str) -> None:
    runner.stop(runner.resolve(jid), action)


def job_connections(jid: str, n: int) -> None:
    runner.set_control(runner.resolve(jid), connections=int(n))


def job_delete(jid: str) -> None:
    runner.delete(runner.resolve(jid))


def job_log(jid: str, offset: int = 0) -> dict:
    p = runner.resolve(jid) / "log.txt"
    if not p.exists():
        return {"lines": [], "next": offset}
    lines = p.read_text(errors="replace").splitlines()
    return {"lines": lines[offset:], "next": len(lines)}


def store_structure(path: str) -> dict:
    p = Path(path)
    key = (str(p), (p / "zarr.json").stat().st_mtime if (p / "zarr.json").exists() else p.stat().st_mtime)
    if key not in _STORE_CACHE:
        _STORE_CACHE.clear()
        _STORE_CACHE[key] = structure.from_store(p)
    return _STORE_CACHE[key]


METHODS = {f.__name__: f for f in (
    hello, keys_list, key_status, key_add, key_test, key_remove, key_default, key_set_quota, key_calibrate,
    collections, datasets, dataset_load, plan, sample_run, job_start, jobs_list, job_resume, job_stop,
    job_connections, job_delete, job_log, store_structure)}
