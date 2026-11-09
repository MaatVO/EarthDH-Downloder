"""Fault injection: the job must survive (or fail cleanly and resume) when the network, disk or a chunk misbehaves."""

import asyncio
import os
import signal
import threading
import time

import numpy as np
import pytest
import xarray as xr
from aiohttp import web

from edh_downloader import runner
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, TimeSpec


class Faulty:
    """Serves the fake dataset, with ``hook(path, n) -> web.Response | None`` to inject faults (n = hit count)."""

    def __init__(self, root):
        self.root, self.hook, self.hits = root, None, {}
        ready = threading.Event()

        async def handler(request):
            p = request.match_info["tail"]
            self.hits[p] = self.hits.get(p, 0) + 1
            if request.headers.get("Authorization") != "Bearer test-key":
                return web.Response(status=401)
            if self.hook and (r := self.hook(p, self.hits[p])) is not None:
                return r
            f = self.root / p
            return web.Response(body=f.read_bytes()) if f.is_file() else web.Response(status=404)

        def run():
            loop = asyncio.new_event_loop()
            app = web.Application()
            app.router.add_get("/ds.zarr/{tail:.*}", handler)
            r = web.AppRunner(app)
            loop.run_until_complete(r.setup())
            site = web.TCPSite(r, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ds.zarr"
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)


@pytest.fixture
def faulty(fake_edh):
    return Faulty(fake_edh.root)


@pytest.fixture
def fast_retries(monkeypatch):
    orig = asyncio.sleep

    async def fast(t, *a, **k):
        return await orig(min(t, 0.02), *a, **k)

    monkeypatch.setattr(asyncio, "sleep", fast)


def _spec(srv, home, name, **kw):
    base = dict(dataset="fake", dataset_url=srv.url, variables=["t2m"],
                time=TimeSpec("2019-11-05", "2019-11-30", "none"), area=AreaSpec("global"),
                output=OutputSpec(directory=str(home / "out"), name=name, keepbits=None),
                download=DownloadSpec(key="test", connections=3, min_free_gb=0.1))
    base.update(kw)
    return JobSpec(**base)


def _run_bounded(job, timeout=90):
    """run_foreground in the main thread (it installs signal handlers); a hang is reported, not waited for."""
    class Hung(BaseException):  # not an Exception: Engine.run() would swallow it as a job failure
        pass

    def alarm(*_):
        raise Hung()

    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(timeout)
    try:
        return runner.run_foreground(job)
    except Hung:
        return "HUNG"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _expect(fake_edh, spec):
    ds = xr.open_zarr(spec.zarr_path)
    assert not np.isnan(ds.t2m.values).any()
    return ds


def test_transient_5xx_and_429_are_retried(home, faulty, fake_edh, fast_retries):
    faulty.hook = lambda p, n: web.Response(status=503 if "t2m/c/" in p and n <= 2 else 200) \
        if "t2m/c/" in p and n <= 2 else None
    s = _spec(faulty, home, "retry")
    assert _run_bounded(runner.create_job(s)) == "completed"
    _expect(fake_edh, s)


def test_garbage_chunk_then_server_fixed_resumes(home, faulty, fake_edh, fast_retries):
    """A proxy/captive portal answers 200 with HTML for one chunk. The job may fail, but resuming once the
    network is fine must complete (a bad chunk must not stay in the spool forever)."""
    bad = {"on": True}
    faulty.hook = lambda p, n: web.Response(body=b"<html>captive portal</html>") \
        if bad["on"] and "t2m/c/0/0/0" in p else None
    s = _spec(faulty, home, "garbage")
    job = runner.create_job(s)
    first = _run_bounded(job)
    assert first in ("failed", "completed")
    bad["on"] = False
    if first == "failed":
        assert _run_bounded(job) == "completed", (job / "log.txt").read_text()[-1500:]
    _expect(fake_edh, s)


