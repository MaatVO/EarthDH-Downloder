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

from . import __version__, catalog, chunking, keys, quota, runner, sample, sources, store
from .gui import structure
from .http import RequestCounter
from .job import JobSpec
from .planner import Plan, PlanError

_DATASETS: dict[str, catalog.Dataset] = {}
_STORE_CACHE: dict = {}
_LOCK = threading.Lock()


def _key(name, provider: str = sources.EDH):
    try:
        return keys.get_key(name, provider)
    except KeyError:
        return None


def hello() -> dict:
    return {"version": __version__, "default_dir": str(Path("~/edh-data").expanduser()),
            "platform": f"{platform.system()} {platform.machine()}", "python": platform.python_version()}


# ------------------------------------------------------------------ keys and quota

def _key_row(k: keys.ApiKey) -> dict:
    """A key as the UI sees it (never the secret). ``source``: where the key comes from (stored, netrc, cdsapirc);
    ``provider``: the data source it opens. Only sources with a monthly quota have ``usage``."""
    counted = sources.get(k.provider).has_quota
    return {"name": k.name, "quota": k.quota, "source": k.source, "provider": k.provider,
            "fingerprint": k.fingerprint, "usage": quota.info(k.fingerprint) if counted else None}


def sources_list() -> list[dict]:
    return [s.to_dict() for s in sources.SOURCES.values()]


def keys_list() -> dict:
    """Every key; ``default`` is the Earth Data Hub default, ``defaults`` the default of each source."""
    return {"default": keys.default_name(), "defaults": {p: keys.default_name(p) for p in sources.SOURCES},
            "keys": [_key_row(k) for k in keys.list_keys()]}


def key_status(name=None, provider: str = sources.EDH) -> dict | None:
    k = _key(name, provider)
    return _key_row(k) if k else None


def key_add(name: str, secret: str, quota_: int | None = None, check: bool = True,
            provider: str = sources.EDH) -> dict:
    try:
        src = sources.get(provider)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    if check:
        ok, msg = asyncio.run(keys.validate(secret, provider))
        if not ok:
            return {"ok": False, "msg": msg}
    try:
        k = keys.add_key(name, secret, None if quota_ is None else int(quota_), provider=provider)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    if check and src.has_quota:
        quota.add(k.fingerprint, 1)
    return {"ok": True, "name": k.name}


def key_test(name: str) -> dict:
    k = keys.find_key(name)
    ok, msg = asyncio.run(keys.validate(k.secret, k.provider))
    if sources.get(k.provider).has_quota:
        quota.add(k.fingerprint, 1)
    return {"ok": ok, "msg": msg}


def key_remove(name: str) -> dict:
    keys.remove_key(name)
    return {"ok": True}


def key_default(name: str) -> dict:
    keys.set_default(name)
    return {"ok": True}


def key_set_quota(name: str, quota_: int) -> dict:
    keys.set_quota(name, int(quota_))
    return {"ok": True}


def key_calibrate(name: str, used: int) -> dict:
    quota.calibrate(keys.get_key(name, sources.EDH).fingerprint, int(used))
    return {"ok": True}


# ------------------------------------------------------------------ catalogue and datasets

def collections(source: str = sources.EDH) -> list[dict]:
    """The dataset groups of a source: Earth Data Hub collections, or Climate Data Store product types."""
    if sources.get(source).id == sources.CDS:
        from .sources.cds import catalog as cds_catalog

        return cds_catalog.categories()
    return catalog.collections()


def datasets(collection: str, source: str = sources.EDH) -> list[dict]:
    if sources.get(source).id == sources.CDS:
        from .sources.cds import catalog as cds_catalog

        return [dict(d, collection=d["category"], deprecated=bool(d["disabled"]), size="")
                for d in cds_catalog.datasets() if d["category"] == collection]
    return catalog.datasets(collection)


def cds_describe(dataset: str, key: str | None = None, refresh: bool = False) -> dict:
    """A CDS dataset's form as the app uses it; with a key, also which of its licences are not accepted yet
    (one free call)."""
    from .sources.cds import catalog as cds_catalog
    from .sources.cds.client import CDSClient

    d = cds_catalog.describe(dataset, refresh)
    d["licences_missing"] = None
    k = _key(key, sources.CDS)
    if k is not None and d["licences"]:
        async def accepted():
            async with CDSClient(k.secret, retries=2) as c:
                return await c.licences()
        try:
            have = asyncio.run(accepted())
            d["licences_missing"] = [l for l in d["licences"] if (l["id"], l["revision"]) not in have]
        except Exception:  # noqa: BLE001  (unknown: the form still works)
            pass
    return d


