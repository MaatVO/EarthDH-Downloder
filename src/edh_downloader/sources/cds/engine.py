"""Run one Climate Data Store job (detached worker, like the Earth Data Hub engine).

Pipeline:

* a **ledger** (``requests.json`` in the job folder) records every planned request and what happened to it:
  ``planned → submitting → queued → ready → downloaded → done``. It is written *before* each step, so a job
  that is paused, killed or crashes never submits a request twice: queued requests are polled again on resume,
  a submission cut short is found again on the CDS by its request, an expired result is requested again (often
  served from the CDS cache);
* a **queue** keeps up to ``max_inflight`` requests waiting or running on the CDS. When the CDS refuses more
  (HTTP 429 or a "temporarily limited" failure) it halves that number and backs off; after a run of successes
  it raises it again, up to the job's setting. All waiting requests are polled with one call, less often while
  nothing changes;
* results are **downloaded** as soon as they are ready (they expire after some days), resuming broken
  downloads, into the spool (bounded by ``max_spool_gb``);
* a **processor** thread waits until a unit's files are all there, decodes them message by message (one
  variable and level at a time), cuts the area, aggregates time and commits the unit to Icechunk; then deletes
  files no later unit needs. When every unit is committed the repository is exported to Zarr.

NetCDF products are downloaded whole first (their time axis and variable names are only known from their files),
then read (``netcdf.py``: zip and tar results are unpacked), cut, aggregated and committed unit by unit as GRIB is.
When the files are not a grid one store can hold (tables, station series, several products), and for the other
datasets (time series, in-situ tables), they are kept as the CDS delivered them, in ``<output>/<name>.files/``
with a ``manifest.json``.
"""

import asyncio
import json
import os
import shutil
import signal
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import zarr

from ... import keys, store
from ...aggregate import reduce_periods
from ...engine import Engine, dir_bytes, now_iso
from ...http import Fatal
from ...job import JobSpec
from ...util import human_bytes, read_json, write_json_atomic
from .. import CDS
from ...planner import PlanError
from . import catalog, grib, netcdf
from .client import (AuthError, BadRequest, CDSClient, CDSError, JobFailed, NoSuchJob, ResultsExpired,
                     Throttled, canonical, is_throttle)
from .output import CdsOutput, Timeline
from .planner import CdsPlan, default_limit, fit_costs

MAX_ATTEMPTS = 3  # a request that fails this often (not because of a queue limit) stops the job
POLL_MIN, POLL_MAX = 5.0, 120.0
BACKOFF_MIN, BACKOFF_MAX = 60.0, 1800.0
RAISE_AFTER = 3  # successes before one more request may wait on the CDS
DOWNLOADS = 2  # result files downloaded at once
RETRY_WAIT = 30.0  # seconds before a failed download is tried again
DOWNLOAD_ATTEMPTS = 5  # failed downloads of one result before it is asked for again (a new link)
RELINKS = 2  # new links asked for one result before its download failures stop the job
SHAPES_CHECKED = 12  # costing calls per plan at most (one per distinct request size)
SLAB_SHARE = 6  # one slab of source steps may use this share of the memory budget (decoding, cutting and
# reducing hold a few copies of it)


def period_groups(starts, ends, cap: int):
    """Consecutive periods ``[p0, p1)`` whose steps fit ``cap`` together; a longer period comes alone."""
    n, p0 = len(starts), 0
    while p0 < n:
        p1 = p0 + 1
        while p1 < n and ends[p1] - starts[p0] <= cap:
            p1 += 1
        yield p0, p1
        p0 = p1


class Running:
    """Statistics of one period accumulated slab by slab (NaN-aware, as ``reduce_periods``)."""

    def __init__(self, stats):
        self.stats = set(stats)
        self.first = self.total = self.count = self.lo = self.hi = None

    def add(self, a: np.ndarray) -> None:
        if self.first is None:
            self.first = a[0].copy()
        if self.stats & {"sum", "mean"}:
            valid = ~np.isnan(a)
            t = np.where(valid, a, 0).sum(axis=0, dtype=np.float64)
            c = valid.sum(axis=0)
            self.total = t if self.total is None else self.total + t
            self.count = c if self.count is None else self.count + c
        if "min" in self.stats:
            lo = np.fmin.reduce(a, axis=0)
            self.lo = lo if self.lo is None else np.fmin(self.lo, lo)
        if "max" in self.stats:
            hi = np.fmax.reduce(a, axis=0)
            self.hi = hi if self.hi is None else np.fmax(self.hi, hi)

    def result(self, stat: str) -> np.ndarray:
        if stat in ("first", "none"):
            return self.first
        if stat == "min":
            return self.lo
        if stat == "max":
            return self.hi
        with np.errstate(invalid="ignore", divide="ignore"):
            if stat == "sum":
                return np.where(self.count > 0, self.total, np.nan)
            if stat == "mean":
                return self.total / self.count
        raise ValueError(f"unknown statistic {stat}")


