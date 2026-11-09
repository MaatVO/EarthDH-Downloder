import time

import numpy as np
import xarray as xr

from edh_downloader import engine, runner
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, TimeSpec


def _spec(fake, home, name):
    return JobSpec(dataset="fake", dataset_url=fake.url, variables=["t2m", "tp"],
                   time=TimeSpec("2019-11-05", "2019-12-15", "none"), area=AreaSpec("global"),
                   output=OutputSpec(directory=str(home / "out"), name=name, keepbits=None),
                   download=DownloadSpec(key="test", connections=3, min_free_gb=0.1))


def test_pause_then_resume_fetches_each_chunk_once(home, fake_edh, monkeypatch):
    spec = _spec(fake_edh, home, "r")
    job = runner.create_job(spec)
    orig = engine.Engine._process_unit
    calls = {"n": 0}

    def once_then_stop(self, u, pool):
        orig(self, u, pool)
        calls["n"] += 1
        if calls["n"] == 2:
            self.request_stop("pause")

    monkeypatch.setattr(engine.Engine, "_process_unit", once_then_stop)
    start = len(fake_edh.requests)
    assert runner.run_foreground(job) == "paused"
    st = runner.status(job)
    assert st["status"] == "paused" and len(st["units_done"]) == 2 < st["units_total"]
    monkeypatch.setattr(engine.Engine, "_process_unit", orig)
    assert runner.run_foreground(job) == "completed"
    chunk_reqs = [p for p in fake_edh.requests[start:] if "/c/" in p and ("/t2m/" in p or "/tp/" in p)]
    assert len(chunk_reqs) == len(set(chunk_reqs)), "a chunk was downloaded twice"
    ds = xr.open_zarr(spec.zarr_path)
    assert not np.isnan(ds.t2m.values).any() and not np.isnan(ds.tp.values).any()


def test_cancelled_job_resumes_without_refetching(home, fake_edh, monkeypatch):
    """The cancel dialog promises this."""
    spec = _spec(fake_edh, home, "c")
    job = runner.create_job(spec)
    orig = engine.Engine._process_unit
    calls = {"n": 0}

    def once_then_cancel(self, u, pool):
        orig(self, u, pool)
        calls["n"] += 1
        if calls["n"] == 2:
            self.request_stop("cancel")

    monkeypatch.setattr(engine.Engine, "_process_unit", once_then_cancel)
    start = len(fake_edh.requests)
    assert runner.run_foreground(job) == "cancelled"
    assert len(runner.status(job)["units_done"]) == 2
    monkeypatch.setattr(engine.Engine, "_process_unit", orig)
    assert runner.run_foreground(job) == "completed"
    chunk_reqs = [p for p in fake_edh.requests[start:] if "/c/" in p and ("/t2m/" in p or "/tp/" in p)]
    assert len(chunk_reqs) == len(set(chunk_reqs)), "a chunk was downloaded twice"
    assert runner.status(job)["output_bytes"] > 0


def test_detached_process(home, fake_edh):
    spec = _spec(fake_edh, home, "bg")
    job = runner.create_job(spec)
    pid = runner.launch(job)
    assert pid > 0
    for _ in range(120):
        st = runner.status(job)
        if st["status"] in ("completed", "failed"):
            break
        time.sleep(0.5)
    assert st["status"] == "completed", (job / "log.txt").read_text()
    assert spec.zarr_path.exists()