def cds_check(dataset: str, request: dict) -> dict:
    """Free, local check of a CDS request against the dataset's constraints: ``{field: [values]}`` that cannot
    be delivered (empty: fine), and the values still possible for each field."""
    from .sources.cds import catalog as cds_catalog

    table = cds_catalog.constraints(dataset)
    return {"invalid": cds_catalog.invalid(table, request), "allowed": cds_catalog.allowed(table, request)}


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

CDS_SHAPES_CHECKED = 12  # costing calls per plan at most (one per distinct request size)
_CDS_COSTS: dict[tuple[str, str], dict] = {}  # (dataset, request hash) -> costing answer: the form plans often


def cds_plan(spec: dict) -> dict:
    """The CDS requests a spec needs, checked for free: the dataset's real cost limit and the cost of each
    distinct request size (costing), the constraints (local), the licences (one call). Needs no queue."""
    from .sources.cds import catalog as cds_catalog
    from .sources.cds.client import CDSClient, CDSError
    from .sources.cds.planner import DEFAULT_LIMIT, CdsPlan
    from .util import read_json, write_json_atomic
    from .paths import cache_dir

    try:
        s = JobSpec.from_dict(spec)
        desc = cds_catalog.describe(s.dataset)
        table = cds_catalog.constraints(s.dataset)
        limits_path = cache_dir() / "cds_limits.json"
        limits = read_json(limits_path, {}, dict)
        p = CdsPlan(s, desc, table, limit=float(limits.get(s.dataset) or DEFAULT_LIMIT))
    except (PlanError, ValueError, KeyError) as e:
        return {"ok": False, "error": str(e)}
    k = _key(s.download.key, sources.CDS)
    checked, missing, note = False, None, None

    async def check(p):
        async with CDSClient(k.secret, retries=2) as c:
            async def costing(r):
                key = (s.dataset, r.hash)
                if key not in _CDS_COSTS:
                    _CDS_COSTS[key] = await c.costing(s.dataset, r.inputs)
                    while len(_CDS_COSTS) > 5000:
                        del _CDS_COSTS[next(iter(_CDS_COSTS))]
                return _CDS_COSTS[key]

            have = await c.licences() if desc["licences"] else set()
            biggest = max(p.requests, key=lambda r: r.fields)
            first = await costing(biggest)
            if first["limit"] and first["limit"] != p.limit:  # plan again with the dataset's own limit
                limits[s.dataset] = first["limit"]
                write_json_atomic(limits_path, limits)
                p = CdsPlan(s, desc, table, limit=first["limit"])
            for _ in range(2):
                costs: dict[int, float] = {}
                for r in sorted(p.requests, key=lambda r: -r.fields):
                    if r.fields not in costs and len(costs) < CDS_SHAPES_CHECKED:
                        costs[r.fields] = (await costing(r))["cost"]
                for r in p.requests:
                    r.cost = costs.get(r.fields)
                over = max((r.cost / r.fields for r in p.requests if r.cost and r.fields), default=1.0)
                if all(r.cost is None or r.cost <= p.limit for r in p.requests) or over <= 1:
                    break
                # CDS counts more than the local estimate: plan with a proportionally smaller limit, once
                p = CdsPlan(s, desc, table, limit=p.limit / over)
                p.limit = first["limit"] or p.limit
            return p, [l for l in desc["licences"] if (l["id"], l["revision"]) not in have]

    if k is None:
        note = "add a Climate Data Store key to check the costs and licences"
    else:
        try:
            p, missing = asyncio.run(check(p))
            checked = True
        except CDSError as e:
            note = f"costs not checked: {e}"
    alt = None
    if s.area.mode == "points" and desc["tier"] == "grib":
        ts = f"{s.dataset}-timeseries"
        if any(d["id"] == ts for d in cds_catalog.datasets()):
            alt = {"dataset": ts, "requests": len(s.area.points),
                   "note": "time series for points come from a faster archive: one request per point"}
    return {"ok": True, "summary": p.summary(), "requests": [r.to_dict() for r in p.requests[:500]],
            "invalid": p.invalid(), "licences_missing": missing, "checked": checked, "note": note,
            "alternative": alt}


