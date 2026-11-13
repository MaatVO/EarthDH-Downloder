"""Download engine: runs one job (in the foreground or as a detached worker process).

Pipeline (same design as the original ``edh_daily.py``, generalised):

* async downloaders (N connections, adjustable while running) fetch every needed source chunk exactly
  once into a spool directory, a few units ahead of processing (bounded by spool size and free disk);
* a processor thread waits until a unit's chunks are all present, decodes them, cuts the selection,
  aggregates time, writes the unit into the Icechunk repository and commits; then deletes spool chunks
  no later unit needs;
* progress, request usage and completed units are persisted, so a job can be paused, killed or crash and
  resume where it stopped. When every unit is committed the repository is exported to a plain Zarr.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import zarr

from . import catalog, keys, quota, sources, store
from .aggregate import bitround, reduce_periods
from .http import EDHClient, Fatal, QuotaReached, RequestCounter
from .job import JobSpec
from .planner import Plan, Unit, VarPlan
from .util import human_bytes, read_json, write_json_atomic

MAX_CONNECTIONS = 64


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Limiter:
    """A semaphore whose capacity can change while tasks are waiting."""

    def __init__(self, n: int):
        self.capacity = max(1, min(MAX_CONNECTIONS, int(n)))
        self.active = 0
        self.cond = asyncio.Condition()

    async def set(self, n: int):
        async with self.cond:
            self.capacity = max(1, min(MAX_CONNECTIONS, n))
            self.cond.notify_all()

    async def __aenter__(self):
        async with self.cond:
            await self.cond.wait_for(lambda: self.active < self.capacity)
            self.active += 1

    async def __aexit__(self, *exc):
        async with self.cond:
            self.active -= 1
            self.cond.notify_all()


def dir_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class Engine:
    def __init__(self, job_dir: Path, log: logging.Logger | None = None):
        self.dir = Path(job_dir)
        meta = read_json(self.dir / "job.json", None, dict)
        if meta is None or not isinstance(meta.get("spec"), dict):
            raise FileNotFoundError(self.dir / "job.json")
        self.job_id = meta.get("id", self.dir.name)
        self.spec = JobSpec.from_dict(meta["spec"])
        o = self.spec.output
        if o.target_mode == "extend":
            o.keep_icechunk = True  # never delete the repository being extended
            o.export_zarr = o.export_zarr or self.spec.zarr_path.exists()  # keep its companion Zarr current
        self.log = log or logging.getLogger("edh")
        self.state_path = self.dir / "state.json"
        self.state = read_json(self.state_path, {}, dict)
        self.state.setdefault("units_done", [])
        self.state.setdefault("requests_job", 0)
        self.state.setdefault("bytes_job", 0)
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.errors: list[str] = []
        self._state_lock = threading.Lock()

    # ---------------------------------------------------------------- state
    def save_state(self, **kw):
        with self._state_lock:
            self.state.update(kw)
            self.state["updated_at"] = now_iso()
            write_json_atomic(self.state_path, self.state)

    def activity(self, force: bool = False, **kw):
        """What the job is doing right now (``state["activity"]``: stage, downloads, the unit being processed,
        the export), for the job card. Parts set to None are dropped. Written at most once a second unless
        ``force``: progress callbacks may call it per chunk."""
        if not hasattr(self, "_act_lock"):
            self._act_lock, self._act, self._act_t = threading.Lock(), {}, 0.0
        with self._act_lock:
            for k, v in kw.items():
                if v is None:
                    self._act.pop(k, None)
                else:
                    self._act[k] = v
            now = time.time()
            if not force and now - self._act_t < 1.0:
                return
            self._act_t = now
            snap = json.loads(json.dumps(self._act, default=str))
        self.save_state(activity=snap)

    def control(self) -> dict:
        return read_json(self.dir / "control.json", {}, dict)

    def request_stop(self, reason: str):
        if not self.stop_event.is_set():
            self.stop_reason = reason
            self.stop_event.set()

    # ---------------------------------------------------------------- run
    def run(self) -> str:
        spec = self.spec
        self.save_state(status="starting", pid=os.getpid(), started_at=now_iso(), error=None, message="", activity={"stage": "planning"})
        signal.signal(signal.SIGTERM, lambda *_: self.request_stop(self.control().get("action", "pause")))
        signal.signal(signal.SIGINT, lambda *_: self.request_stop("pause"))
        try:
            if self.state.get("exported") and spec.zarr_path.exists() and spec.output.target_mode != "extend":
                # crashed after the export (e.g. while removing the repository): nothing left to download
                if not spec.output.keep_icechunk:
                    self._remove_icechunk()
                self._done()
                return "completed"
            src = sources.require_jobs(spec.source)
            key = keys.get_key(spec.download.key, src.id)
            limit = int(key.quota * spec.download.quota_safety)
            self.counter = RequestCounter(key.fingerprint, limit=limit)
            self.key = key
            info = catalog.dataset_info(spec)
            url = info["url"]
            ds = catalog.load_dataset(url, key.secret, counter=self.counter, info=info)
            self.plan = plan = Plan(spec, ds)
            for w in plan.warnings:
                self.log.warning(w)
            s = plan.summary()
            self.log.info(f"plan: {s['requests']} requests, ~{human_bytes(s['download_bytes_est'])} to download, "
                          f"{len(plan.units)} units, outputs {s['outputs']}, space {s['space']}")
            self.save_state(units_total=len(plan.units), plan=s, requests_total=plan.n_requests,
                            connections=self.control().get("connections", spec.download.connections))
            self._open_repo()
            done = set(self.state["units_done"])
            if len(done) < len(plan.units):
                self.save_state(status="running")
                asyncio.run(self._pipeline())
            if self.errors:
                raise Fatal("; ".join(self.errors[:3]))
            if self.stop_event.is_set() and len(set(self.state["units_done"])) < len(plan.units):
                status = {"cancel": "cancelled", "quota": "paused_quota"}.get(self.stop_reason, "paused")
                self.log.info(f"stopped ({status}); {len(self.state['units_done'])}/{len(plan.units)} units done")
                self.save_state(status=status, pid=None, message=f"stopped: {self.stop_reason}")
                return status
            self._finish()
            return "completed"
        except Exception as e:
            self.log.exception(f"job failed: {e}")
            self.save_state(status="failed", pid=None, error=f"{type(e).__name__}: {e}")
            return "failed"
        finally:
            if hasattr(self, "counter"):
                self.counter.flush()

    def _open_repo(self):
        path = self.spec.icechunk_path
        if self.spec.output.target_mode == "extend":
            repo = store.open_repo(path)
            if repo is None:
                raise Fatal(f"cannot extend: {path} is not an Icechunk repository")
            direction = self.spec.output.extend_direction or "forward"
            self.log.info(f"extending {direction} into Icechunk repository {path}")
            done = store.extension_done(repo, self.job_id)  # committed with the resize: never extend twice
            if done is None:
                problem = store.check_extension(repo, self.plan, direction)
                if problem:
                    raise Fatal(f"cannot extend {path.name}: {problem}")
                done = store.extend_repo(repo, self.plan, direction, self.job_id)
            self.time_offset = done["offset"]
            self.save_state(time_offset=self.time_offset, extend_direction=direction)
            self.repo = repo
            return

        repo = store.open_repo(path)
        if repo is None:
            if path.exists() and any(path.iterdir()):
                raise Fatal(f"{path} exists and is not an Icechunk repository; choose another output name")
            self.log.info(f"creating Icechunk repository {path}")
            repo = store.create_repo(path, self.plan, self.job_id)
            self.save_state(units_done=[])
        else:
            g = zarr.open_group(repo.readonly_session("main").store, mode="r")
            if g.attrs.get("edh_job_id") != self.job_id:
                raise Fatal(f"{path} belongs to another job ({g.attrs.get('edh_job_id')}); choose another output name")
            self.log.info(f"resuming into {path}")
        self.time_offset = 0
        self.repo = repo

    def _finish(self):
        plan, spec = self.plan, self.spec
        extra = {"edh_requests": self.state["requests_job"], "edh_bytes_downloaded": self.state["bytes_job"],
                 "edh_completed": now_iso()}
        if spec.output.target_mode == "extend":
            store.update_extended_attrs(self.repo, plan, self.job_id, extra)
        else:
            store.set_attrs(self.repo, store.root_attrs(plan, self.job_id, "complete", extra), "job complete")
        if spec.output.export_zarr:
            self.save_state(status="exporting", message=f"exporting to {spec.zarr_path}")
            self.activity(force=True, stage="exporting", unit=None, downloads=None,
                          export={"done": 0, "total": None, "path": str(spec.zarr_path), "since": now_iso()})
            self.log.info(f"exporting to {spec.zarr_path}")
            n = store.export_zarr(self.repo, spec.zarr_path, consolidated=spec.output.consolidated, plan=plan,
                                  memory_gb=spec.download.memory_gb,
                                  progress=lambda d, t: self.activity(
                                      export={"done": d, "total": t, "path": str(spec.zarr_path),
                                              "since": (self._act.get("export") or {}).get("since")}))
            self.log.info(f"exported {n} objects ({human_bytes(dir_bytes(spec.zarr_path))}); "
                          f"verified identical to the Icechunk repository")
            self.save_state(exported=True)  # from here on, a crash only has to finish the cleanup
            if not spec.output.keep_icechunk:
                self._remove_icechunk()
        self._done()

    def _remove_icechunk(self):
        if self.spec.icechunk_path.exists():
            shutil.rmtree(self.spec.icechunk_path)
        self.log.info("removed the Icechunk repository (keep_icechunk is off)")

    def _done(self):
        spec = self.spec
        spec.remove_spool(self.job_id, self.dir)
        if hasattr(self, "_act_lock"):
            with self._act_lock:
                self._act = {"stage": "done"}
        outputs = [str(p) for p in (spec.icechunk_path, spec.zarr_path) if p.exists()]
        self.save_state(status="completed", pid=None, finished_at=now_iso(), message="done", outputs=outputs,
                        output_bytes=sum(dir_bytes(Path(p)) for p in outputs), eta_s=0, activity={"stage": "done"})
        self.log.info(f"completed: {outputs}")

    # ---------------------------------------------------------------- spool
    def spool_dir(self) -> Path:
        return self.spec.spool_path(self.job_id, self.dir)

    def chunk_path(self, c) -> Path:
        var, _, t, li, ty, tx = c
        parts = [var] + ([str(li)] if li is not None else [])
        name = f"{'s' if t is None else t}.{ty}.{tx}"
        return self.spool_dir().joinpath(*parts, name)

    def chunk_present(self, c) -> bool:
        p = self.chunk_path(c)
        return p.exists() or p.with_suffix(p.suffix + ".miss").exists()

    def load_chunk(self, c, vp: VarPlan) -> np.ndarray:
        p = self.chunk_path(c)
        dec = vp.var.decoder()
        if p.exists():
            try:
                a = dec.decode(p.read_bytes())
            except Exception as e:  # noqa: BLE001 - truncated/garbage download: drop it so a resume fetches it again
                p.unlink(missing_ok=True)
                raise RuntimeError(f"downloaded chunk {p.name} of {c[0]} is corrupt ({e}); it was discarded, "
                                   f"resume the job to download it again") from e
        else:
            a = dec.empty()
        if vp.var.kind == "level":
            a = a.reshape(a.shape[0], a.shape[2], a.shape[3])
        return a

    def _adopt_sampled(self, todo) -> int:
        """Move chunks the sample already downloaded (recently) into the spool: no request is spent twice."""
        from .sample import RAW_TTL, raw_path

        plan, n, now = self.plan, 0, time.time()
        for u in todo:
            for c in plan.unit_needs[u.index]:
                if self.chunk_present(c):
                    continue
                vp = plan.vars[c[1]]
                src = raw_path(vp.chunk_url(plan.ds.url, c[2], c[3], c[4], c[5]))
                for q, miss in ((src, False), (src.with_suffix(".miss"), True)):
                    try:
                        if not q.exists() or now - q.stat().st_mtime > RAW_TTL:
                            continue
                        p = self.chunk_path(c)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(q), str(p.with_suffix(p.suffix + ".miss") if miss else p))
                        n += 1
                        break
                    except OSError:
                        continue
        return n

    # ---------------------------------------------------------------- pipeline
    async def _pipeline(self):
        plan, spec = self.plan, self.spec
        loop = asyncio.get_running_loop()
        spool = self.spool_dir()
        spool.mkdir(parents=True, exist_ok=True)
        done = set(self.state["units_done"])
        todo = [u for u in plan.units if u.index not in done]
        lock = threading.Lock()
        need: dict[tuple, list[int]] = {}
        remaining = {}
        adopted = self._adopt_sampled(todo)
        if adopted:
            self.log.info(f"reusing {adopted} chunks downloaded by the sample")
        for u in todo:
            missing = 0
            for c in plan.unit_needs[u.index]:
                if not self.chunk_present(c):
                    need.setdefault(c, []).append(u.index)
                    missing += 1
            remaining[u.index] = missing
        ready = {u.index: threading.Event() for u in todo}
        for u in todo:
            if remaining[u.index] == 0:
                ready[u.index].set()
        n_needed = len(need)
        self.log.info(f"{len(todo)} units to do, {n_needed} chunks to download "
                      f"({plan.n_requests - n_needed} already downloaded)")
        processed = {"n": len(done), "current": None}
        stop_async = asyncio.Event()
        quota_ok = asyncio.Event()
        quota_ok.set()
        conn = int(self.control().get("connections") or spec.download.connections)
        limiter = Limiter(conn)
        work_q: asyncio.Queue = asyncio.Queue()
        stats = {"bytes": 0, "req": 0}
        history = deque(maxlen=60)  # (time, bytes, requests) samples for rates

        # ---------- processor thread
        def processor():
            pool = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2))
            try:
                for u in todo:
                    while not ready[u.index].wait(1):
                        if self.stop_event.is_set():
                            return
                    if self.stop_event.is_set():
                        return
                    processed["current"] = u.index
                    t0 = time.time()
                    self._process_unit(u, pool)
                    for c in plan.unit_needs[u.index]:
                        if plan.chunk_last_unit[c] <= u.index:
                            p = self.chunk_path(c)
                            for q in (p, p.with_suffix(p.suffix + ".miss")):
                                if q.exists():
                                    q.unlink()
                    with self._state_lock:
                        self.state["units_done"].append(u.index)
                    processed["n"] += 1
                    self.save_state()
                    self.log.info(f"unit {u.index + 1}/{len(plan.units)} committed ({self._unit_label(u)}) "
                                  f"in {time.time() - t0:.1f}s")
            except Exception as e:
                self.log.exception(f"processing failed: {e}")
                self.errors.append(f"processing: {type(e).__name__}: {e}")
                self.request_stop("error")
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
                loop.call_soon_threadsafe(stop_async.set)

        proc = threading.Thread(target=processor, name="processor", daemon=True)
        proc.start()

        def chunk_done(c):
            with lock:
                for ui in need.get(c, []):
                    remaining[ui] -= 1
                    if remaining[ui] == 0:
                        ready[ui].set()

        # ---------- workers
        async def worker(client: EDHClient):
            while True:
                c = await work_q.get()
                if c is None:
                    return
                if self.stop_event.is_set():
                    continue
                await quota_ok.wait()
                vp = plan.vars[c[1]]
                url = vp.chunk_url(plan.ds.url, c[2], c[3], c[4], c[5])
                try:
                    async with limiter:
                        data = await client.get(url)
                except QuotaReached:
                    quota_ok.clear()
                    await work_q.put(c)
                    continue
                except Fatal as e:
                    self.errors.append(str(e))
                    self.log.error(str(e))
                    self.request_stop("error")
                    continue
                except Exception as e:  # noqa: BLE001 - a worker must never die silently (the job would wait forever)
                    self.errors.append(f"download: {type(e).__name__}: {e}")
                    self.log.exception(f"download failed: {e}")
                    self.request_stop("error")
                    continue
                p = self.chunk_path(c)
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if data is None:
                        p.with_suffix(p.suffix + ".miss").touch()
                    else:
                        tmp = p.with_suffix(".part")
                        tmp.write_bytes(data)
                        os.replace(tmp, p)
                        stats["bytes"] += len(data)
                except OSError as e:
                    self.errors.append(f"cannot write {p}: {e}")
                    self.log.error(f"cannot write {p}: {e}")
                    self.request_stop("error")
                    continue
                stats["req"] += 1
                chunk_done(c)

        # ---------- producer with backpressure
        async def producer():
            max_ahead = 2
            free_path = spool if spool.exists() else spool.parent
            for u in todo:
                while not self.stop_event.is_set():
                    ahead = u.index - (processed["current"] if processed["current"] is not None else todo[0].index)
                    spool_b = self._spool_bytes
                    free_gb = shutil.disk_usage(free_path).free / 1e9
                    out_free = shutil.disk_usage(spec.output_dir).free / 1e9
                    if ahead <= max_ahead and spool_b < spec.download.max_spool_gb * 1e9 \
                            and free_gb > spec.download.min_free_gb and out_free > spec.download.min_free_gb:
                        break
                    if (free_gb <= spec.download.min_free_gb or out_free <= spec.download.min_free_gb) and ahead == 0:
                        self.errors.append(f"less than {spec.download.min_free_gb} GB free on disk")
                        self.request_stop("error")
                        break
                    await asyncio.sleep(1)
                if self.stop_event.is_set():
                    break
                for c in plan.unit_needs[u.index]:
                    if c in need and need[c][0] == u.index:  # enqueue at the first unit needing it
                        await work_q.put(c)

        # ---------- monitor: state, rates, control, quota
        self._spool_bytes = dir_bytes(spool)

        async def monitor():
            nonlocal conn
            last_spool = 0.0
            while not stop_async.is_set():
                await asyncio.sleep(2)
                ctl = self.control()
                want = int(ctl.get("connections") or conn)
                if want != conn:
                    conn = want
                    await limiter.set(conn)
                    self.log.info(f"connections set to {conn}")
                if self.stop_event.is_set():
                    break
                if not quota_ok.is_set():
                    await handle_quota()
                    continue
                t = time.time()
                history.append((t, stats["bytes"], stats["req"]))
                if t - last_spool > 15:
                    self._spool_bytes = dir_bytes(spool)
                    last_spool = t
                self._update_progress(history, stats, n_needed, conn, processed)

        async def handle_quota():
            reset = quota.next_reset()
            msg = (f"local quota limit reached ({self.counter.month_used}/{self.counter.limit} requests this "
                   f"month)")
            if not spec.download.wait_for_quota:
                self.log.warning(msg + "; pausing (resume after the monthly reset)")
                self.request_stop("quota")
                return
            self.log.warning(msg + f"; waiting for the reset at {reset:%Y-%m-%d %H:%M} UTC")
            self.save_state(status="waiting_quota", message=f"waiting for quota reset {reset:%Y-%m-%d %H:%M} UTC")
            while datetime.now(timezone.utc) < reset + _grace() and not self.stop_event.is_set():
                await asyncio.sleep(5)
            if self.stop_event.is_set():
                return
            self.counter.flush()
            self.counter.month_used = quota.used(self.counter.fp)
            self.save_state(status="running", message="quota reset; continuing")
            self.log.info("monthly quota reset; continuing")
            quota_ok.set()

        async def watch_stop():
            while not self.stop_event.is_set() and not stop_async.is_set():
                await asyncio.sleep(0.5)
            stop_async.set()

        client = EDHClient(self.key.secret, counter=self.counter, connections=MAX_CONNECTIONS)
        client.on_retry = self.log.warning
        async with client:
            workers = [asyncio.create_task(worker(client)) for _ in range(MAX_CONNECTIONS)]
            mon = asyncio.create_task(monitor())
            ws = asyncio.create_task(watch_stop())
            prod = asyncio.create_task(producer())
            await stop_async.wait()  # processor finished (all done, stop or error)
            prod.cancel()
            while not work_q.empty():
                work_q.get_nowait()
            for _ in workers:
                work_q.put_nowait(None)
            quota_ok.set()
            await asyncio.wait(workers, timeout=30)
            for w in workers:
                w.cancel()
            mon.cancel()
            ws.cancel()
        await asyncio.get_running_loop().run_in_executor(None, proc.join)
        self.state["requests_job"] += stats["req"]
        self.state["bytes_job"] += stats["bytes"]
        self.save_state()

    def _update_progress(self, history, stats, n_needed, conn, processed):
        rate = req_rate = None
        if len(history) >= 2:
            (t0, b0, r0), (t1, b1, r1) = history[0], history[-1]
            if t1 > t0:
                rate, req_rate = (b1 - b0) / (t1 - t0), (r1 - r0) / (t1 - t0)
        left = n_needed - stats["req"]
        eta = left / req_rate if req_rate else None
        self.save_state(
            status="running", connections=conn, rate_bps=rate, req_rate=req_rate, eta_s=eta,
            requests_run=stats["req"], requests_left=left, bytes_run=stats["bytes"],
            spool_bytes=self._spool_bytes, month_used=self.counter.month_used, month_limit=self.counter.limit,
            units_processed=processed["n"],
            message=f"{processed['n']}/{len(self.plan.units)} units committed",
        )

    def _unit_label(self, u: Unit) -> str:
        if u.static or not self.plan.n_periods:
            return "static fields"
        a, b = self.plan.labels[u.k0], self.plan.labels[u.k1 - 1]
        return f"{a} .. {b}"

    # ---------------------------------------------------------------- processing
    def _process_unit(self, u: Unit, pool: ThreadPoolExecutor):
        plan = self.plan
        keepbits = self.spec.output.keepbits
        session = self.repo.writable_session("main")
        g = zarr.open_group(session.store, mode="r+")
        starts = (plan.p_start[u.k0:u.k1] - u.s) if not u.static else None
        for vp in plan.vars:
            static = vp.var.kind == "static"
            if static and u.index != 0:
                continue
            if u.static and not static:
                continue
            for li in (vp.level_idx or [None]):
                lpos = () if li is None else (plan.level_pos[li],)
                offset = getattr(self, "time_offset", 0)
                tsl = () if static else (slice(offset + u.k0, offset + u.k1),)
                if plan.points is not None:
                    arr = self._assemble_points(u, vp, li)
                    for stat, name in zip(vp.stats, vp.out_names):
                        out = arr if static else reduce_periods(arr, starts, stat)
                        g[name][(*tsl, *lpos, slice(None))] = self._encode(out, keepbits)
                    continue
                futures = deque()
                window = (pool._max_workers or 2) + 1
                for grp in vp.groups:
                    futures.append(pool.submit(self._compute_group, u, vp, li, grp, starts))
                    if len(futures) >= window:
                        self._write_group(g, vp, futures.popleft().result(), tsl, lpos, keepbits)
                while futures:
                    self._write_group(g, vp, futures.popleft().result(), tsl, lpos, keepbits)
        session.commit(f"unit {u.index}: {self._unit_label(u)}", metadata={"unit": u.index})

    def _time_chunks(self, u: Unit, vp: VarPlan):
        for t in range(u.s // vp.tc, (u.e - 1) // vp.tc + 1):
            t0 = t * vp.tc
            a0, a1 = max(u.s, t0), min(u.e, t0 + vp.tc)
            yield t, a0 - t0, a1 - t0, a0 - u.s, a1 - u.s

    def _assemble_points(self, u: Unit, vp: VarPlan, li) -> np.ndarray:
        plan = self.plan
        n = len(plan.points)
        static = vp.var.kind == "static"
        arr = np.full((n,) if static else (u.span, n), np.nan, dtype=np.float32)
        ti, tj = plan.point_i // vp.cy, plan.point_j // vp.cx
        n_idx = plan.vars.index(vp)
        for ty, tx in vp.tiles:
            sel = np.where((ti == ty) & (tj == tx))[0]
            rl, cl = plan.point_i[sel] - ty * vp.cy, plan.point_j[sel] - tx * vp.cx
            if static:
                arr[sel] = self.load_chunk((vp.var.name, n_idx, None, None, ty, tx), vp)[rl, cl]
                continue
            for t, c0, c1, o0, o1 in self._time_chunks(u, vp):
                ch = self.load_chunk((vp.var.name, n_idx, t, li, ty, tx), vp)
                arr[o0:o1, sel] = ch[c0:c1][:, rl, cl]
        return arr

    def _compute_group(self, u: Unit, vp: VarPlan, li, grp, starts):
        plan = self.plan
        static = vp.var.kind == "static"
        rows_t = sorted({ty for ty, _ in grp})
        cols_t = sorted({tx for _, tx in grp})
        rpos = np.where(np.isin(plan.lat_idx // vp.cy, rows_t))[0]
        cpos = np.where(np.isin(plan.lon_idx // vp.cx, cols_t))[0]
        shape = (len(rpos), len(cpos)) if static else (u.span, len(rpos), len(cpos))
        arr = np.full(shape, np.nan, dtype=np.float32)
        src_r, src_c = plan.lat_idx[rpos], plan.lon_idx[cpos]
        n_idx = plan.vars.index(vp)
        for ty, tx in grp:
            rsub = np.where(src_r // vp.cy == ty)[0]
            csub = np.where(src_c // vp.cx == tx)[0]
            if not len(rsub) or not len(csub):
                continue
            rl, cl = src_r[rsub] - ty * vp.cy, src_c[csub] - tx * vp.cx
            if static:
                ch = self.load_chunk((vp.var.name, n_idx, None, None, ty, tx), vp)
                arr[np.ix_(rsub, csub)] = ch[np.ix_(rl, cl)]
                continue
            for t, c0, c1, o0, o1 in self._time_chunks(u, vp):
                ch = self.load_chunk((vp.var.name, n_idx, t, li, ty, tx), vp)
                arr[o0:o1, rsub[:, None], csub[None, :]] = ch[c0:c1][:, rl[:, None], cl[None, :]]
        outs = [arr if static else reduce_periods(arr, starts, s) for s in vp.stats]
        return rpos, cpos, outs

    def _encode(self, out, keepbits):
        if self.spec.output.dtype == "float64":
            return np.asarray(out, dtype=np.float64)
        return bitround(np.asarray(out, dtype=np.float32), keepbits)

    def _write_group(self, g, vp: VarPlan, result, tsl, lpos, keepbits):
        rpos, cpos, outs = result
        rs = slice(int(rpos[0]), int(rpos[-1]) + 1)
        # output columns can wrap (global with -180..180, or boxes across the seam): write contiguous runs
        breaks = np.where(np.diff(cpos) != 1)[0] + 1
        runs = np.split(np.arange(len(cpos)), breaks)
        for name, out in zip(vp.out_names, outs):
            out = self._encode(out, keepbits)
            for run in runs:
                cs = slice(int(cpos[run[0]]), int(cpos[run[-1]]) + 1)
                g[name][(*tsl, *lpos, rs, cs)] = out[..., run[0]:run[-1] + 1]


def _grace():
    from datetime import timedelta

    return timedelta(minutes=5)


def setup_logging(job_dir: Path, to_stdout: bool) -> logging.Logger:
    log = logging.getLogger("edh")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(Path(job_dir) / "log.txt")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_stdout:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log