class Ledger:
    """Every request of the job and its state, saved atomically on each change."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        d = read_json(path, {}, dict)
        self.dataset = d.get("dataset")
        self.period: list[str] | None = d.get("period")  # first and last instant of the job (frozen)
        self.entries: list[dict] = d.get("requests") or []

    def init(self, dataset: str, requests: list, period: list[str] | None = None) -> None:
        self.dataset = dataset
        self.period = period
        self.entries = [{"index": r.index, "hash": r.hash, "inputs": r.inputs, "start": r.start, "end": r.end,
                         "fields": r.fields, "variables": r.variables, "levels": r.levels,
                         "bytes_est": r.bytes_est, "state": "planned", "job_id": None, "attempts": 0}
                        for r in requests]
        self.save()

    def save(self) -> None:
        with self.lock:
            write_json_atomic(self.path, {"dataset": self.dataset, "period": self.period, "requests": self.entries})

    def set(self, e: dict, **kw) -> None:
        with self.lock:
            e.update(kw)
        self.save()

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e["state"]] = out.get(e["state"], 0) + 1
        return out


class CdsEngine(Engine):
    """Same job folder, state, control, stop handling, export and finish as ``Engine``; another pipeline."""

    def run(self) -> str:
        spec = self.spec
        self.save_state(status="starting", pid=os.getpid(), started_at=now_iso(), error=None, message="", activity={"stage": "planning"})
        signal.signal(signal.SIGTERM, lambda *_: self.request_stop(self.control().get("action", "pause")))
        signal.signal(signal.SIGINT, lambda *_: self.request_stop("pause"))
        try:
            if self.state.get("exported") and spec.zarr_path.exists():
                if not spec.output.keep_icechunk:
                    self._remove_icechunk()
                self._done()
                return "completed"
            if spec.output.target_mode == "extend":
                raise Fatal("extending an existing store is not available for Climate Data Store jobs yet")
            self.key = keys.get_key(spec.download.key, CDS)
            self.desc = catalog.describe(spec.dataset)
            tier = self.desc["tier"]
            missing = None  # the decoder this job needs, when it cannot be loaded: before anything is planned
            try:
                if tier == "grib":
                    grib._eccodes()
                elif tier == "netcdf" and not self.state.get("kept_files"):
                    netcdf.available()
            except RuntimeError as e:
                pkg = "eccodes" if tier == "grib" else "netCDF4"
                missing = Fatal(f"{e}. Install it (pip install {pkg}) where the job runs, then resume")
            if missing and tier == "grib":
                raise missing
            self.ledger = Ledger(self.dir / "requests.json")
            self.cds_plan = self._make_plan()
            # NetCDF: converted unless the selection cannot fit a store, or an earlier run found the files cannot
            self.nc = tier == "netcdf" and self.cds_plan.convert and not self.state.get("kept_files")
            self.files_only = tier != "grib" and not self.nc
            if missing and self.nc:  # still before any request is queued
                raise missing
            self.save_state(cds_tier=tier, cds_convert=not self.files_only, requests_total=len(self.ledger.entries),
                            plan=self.cds_plan.summary())
            if self.files_only:
                self.units, self.unit_need = [], {}
            elif self.nc:  # units are known once every file is there
                self.units, self.unit_need = [], {}
                self._open_output()
            else:
                tl = Timeline(self.cds_plan)
                self.timeline = tl
                self.units = tl.units
                self.unit_need = {u.index: tl.unit_requests(u) for u in tl.units}
                self.save_state(units_total=len(self.units))
                self._open_output()
            self.time_offset = 0
            asyncio.run(self._pipeline())
            if self.errors:
                raise Fatal("; ".join(self.errors[:3]))
            if self.stop_event.is_set() and not self._all_done():
                status = {"cancel": "cancelled"}.get(self.stop_reason, "paused")
                self.log.info(f"stopped ({status})")
                self.save_state(status=status, pid=None, message=f"stopped: {self.stop_reason}")
                return status
            if self.files_only:
                self._done_files()
            elif self.nc:
                return self._convert_nc()
            else:
                self._finish()
            return "completed"
        except Exception as e:  # noqa: BLE001
            self.log.exception(f"job failed: {e}")
            self.save_state(status="failed", pid=None, error=f"{type(e).__name__}: {e}")
            return "failed"

    def _open_output(self):
        """The output of an earlier run (resume), with its repository; None on a first run."""
        spec = self.spec
        self.output = CdsOutput.load(self.cds_plan, self.dir)
        self.repo = store.open_repo(spec.icechunk_path) if self.output is not None else None
        if self.output is not None and self.repo is None:
            raise Fatal(f"{spec.icechunk_path} is missing: the job cannot continue (delete it and start again)")
        if self.output is None and spec.icechunk_path.exists() and any(spec.icechunk_path.iterdir()):
            raise Fatal(f"{spec.icechunk_path} exists and is not this job's repository; choose another output name")
        self.plan = self.output

    # ---------------------------------------------------------------- plan and checks
    def _make_plan(self) -> CdsPlan:
        """The plan of the first run, kept in the ledger: a resumed job asks for exactly the same requests."""
        spec, desc = self.spec, self.desc
        table = catalog.constraints(spec.dataset)
        limits = read_json(self._limits_path(), {}, dict)
        frozen = tuple(self.ledger.period) if self.ledger.entries and self.ledger.period else None
        plan = CdsPlan(spec, desc, table, limit=default_limit(spec.dataset, limits), frozen=frozen)
        for w in plan.warnings:
            self.log.warning(w)
        if self.ledger.entries:
            from .planner import CdsRequest

            plan.requests = [CdsRequest(e["index"], e["inputs"], e["hash"], e["start"], e["end"], e["fields"],
                                        e["variables"], e["levels"], e.get("bytes_est")) for e in self.ledger.entries]
            return plan
        bad = plan.invalid()
        if bad:
            i, fields = next(iter(bad.items()))
            raise Fatal(f"request {i + 1} asks for data the dataset does not have: "
                        + "; ".join(f"{k} {v}" for k, v in fields.items()))
        plan = asyncio.run(self._check_costs(plan, table, limits))
        period = [str(plan.instants[0]), str(plan.instants[-1])] if plan.instants is not None else None
        self.ledger.init(spec.dataset, plan.requests, period)
        s = plan.summary()
        self.log.info(f"plan: {s['requests']} request{'s' if s['requests'] != 1 else ''}, {s['fields']:,} fields"
                      + (f", ~{human_bytes(s['download_bytes_est'])} to download" if s["download_bytes_est"] else ""))
        return plan

    def _limits_path(self) -> Path:
        from ...paths import cache_dir

        return cache_dir() / "cds_limits.json"

    async def _check_costs(self, plan: CdsPlan, table, limits) -> CdsPlan:
        spec = self.spec
        async with CDSClient(self.key.secret, retries=3) as c:
            if self.desc["licences"]:
                have = await c.licences()
                missing = [l for l in self.desc["licences"] if (l["id"], l["revision"]) not in have]
                if missing:
                    raise Fatal("accept the licence of this dataset first: "
                                + ", ".join(l["label"] for l in missing)
                                + f" (on https://cds.climate.copernicus.eu/datasets/{spec.dataset}, tab Download)")
            seen: dict[str, dict] = {}

            async def costing(r):
                if r.hash not in seen:
                    seen[r.hash] = await c.costing(spec.dataset, r.inputs)
                return seen[r.hash]

            plan = await fit_costs(plan, lambda lim: CdsPlan(spec, self.desc, table, limit=lim), costing,
                                   SHAPES_CHECKED)
            if plan.limit != limits.get(spec.dataset):
                limits[spec.dataset] = plan.limit
                write_json_atomic(self._limits_path(), limits)
            over = [r for r in plan.requests if r.cost is not None and r.cost > plan.limit]
            if over:
                raise Fatal(f"request {over[0].index + 1} costs {over[0].cost:,.0f}, over the dataset's limit "
                            f"{plan.limit:,.0f}: select fewer variables or levels")
        return plan

    # ---------------------------------------------------------------- pipeline
    def _all_done(self) -> bool:
        if self.files_only or self.nc:  # every file downloaded (a NetCDF job converts them afterwards)
            return all(e["state"] in ("downloaded", "done") for e in self.ledger.entries)
        return len(set(self.state["units_done"])) >= len(self.units)

    def _needed(self) -> list[dict]:
        """Requests some unfinished unit still needs (all of them for a files-only or NetCDF job)."""
        if self.files_only or self.nc:
            return [e for e in self.ledger.entries if e["state"] not in ("downloaded", "done")]
        done = set(self.state["units_done"])
        want = sorted({r for u in self.units if u.index not in done for r in self.unit_need[u.index]})
        return [self.ledger.entries[i] for i in want]

    def _file_for(self, e: dict) -> Path:
        if self.files_only:
            return self.files_dir() / self._file_name(e)
        return self.spool_dir() / f"{e['index']:05d}-{e['hash']}.{'grib' if not self.nc else self._ext(e)}"

    @staticmethod
    def _ext(e: dict) -> str:
        ext = {"application/x-grib": "grib", "application/netcdf": "nc", "application/x-netcdf": "nc",
               "application/zip": "zip", "text/csv": "csv"}.get(e.get("type") or "", None)
        if not ext:
            ext = (e.get("href") or "").rsplit(".", 1)[-1] if "." in (e.get("href") or "")[-6:] else "bin"
        return ext

    def _file_name(self, e: dict) -> str:
        return f"{self.spec.output.name}_{e['index'] + 1:04d}_{e['start'][:10]}_{e['end'][:10]}.{self._ext(e)}"

    def files_dir(self) -> Path:
        return self.spec.output_dir / f"{self.spec.output.name}.files"

    async def _pipeline(self):
        spec = self.spec
        led = self.ledger
        loop = asyncio.get_running_loop()
        self.spool_dir().mkdir(parents=True, exist_ok=True)
        if self.files_only:
            self.files_dir().mkdir(parents=True, exist_ok=True)
        # files that should be there: check them (a crash can leave anything behind)
        for e in led.entries:
            if e["state"] == "downloaded":
                f = Path(e.get("file") or "")
                if not (f.exists() and (e.get("size") is None or f.stat().st_size == e["size"])):
                    led.set(e, state="ready" if e.get("href") else "planned", file=None)
        done_units = set(self.state["units_done"])
        todo = [u for u in self.units if u.index not in done_units]
        ready = {u.index: threading.Event() for u in todo}
        wake = asyncio.Event()  # something changed: the queue loop looks again at once
        stop_async = asyncio.Event()
        stats = {"bytes": 0, "submitted": 0}
        processed = {"n": len(done_units), "current": None}

        def check_ready():
            for u in todo:
                if not ready[u.index].is_set() and all(
                        led.entries[r]["state"] in ("downloaded", "done") for r in self.unit_need[u.index]):
                    ready[u.index].set()

        check_ready()
        self.activity(force=True, stage="queue", downloads=None, unit=None)

        # ---------- processor thread
        def processor():
            try:
                for u in todo:
                    while not ready[u.index].wait(1):
                        if self.stop_event.is_set():
                            return
                    if self.stop_event.is_set():
                        return
                    processed["current"] = u.index
                    t0 = time.time()
                    self._process_unit(u)
                    with self._state_lock:
                        self.state["units_done"].append(u.index)
                    processed["n"] += 1
                    self.activity(force=True, unit=None)
                    self.save_state()
                    later = {r for v in todo if v.index > u.index for r in self.unit_need[v.index]}
                    for r in self.unit_need[u.index]:
                        if r not in later:
                            e = led.entries[r]
                            Path(e.get("file") or "").unlink(missing_ok=True)
                            led.set(e, state="done", file=None)
                    self.log.info(f"unit {u.index + 1}/{len(self.units)} committed "
                                  f"({self.output.labels[u.k0]} .. {self.output.labels[u.k1 - 1]}) "
                                  f"in {time.time() - t0:.1f}s")
            except Exception as e:  # noqa: BLE001
                self.log.exception(f"processing failed: {e}")
                self.errors.append(f"processing: {type(e).__name__}: {e}")
                self.request_stop("error")
            finally:
                loop.call_soon_threadsafe(stop_async.set)
                loop.call_soon_threadsafe(wake.set)

        proc = None
        if not self.files_only and todo:
            proc = threading.Thread(target=processor, name="processor", daemon=True)
            proc.start()

        # ---------- queue
        ctl_cap = lambda: max(1, int(self.control().get("max_inflight") or spec.cds.max_inflight))  # noqa: E731
        q = {"cap": ctl_cap(), "backoff_until": 0.0, "backoff": BACKOFF_MIN, "streak": 0, "poll": POLL_MIN,
             "downloading": set()}

        def throttle(why: str):
            active = sum(1 for e in led.entries if e["state"] == "queued")
            q["cap"] = max(1, min(q["cap"], max(1, active)) // 2)
            q["backoff_until"] = time.time() + q["backoff"]
            self.log.warning(f"the CDS limits requests for now ({why}); at most {q['cap']} waiting, "
                             f"next submission in {q['backoff']:.0f}s")
            q["backoff"] = min(BACKOFF_MAX, q["backoff"] * 2)
            q["streak"] = 0

        def fatal(msg: str):
            self.log.error(msg)
            self.errors.append(msg)
            self.request_stop("error")

        async def reconcile(c: CDSClient):
            """A submission cut short (crash between the POST and the ledger): find the job on the CDS."""
            pending = [e for e in led.entries if e["state"] == "submitting"]
            if not pending:
                return
            jobs = await c.jobs(limit=100, max_pages=2)
            known = {e["job_id"] for e in led.entries if e.get("job_id")}
            for e in pending:
                found = None
                for j in jobs:
                    if j.get("processID") != led.dataset or j["jobID"] in known:
                        continue
                    try:
                        full = await c.job(j["jobID"], request=True)
                    except CDSError:
                        continue
                    ids = ((full.get("metadata") or {}).get("request") or {}).get("ids") or {}
                    if canonical(ids) == e["inputs"]:
                        found = j["jobID"]
                        break
                if found:
                    known.add(found)
                    led.set(e, state="queued", job_id=found)
                    self.log.info(f"request {e['index'] + 1}: found the submission of the last run ({found[:8]})")
                else:
                    led.set(e, state="planned", job_id=None)

        async def poll(c: CDSClient) -> bool:
            queued = [e for e in self._needed() if e["state"] == "queued" and e.get("job_id")]
            if not queued:
                return False
            st = await c.statuses([e["job_id"] for e in queued])
            changed = False
            for e in queued:
                j = st.get(e["job_id"])
                if j is None:  # dismissed on the CDS (or unknown): ask again
                    self.log.warning(f"request {e['index'] + 1}: job {e['job_id'][:8]} is gone on the CDS; "
                                     "submitting it again")
                    led.set(e, state="planned", job_id=None)
                    changed = True
                    continue
                status = j.get("status")
                if status != e.get("status"):
                    led.set(e, status=status, started=j.get("started"))
                    changed = True
                if status == "successful":
                    try:
                        res = await c.results(e["job_id"])
                    except ResultsExpired:
                        led.set(e, state="planned", job_id=None)
                        continue
                    except JobFailed as err:
                        await failed(c, e, str(err))
                        continue
                    led.set(e, state="ready", href=res["href"], size=res.get("file:size"), type=res.get("type"),
                            checksum=res.get("file:checksum"), dl_failures=0,
                            finished=j.get("finished") or now_iso())
                    q["streak"] += 1
                    self.log.info(f"request {e['index'] + 1}/{len(led.entries)} ready "
                                  f"({human_bytes(res.get('file:size') or 0)})")
                elif status in ("failed", "rejected"):
                    await failed(c, e, await c.failure(e["job_id"]))
            return changed

        async def failed(c, e, why: str):
            if is_throttle(why):
                led.set(e, state="planned", job_id=None, status=None)
                throttle(why[:120])
                return
            n = e.get("attempts", 0) + 1
            if n >= MAX_ATTEMPTS:
                led.set(e, state="failed", attempts=n, error=why[:500])
                fatal(f"request {e['index'] + 1} failed {n} times on the CDS: {why[:300]}")
            else:
                led.set(e, state="planned", job_id=None, status=None, attempts=n, error=why[:500])
                self.log.warning(f"request {e['index'] + 1} failed on the CDS ({why[:200]}); trying again")

        def spool_full() -> bool:
            """Results on disk (or about to be) fill the spool; requests still waiting on the CDS take no disk
            yet, so they may add up to one more spool's worth: the queue keeps moving while units are
            processed, without filling the disk if everything finishes at once."""
            if self.files_only or self.nc:  # every file is needed before anything is converted
                return False
            size = lambda e: e.get("size") or e.get("bytes_est") or 0  # noqa: E731
            disk = sum(size(e) for e in led.entries if e["state"] in ("ready", "downloaded"))
            waiting = sum(size(e) for e in led.entries if e["state"] == "queued")
            budget = spec.download.max_spool_gb * 1e9
            return disk >= budget or disk + waiting >= 2 * budget

        async def submit(c: CDSClient) -> bool:
            if time.time() < q["backoff_until"] or self.stop_event.is_set():
                return False
            if q["streak"] >= RAISE_AFTER and q["cap"] < ctl_cap():
                q["cap"] += 1
                q["streak"] = 0
                q["backoff"] = BACKOFF_MIN
            q["cap"] = min(q["cap"], ctl_cap())
            active = sum(1 for e in led.entries if e["state"] == "queued")
            changed = False
            for e in self._needed():
                if active >= q["cap"]:
                    break
                if e["state"] != "planned":
                    continue
                if active and spool_full():
                    break
                led.set(e, state="submitting", submitted_at=now_iso())
                try:
                    j = await c.submit(led.dataset, e["inputs"])
                except Throttled as err:
                    led.set(e, state="planned")
                    throttle(str(err)[:120])
                    break
                except BadRequest as err:
                    led.set(e, state="failed", error=str(err)[:500])
                    fatal(f"request {e['index'] + 1} refused by the CDS: {err}")
                    break
                led.set(e, state="queued", job_id=j["jobID"], status=j.get("status"))
                stats["submitted"] += 1
                active += 1
                changed = True
                self.log.info(f"request {e['index'] + 1}/{len(led.entries)} submitted "
                              f"({e['start'][:10]} .. {e['end'][:10]}, {e['fields']:,} fields)"
                              + (": served from the CDS cache" if j.get("status") == "successful" else ""))
            return changed

        async def download(c: CDSClient, e: dict):
            try:
                dest = self._file_for(e)
                n = await c.download(e["href"], dest, e.get("size"),
                                     progress=lambda k: stats.__setitem__("bytes", stats["bytes"] + k),
                                     checksum=e.get("checksum"))
                led.set(e, state="downloaded", file=str(dest), downloaded_at=now_iso(), relinks=0)
                self.log.info(f"request {e['index'] + 1}: downloaded {human_bytes(n)}")
                await loop.run_in_executor(None, check_ready)
            except ResultsExpired:
                self.log.warning(f"request {e['index'] + 1}: the result expired; asking for it again")
                led.set(e, state="planned", job_id=None, href=None)
            except CDSError as err:
                n = e.get("dl_failures", 0) + 1
                if n >= DOWNLOAD_ATTEMPTS and e.get("relinks", 0) >= RELINKS:
                    led.set(e, dl_failures=0)
                    fatal(f"request {e['index'] + 1}: download failed {n * (RELINKS + 1)} times, also from new "
                          f"links ({err}); the job stops so the same file is not fetched again and again")
                elif n >= DOWNLOAD_ATTEMPTS:  # the link itself may be bad: ask for the result again
                    self.log.warning(f"request {e['index'] + 1}: download failed {n} times ({err}); asking the "
                                     "CDS for it again")
                    led.set(e, state="planned", job_id=None, href=None, dl_failures=0,
                            relinks=e.get("relinks", 0) + 1)
                else:
                    led.set(e, dl_failures=n)
                    self.log.warning(f"request {e['index'] + 1}: download failed ({err}); trying again")
                    await asyncio.sleep(RETRY_WAIT)
            finally:
                q["downloading"].discard(e["index"])
                wake.set()

        async def cancel_remote(c: CDSClient):
            for e in led.entries:
                if e["state"] == "queued" and e.get("job_id"):
                    try:
                        await c.delete(e["job_id"])
                    except CDSError:
                        pass
                    led.set(e, state="planned", job_id=None, status=None)

        def progress():
            counts = led.counts()
            now = pd.Timestamp.now(tz="UTC").tz_localize(None)
            waits = [(now - pd.Timestamp(e["submitted_at"]).tz_localize(None)).total_seconds()
                     for e in led.entries if e["state"] == "queued" and e.get("submitted_at")
                     and e.get("status") in (None, "accepted")]
            running = sum(1 for e in led.entries if e["state"] == "queued" and e.get("status") == "running")
            waiting = counts.get("queued", 0) - running
            n_units = len(self.units)
            if self.files_only or self.nc:
                got = counts.get("downloaded", 0) + counts.get("done", 0)
                msg = f"{got}/{len(led.entries)} files downloaded"
            else:
                msg = f"{processed['n']}/{n_units} units committed"
            msg += f" · {waiting} waiting on the CDS, {running} running" if waiting or running else ""
            if time.time() < q["backoff_until"]:
                msg += f" · CDS limit: next submission in {q['backoff_until'] - time.time():.0f}s"
            self.save_state(status="running", cds_requests=counts, cds_waiting=waiting, cds_running=running,
                            cds_inflight_cap=q["cap"], cds_oldest_wait_s=max(waits) if waits else None,
                            units_processed=processed["n"], bytes_run=stats["bytes"],
                            requests_run=stats["submitted"], spool_bytes=dir_bytes(self.spool_dir()),
                            message=msg)

        async def queue_loop():
            async with CDSClient(self.key.secret, retries=4) as c:
                try:
                    await reconcile(c)
                    while not self.stop_event.is_set():
                        if self._all_done() and not q["downloading"]:
                            break
                        if stop_async.is_set() and (proc is None or not proc.is_alive()):
                            break  # the processor ended (all committed, or an error)
                        changed = await poll(c)
                        changed |= await submit(c)
                        for e in self._needed():
                            if (e["state"] == "ready" and e["index"] not in q["downloading"]
                                    and len(q["downloading"]) < DOWNLOADS):
                                q["downloading"].add(e["index"])
                                asyncio.create_task(download(c, e))
                                changed = True
                        progress()
                        q["poll"] = POLL_MIN if changed else min(POLL_MAX, q["poll"] * 1.5)
                        wake.clear()
                        try:
                            await asyncio.wait_for(wake.wait(), timeout=q["poll"])
                        except asyncio.TimeoutError:
                            pass
                    while q["downloading"]:
                        await asyncio.sleep(0.2)
                    if self.stop_reason == "cancel":
                        await cancel_remote(c)
                except AuthError as e:
                    fatal(str(e))
                except Exception as e:  # noqa: BLE001  (the queue must never die silently)
                    self.log.exception(f"queue failed: {e}")
                    fatal(f"queue: {type(e).__name__}: {e}")
                finally:
                    if proc is None:
                        stop_async.set()

        async def watch_stop():
            while not self.stop_event.is_set() and not stop_async.is_set():
                await asyncio.sleep(0.5)
            wake.set()

        speeds: dict[int, dict] = {}  # per download: bytes and time at the last look, smoothed speed

        async def ticker():
            """Once a second, the files being downloaded (bytes on disk, so a resumed file counts what it already
            has) and where the requests are, for the job card."""
            while True:
                await asyncio.sleep(1.0)
                now = time.time()
                rows = []
                for i in sorted(q["downloading"]):
                    e = led.entries[i]
                    dest = self._file_for(e)
                    part = dest.with_name(dest.name + ".part")
                    try:
                        have = part.stat().st_size if part.exists() else dest.stat().st_size if dest.exists() else 0
                    except OSError:
                        have = 0
                    sp = speeds.setdefault(i, {"t": now, "b": have, "rate": 0.0, "since": now_iso()})
                    if now - sp["t"] >= 0.9:
                        r = (have - sp["b"]) / (now - sp["t"])
                        sp["rate"] = r if not sp["rate"] else 0.6 * sp["rate"] + 0.4 * r
                        sp["t"], sp["b"] = now, have
                    rows.append({"request": i + 1, "start": e["start"], "end": e["end"], "bytes": have,
                                 "size": e.get("size"), "rate_bps": max(0.0, sp["rate"]), "since": sp["since"]})
                for i in [k for k in speeds if k not in q["downloading"]]:
                    del speeds[i]
                c = led.counts()
                self.activity(stage="downloading" if rows else "queue", downloads=rows or None,
                              requests={k: c.get(k, 0) for k in ("planned", "submitting", "queued", "ready",
                                                                 "downloaded", "done", "failed")})

        ws = asyncio.create_task(watch_stop())
        tk = asyncio.create_task(ticker())
        await queue_loop()
        if proc is not None:
            await loop.run_in_executor(None, proc.join)
        ws.cancel()
        tk.cancel()
        self.activity(force=True, downloads=None)
        self.state["requests_job"] = self.state.get("requests_job", 0) + stats["submitted"]
        self.state["bytes_job"] = self.state.get("bytes_job", 0) + stats["bytes"]
        self.save_state(cds_requests=led.counts(), cds_waiting=0, cds_running=0, cds_oldest_wait_s=None)

    # ---------------------------------------------------------------- processing
    def _ensure_output(self, files: list[Path]):
        """The layout, from the first unit's files: the grid, and one output variable per GRIB parameter."""
        if self.output is not None:
            return
        msgs = [m for f in files for m in grib.index(f)]
        if not msgs:
            raise Fatal("the first result file holds no GRIB fields")
        g = grib.grid(files[0], msgs[0])
        variables = CdsOutput.describe_variables(msgs, self.cds_plan)
        unmatched = [v for v in self.cds_plan.variables if v not in {x.get("form_name") for x in variables}]
        if unmatched and any(self.spec.time.stats.get(v) for v in unmatched):
            self.log.warning(f"could not tell which GRIB field is {', '.join(unmatched)}: their statistics use "
                             "the default (sum for accumulations, mean otherwise)")
        out = CdsOutput(self.cds_plan, g, variables)
        out.save(self.dir, variables)
        out.remember(variables)
        for w in out.warnings:
            if w not in self.cds_plan.warnings:
                self.log.warning(w)
        self.log.info("output variables: " + ", ".join(f"{v['name']} ({v.get('form_name') or v['long_name']})"
                                                       for v in variables))
        self.log.info(f"creating Icechunk repository {self.spec.icechunk_path}")
        self.repo = store.create_repo(self.spec.icechunk_path, out, self.job_id)
        self.output = self.plan = out

    def _process_unit(self, u):
        """Decode, cut, aggregate and commit one unit of a GRIB job."""
        led = self.ledger
        files = [Path(led.entries[r]["file"]) for r in self.unit_need[u.index]]
        self._ensure_output(files)
        out = self.output
        steps = out.instants[u.s:u.e]
        tidx = {pd.Timestamp(t): i for i, t in enumerate(steps)}
        level_kind = {vp.var.param: vp.var.kind == "level" for vp in out.vars}
        groups: dict[tuple, dict[int, tuple]] = {}
        for f in files:
            for m in grib.index(f):
                i = tidx.get(pd.Timestamp(m.valid))
                if i is None:  # outside the job's period (a month asked for whole): not stored
                    continue
                key = (m.param, float(m.level) if level_kind.get(m.param) else None)
                got = groups.setdefault(key, {})
                if i in got:
                    raise Fatal(f"the CDS delivered several fields of {m.cf_name or m.short_name} for {m.valid}"
                                + (f" at level {m.level:g}" if key[1] is not None else "")
                                + " (ensemble members, several products, lead times or levels): one store holds "
                                  "one value per variable, level and time; change the selection")
                got[i] = (f, m)
        readers = {f: grib.Reader(f) for f in files}

        def sources():
            for vp in out.vars:
                levels = out.level_values if vp.var.kind == "level" else [None]
                for li, lev in enumerate(levels):
                    by_step = groups.get((vp.var.param, None if lev is None else float(lev)), {})
                    if not by_step:
                        self.log.warning(f"no data for {vp.var.name}" + (f" at {lev}" if lev is not None else "")
                                         + f" in {steps[0]} .. {steps[-1]}")
                        continue
                    m0 = next(iter(by_step.values()))[1]

                    def fill(a, s0, s1, by_step=by_step):
                        for i in range(s0, s1):
                            fm = by_step.get(i)
                            if fm is not None:
                                a[i - s0] = readers[fm[0]].read(fm[1])

                    yield vp, (None if lev is None else li), (m0.nj, m0.ni), fill

        try:
            self._commit(u, sources())
        finally:
            for r in readers.values():
                r.close()

    def _process_unit_nc(self, u, index: dict):
        """Read, cut, aggregate and commit one unit of a NetCDF job. ``index`` maps each variable's times to the
        file and position holding them; consecutive steps of one file are read together."""
        out = self.output
        steps = out.instants[u.s:u.e]
        ny, nx = out.grid.shape
        reader = netcdf.Reader()

        def sources():
            for vp in out.vars:
                where = index.get(vp.var.attrs.get("source_variable", vp.var.name), {})
                by_step = {i: where[t] for i, t in enumerate(steps) if t in where}
                if not by_step:
                    continue
                for li in (range(len(out.level_values)) if vp.var.kind == "level" else [None]):
                    def fill(a, s0, s1, li=li):
                        i = s0
                        while i < s1:
                            if i not in by_step:
                                i += 1
                                continue
                            f, p = by_step[i]
                            j = i + 1
                            while j < s1 and j in by_step and by_step[j][0] is f and by_step[j][1] == p + (j - i):
                                j += 1
                            a[i - s0:j - s0] = reader.read(f, p, p + (j - i), li)
                            i = j

                    yield vp, li, (ny, nx), fill

        try:
            self._commit(u, sources())
        finally:
            reader.close()

    def _commit(self, u, sources):
        """Write one unit from ``sources`` (output variable, level position, source field shape, and
        ``fill(a, s0, s1)`` putting source steps ``[s0, s1)`` of the unit into ``a``), then commit it. The source
        steps are read in slabs that fit the memory budget (whole periods together; a period longer than a slab
        is accumulated slab by slab), so neither the size of a request nor the length of a period (a year of
        hourly global fields) needs the memory."""
        out = self.output
        starts = np.asarray(out.p_start[u.k0:u.k1] - u.s)
        ends = np.asarray(out.p_end[u.k0:u.k1] - u.s)
        keepbits = self.spec.output.keepbits
        session = self.repo.writable_session("main")
        g = zarr.open_group(session.store, mode="r+")
        space = (slice(None),) * len(out.shape_space)
        items = list(sources)
        steps = u.e - u.s
        info = {"index": u.index + 1, "of": len(self.units), "first": str(out.labels[u.k0]),
                "last": str(out.labels[u.k1 - 1]), "periods": u.k1 - u.k0, "item": 0, "items": len(items),
                "var": None, "level": None, "steps_done": 0, "steps": steps, "phase": "reading", "since": now_iso(),
                "requests": [r + 1 for r in (self.unit_need.get(u.index) if self.unit_need else
                                             [e["index"] for e in self.ledger.entries])]}
        self.activity(force=True, stage="processing", unit=dict(info))
        for k, (vp, li, (nj, ni), fill) in enumerate(items):
            cap = max(1, int(self.spec.download.memory_gb * 1e9 / SLAB_SHARE // (nj * ni * 4)))
            lpos = () if li is None else (li,)
            info.update(item=k + 1, var=", ".join(vp.out_names), steps_done=0,
                        level=None if li is None else out.level_values[li])

            def slab(s0, s1):
                info["phase"] = "reading"
                self.activity(unit=dict(info))
                a = np.full((s1 - s0, nj, ni), np.nan, dtype=np.float32)
                fill(a, s0, s1)
                return out.cut(a)

            for p0, p1 in period_groups(starts, ends, cap):
                s0, s1 = int(starts[p0]), int(ends[p1 - 1])
                tsl = (slice(u.k0 + p0, u.k0 + p1),)
                if s1 - s0 <= cap:
                    cut = slab(s0, s1)
                    info["phase"] = "writing"
                    for stat, name in zip(vp.stats, vp.out_names):
                        g[name][(*tsl, *lpos, *space)] = self._encode(
                            reduce_periods(cut, starts[p0:p1] - s0, stat), keepbits)
                    del cut
                else:  # one period longer than a slab
                    acc = Running(vp.stats)
                    for c0 in range(s0, s1, cap):
                        acc.add(slab(c0, min(s1, c0 + cap)))
                        info["steps_done"] = min(s1, c0 + cap)
                    info["phase"] = "writing"
                    for stat, name in zip(vp.stats, vp.out_names):
                        g[name][(*tsl, *lpos, *space)] = self._encode(acc.result(stat)[None], keepbits)
                info["steps_done"] = s1
                self.activity(unit=dict(info))
        info["phase"] = "committing"
        self.activity(force=True, unit=dict(info))
        session.commit(f"unit {u.index}: {out.labels[u.k0]} .. {out.labels[u.k1 - 1]}", metadata={"unit": u.index})

    # ---------------------------------------------------------------- NetCDF jobs
    def _in_period(self, times: pd.DatetimeIndex) -> np.ndarray:
        """Which file times belong to the job's period: the days, months or years the request asked for (files
        stamp monthly means mid-month, daily statistics at noon ...); every time when nothing was selected."""
        inst, kind = self.cds_plan.instants, self.desc["time"]["kind"]
        if inst is None or not len(times):
            return np.ones(len(times), dtype=bool)
        if kind == "ymd":
            return np.isin(times.normalize(), inst.normalize())
        if kind == "ym":
            return np.isin(times.to_period("M"), inst.to_period("M").unique())
        return np.isin(times.year, inst.year.unique())

    def _scan_nc(self):
        files = []
        got = [e for e in self.ledger.entries if e.get("file")]
        for i, e in enumerate(got):
            self.activity(stage="converting", scan={"what": "unpacking results", "done": i, "total": len(got)})
            files += netcdf.unpack(Path(e["file"]), self.spool_dir() / "unpacked" / f"{e['index']:05d}")
        if not files:
            raise netcdf.NotGridded("the results hold no NetCDF file")
        sc = netcdf.scan(files, progress=lambda d, t: self.activity(
            stage="converting", scan={"what": "reading the files' metadata", "done": d, "total": t}))
        self.activity(force=True, scan=None)
        sc.times = sc.times[self._in_period(sc.times)]
        if not len(sc.times):
            raise netcdf.NotGridded("no time of the files is in the selected period")
        return sc

    def _convert_nc(self) -> str:
        """Every file is there: read them, create the store (first run), commit the units not done yet."""
        spec, led = self.spec, self.ledger
        self.save_state(status="running", message="converting: reading the NetCDF files")
        if self.output is None:
            try:
                sc = self._scan_nc()
                out, variables = CdsOutput.from_scan(self.cds_plan, sc)
            except (netcdf.NotGridded, PlanError) as err:
                return self._keep_files(str(err))
            out.save(self.dir, variables)
            out.remember(variables)
            for w in out.warnings:
                if w not in self.cds_plan.warnings:
                    self.log.warning(w)
            self.log.info("output variables: " + ", ".join(f"{v['name']} ({v.get('form_name') or v['long_name']})"
                                                           for v in variables))
            self.log.info(f"creating Icechunk repository {spec.icechunk_path}")
            self.repo = store.create_repo(spec.icechunk_path, out, self.job_id)
            self.output = self.plan = out
        else:  # resumed: the same files, read again (only their metadata)
            try:
                sc = self._scan_nc()
            except netcdf.NotGridded as err:
                raise Fatal(f"the downloaded files changed since the store was created: {err}") from None
        out = self.output
        keep = set(out.instants)
        index = {var: {t: (f, p) for f in fl for p, t in enumerate(f.times) if t in keep}
                 for var, fl in sc.fields.items()}
        self.units = out.units
        done = set(self.state["units_done"])
        self.save_state(units_total=len(self.units))
        for u in self.units:
            if u.index in done:
                continue
            if self.stop_event.is_set():
                status = {"cancel": "cancelled"}.get(self.stop_reason, "paused")
                self.log.info(f"stopped ({status})")
                self.save_state(status=status, pid=None, message=f"stopped: {self.stop_reason}")
                return status
            t0 = time.time()
            self._process_unit_nc(u, index)
            self.activity(force=True, unit=None)
            with self._state_lock:
                self.state["units_done"].append(u.index)
            n = len(set(self.state["units_done"]))
            self.save_state(units_processed=n, message=f"{n}/{len(self.units)} units committed")
            self.log.info(f"unit {u.index + 1}/{len(self.units)} committed ({out.labels[u.k0]} .. "
                          f"{out.labels[u.k1 - 1]}) in {time.time() - t0:.1f}s")
        for e in led.entries:
            if e.get("file"):
                Path(e["file"]).unlink(missing_ok=True)
                led.set(e, state="done", file=None)
        self._finish()
        return "completed"

    def _keep_files(self, why: str) -> str:
        """The files are not a grid one store can hold: keep them as the CDS delivered them."""
        d = self.files_dir()
        d.mkdir(parents=True, exist_ok=True)
        self.log.warning(f"the files are kept as delivered, not converted: {why}")
        self.save_state(kept_files=why)  # a resume from here on finishes as a files-only job
        for e in self.ledger.entries:
            if e.get("file") and Path(e["file"]).exists():
                dest = d / self._file_name(e)
                shutil.move(e["file"], dest)
                self.ledger.set(e, file=str(dest))
        self._done_files(why)
        return "completed"

    # ---------------------------------------------------------------- files-only jobs
    def _done_files(self, why: str | None = None):
        d = self.files_dir()
        manifest = [{"file": Path(e["file"]).name, "request": e["inputs"], "start": e["start"], "end": e["end"],
                     "size": e.get("size")} for e in self.ledger.entries if e.get("file")]
        write_json_atomic(d / "manifest.json", {"dataset": self.spec.dataset, "job": self.job_id,
                                                "files": manifest, "source": CdsOutput.provider,
                                                **({"not_converted": why} if why else {})})
        self.spec.remove_spool(self.job_id, self.dir)
        why = why or self.state.get("kept_files")
        self.save_state(status="completed", pid=None, finished_at=now_iso(),
                        message=f"done: kept as files ({why})" if why else "done", outputs=[str(d)],
                        output_bytes=dir_bytes(d), eta_s=0)
        self.log.info(f"completed: {len(manifest)} files in {d}")