def plan(spec: dict, connections: int, follow: bool) -> dict:
    """Everything the form shows about a spec: summary, chunk layouts, size estimates, dataset structure."""
    if spec.get("source") == sources.CDS:
        return cds_plan(spec)
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
    src = sources.require_jobs(s.source)
    try:
        keys.get_key(s.download.key, src.id)
    except KeyError as e:
        raise ValueError(e.args[0] if e.args else f"add a {src.title} API key first") from None
    name = s.output.name
    if (not name.strip() or name != name.strip() or name in (".", "..") or "/" in name or "\\" in name
            or "\0" in name):
        raise ValueError("the output name must be a plain file name (no slashes, not empty)")
    if not s.output.directory.strip():
        raise ValueError("choose an output directory")
    if not 1 <= int(s.download.connections) <= 64:
        raise ValueError("connections must be between 1 and 64")
    with runner.file_lock(runner.jobs_dir() / "start.lock"):  # a double click starts one job
        if s.output.target_mode == "extend":
            if not s.icechunk_path.exists():
                raise ValueError(f"target Icechunk repository {s.icechunk_path} does not exist")
        elif s.icechunk_path.exists() or s.zarr_path.exists():
            raise ValueError(f"output {s.output.name} already exists in {s.output.directory}; choose another name")
        mine = {s.icechunk_path.resolve(), s.zarr_path.resolve()}
        for j in runner.list_jobs():
            if j.get("status") in runner.FINISHED or not j.get("spec"):
                continue
            try:
                o = JobSpec.from_dict(j["spec"])
                theirs = {o.icechunk_path.resolve(), o.zarr_path.resolve()}
            except Exception:  # noqa: BLE001
                continue
            if mine & theirs:
                raise ValueError(f"job {j['id']} ({j.get('status')}) already writes this output: resume or "
                                 f"delete it, or choose another name")
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


def job_stop(jid: str, action: str) -> dict:
    runner.stop(runner.resolve(jid), action)
    return {"ok": True}


def job_connections(jid: str, n: int) -> dict:
    runner.set_control(runner.resolve(jid), connections=int(n))
    return {"ok": True}


def job_delete(jid: str) -> dict:
    runner.delete(runner.resolve(jid))
    return {"ok": True}


LOG_TAIL = 2000  # lines shown when the log opens (what the viewer keeps)


def job_log(jid: str, offset: int = 0) -> dict:
    """New log lines from byte ``offset`` (0: the last LOG_TAIL lines); ``next`` is the offset to ask next."""
    p = runner.resolve(jid) / "log.txt"
    if not p.exists():
        return {"lines": [], "next": offset}
    with open(p, "rb") as f:
        size = f.seek(0, 2)
        if offset > size:  # log replaced
            offset = 0
        start = offset if offset else max(0, size - LOG_TAIL * 400)
        f.seek(start)
        data = f.read()
    cut = data.rfind(b"\n") + 1  # only whole lines; the rest comes next time
    lines = data[:cut].decode(errors="replace").splitlines()
    if not offset and start:
        lines = lines[1:]  # first line was cut in the middle
    return {"lines": lines[-LOG_TAIL:] if not offset else lines, "next": start + cut}


def store_structure(path: str) -> dict:
    p = Path(path)
    key = (str(p), (p / "zarr.json").stat().st_mtime if (p / "zarr.json").exists() else p.stat().st_mtime)
    with _LOCK:
        hit = _STORE_CACHE.pop(key, None)
    if hit is None:
        hit = structure.from_store(p)
    with _LOCK:
        _STORE_CACHE[key] = hit  # most recent last
        for k in [k for k in _STORE_CACHE if k[0] == key[0] and k != key]:  # store changed: drop the old view
            del _STORE_CACHE[k]
        while len(_STORE_CACHE) > 16:
            del _STORE_CACHE[next(iter(_STORE_CACHE))]
    return hit


def store_inspect(path: str) -> dict:
    return store.inspect_icechunk_for_extend(path)


METHODS = {f.__name__: f for f in (
    hello, sources_list, keys_list, key_status, key_add, key_test, key_remove, key_default, key_set_quota, key_calibrate,
    collections, datasets, cds_describe, cds_check, cds_plan, dataset_load, plan, sample_run, job_start, jobs_list, job_resume, job_stop,
    job_connections, job_delete, job_log, store_structure, store_inspect)}