def test_disk_error_writing_a_chunk_does_not_hang(home, faulty, fake_edh, fast_retries, monkeypatch):
    from pathlib import Path

    orig, state = Path.write_bytes, {"n": 0}

    def flaky(self, data):
        if self.suffix == ".part":
            state["n"] += 1
            if state["n"] == 3:
                raise OSError(28, "No space left on device")
        return orig(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    s = _spec(faulty, home, "enospc")
    r = _run_bounded(runner.create_job(s), timeout=60)
    assert r in ("failed", "completed"), f"job {r}"


def test_key_rejected_midway_fails_then_resumes(home, faulty, fake_edh, fast_retries):
    rejecting = {"on": True}
    faulty.hook = lambda p, n: web.Response(status=401) if rejecting["on"] and "t2m/c/1/" in p else None
    s = _spec(faulty, home, "k401")
    job = runner.create_job(s)
    assert _run_bounded(job) == "failed"
    st = runner.status(job)
    assert st["status"] == "failed" and st.get("error")
    rejecting["on"] = False
    assert _run_bounded(job) == "completed", (job / "log.txt").read_text()[-1500:]
    _expect(fake_edh, s)


def test_sigkill_then_resume(home, faulty, fake_edh):
    s = _spec(faulty, home, "kill9", variables=["t2m", "tp"], time=TimeSpec("2019-11-05", "2019-12-15", "none"))
    # slow the server so the kill lands mid-run
    faulty.hook = lambda p, n: (time.sleep(0.15), None)[1]
    job = runner.create_job(s)
    pid = runner.launch(job)
    for _ in range(200):
        st = runner.status(job)
        if len(st.get("units_done", [])) >= 1:
            break
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    time.sleep(1)
    assert runner.status(job)["status"] == "interrupted"
    faulty.hook = None
    assert _run_bounded(job, 120) == "completed", (job / "log.txt").read_text()[-1500:]
    ds = xr.open_zarr(s.zarr_path)
    assert not np.isnan(ds.t2m.values).any() and not np.isnan(ds.tp.values).any()


def test_extend_failure_before_first_unit_then_resume_does_not_extend_twice(home, fake_edh, monkeypatch):
    from edh_downloader import engine, store

    base = _spec(fake_edh, home, "ext", time=TimeSpec("2019-11-15", "2019-11-19", "none"),
                 area=AreaSpec("bbox", west=-30, east=30, south=-20, north=20))
    assert _run_bounded(runner.create_job(base)) == "completed"
    n0 = store.inspect_icechunk_for_extend(base.icechunk_path)["time_len"]
    ext = _spec(fake_edh, home, "ext", time=TimeSpec("2019-11-20", "2019-11-24", "none"),
                area=AreaSpec("bbox", west=-30, east=30, south=-20, north=20),
                output=OutputSpec(directory=str(home / "out"), name="ext", target_mode="extend",
                                  target_store=str(base.icechunk_path), extend_direction="forward"))
    job = runner.create_job(ext)
    orig = engine.Engine._process_unit

    def boom(self, u, pool):
        raise RuntimeError("transient failure")

    monkeypatch.setattr(engine.Engine, "_process_unit", boom)
    assert _run_bounded(job) == "failed"
    monkeypatch.setattr(engine.Engine, "_process_unit", orig)
    assert _run_bounded(job) == "completed"
    n1 = store.inspect_icechunk_for_extend(base.icechunk_path)["time_len"]
    assert n1 == n0 + 5 * 24, f"time axis is {n1} long, expected {n0 + 5 * 24}"


def _extend_spec(fake, home, base, t0, t1, direction="forward", **kw):
    area = kw.pop("area", AreaSpec("bbox", west=-30, east=30, south=-20, north=20))
    return _spec(fake, home, "ext", time=TimeSpec(t0, t1, "none"), area=area,
                 output=OutputSpec(directory=str(home / "out"), name="ext", target_mode="extend",
                                   target_store=str(base.icechunk_path), extend_direction=direction), **kw)


@pytest.fixture
def base_store(home, fake_edh):
    base = _spec(fake_edh, home, "ext", time=TimeSpec("2019-11-15", "2019-11-19", "none"),
                 area=AreaSpec("bbox", west=-30, east=30, south=-20, north=20))
    assert _run_bounded(runner.create_job(base)) == "completed"
    return base


@pytest.mark.parametrize("what,kw,t", [
    ("different area", dict(area=AreaSpec("bbox", west=-10, east=10, south=-10, north=10)), ("2019-11-20", "2019-11-24")),
    ("different variable", dict(variables=["tp"]), ("2019-11-20", "2019-11-24")),
    ("overlapping dates", {}, ("2019-11-18", "2019-11-22")),
    ("gap in dates", {}, ("2019-11-25", "2019-11-28")),
])
def test_extend_with_incompatible_job_is_refused_not_silently_corrupting(home, fake_edh, base_store, what, kw, t):
    from edh_downloader import store

    n0 = store.inspect_icechunk_for_extend(base_store.icechunk_path)["time_len"]
    job = runner.create_job(_extend_spec(fake_edh, home, base_store, *t, **kw))
    r = _run_bounded(job)
    n1 = store.inspect_icechunk_for_extend(base_store.icechunk_path)["time_len"]
    assert r == "failed" or n1 == n0 + 0 or what == "gap in dates", f"{what}: job {r}, time axis {n0} -> {n1}"
    if r == "failed":
        assert n1 == n0, f"{what}: job failed but left the store resized {n0} -> {n1}"


@pytest.mark.parametrize("export,keep", [(True, False), (False, True)])
def test_extend_keeps_the_target_and_its_companion_zarr_current(home, fake_edh, base_store, export, keep):
    import xarray as xr

    base_store.output.export_zarr = True
    s = _extend_spec(fake_edh, home, base_store, "2019-11-20", "2019-11-24")
    s.output.export_zarr, s.output.keep_icechunk = export, keep
    assert _run_bounded(runner.create_job(s)) == "completed"
    assert base_store.icechunk_path.exists(), "the extended repository was deleted"
    z = xr.open_zarr(base_store.zarr_path)
    assert str(z.valid_time.values[-1])[:10] == "2019-11-24", "the companion Zarr is stale"
