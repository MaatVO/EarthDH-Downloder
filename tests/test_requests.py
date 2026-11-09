"""Request economy: every request counts against the monthly quota, none should be spent twice."""

import asyncio

from edh_downloader import catalog, keys, sample
from edh_downloader.http import RequestCounter
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, TimeSpec
from edh_downloader.planner import Plan
from edh_downloader.runner import create_job, run_foreground


def _spec(fake, home, name):
    return JobSpec(dataset="fake", dataset_url=fake.url, variables=["t2m", "tp"],
                   time=TimeSpec("2019-11-10", "2019-12-20", "day"), area=AreaSpec("global"),
                   output=OutputSpec(directory=str(home / "out"), name=name),
                   download=DownloadSpec(key="test", connections=4, min_free_gb=0.1))


def _chunks(reqs):
    return [p for p in reqs if "/c/" in p]


def test_sampled_chunks_are_not_downloaded_again_by_the_job(home, fake_edh):
    s = _spec(fake_edh, home, "smp")
    key = keys.get_key("test")
    plan = Plan(s, catalog.load_dataset(fake_edh.url, key.secret))
    start = len(fake_edh.requests)
    asyncio.run(sample.run(plan, key.secret, RequestCounter(key.fingerprint), n=6))
    assert run_foreground(create_job(s)) == "completed"
    got = _chunks(fake_edh.requests[start:])
    dup = len(got) - len(set(got))
    assert dup == 0, f"{dup} chunks fetched by the sample were downloaded again by the job"
    assert len(set(got)) == plan.n_requests


def test_resume_of_completed_job_and_metadata_cost_nothing(home, fake_edh):
    s = _spec(fake_edh, home, "meta")
    job = create_job(s)
    assert run_foreground(job) == "completed"
    n = len(fake_edh.requests)
    assert run_foreground(job) == "completed"
    assert fake_edh.requests[n:] == []


def test_crash_while_removing_icechunk_after_export_does_not_redownload(home, fake_edh, monkeypatch):
    import shutil

    from edh_downloader import engine, runner

    s = _spec(fake_edh, home, "zonly")
    s.output.export_zarr, s.output.keep_icechunk = True, False
    job = create_job(s)

    class Crash(BaseException):
        pass

    real = shutil.rmtree

    def crash_after(path, *a, **k):
        real(path, *a, **k)
        if str(path).endswith(".icechunk"):
            raise Crash()  # power cut while the repository was being removed

    monkeypatch.setattr(engine.shutil, "rmtree", crash_after)
    try:
        run_foreground(job)
    except Crash:
        pass
    monkeypatch.setattr(engine.shutil, "rmtree", real)
    assert runner.status(job)["status"] == "interrupted" or runner.status(job)["status"] == "exporting"
    n = len(fake_edh.requests)
    assert run_foreground(job) == "completed"
    assert _chunks(fake_edh.requests[n:]) == [], "the finished job downloaded everything again"
    assert s.zarr_path.exists() and not s.icechunk_path.exists()


def test_only_the_needed_chunks_are_requested(home, fake_edh):
    import numpy as np
    import pandas as pd

    from conftest import CX, CY, TC
    from edh_downloader.job import Point

    key = keys.get_key("test")
    ds = catalog.load_dataset(fake_edh.url, key.secret)
    tt = pd.Timestamp("1940-01-01") + pd.to_timedelta(fake_edh.t, unit="h")
    for area in (AreaSpec("bbox", west=-30, east=40, south=-20, north=35),
                 AreaSpec("points", points=[Point("a", 10, 10), Point("b", 11, 11), Point("c", -60, 170)])):
        s = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m", "t"], levels=[500],
                    time=TimeSpec("2019-11-20", "2019-12-02", "day"), area=area,
                    output=OutputSpec(directory=str(home), name="n"), download=DownloadSpec(key="test"))
        p = Plan(s, ds)
        sel = np.where((tt >= "2019-11-20") & (tt < "2019-12-03"))[0]
        n_t = len(np.unique(sel // TC))
        if p.points is not None:
            tiles = len({(i // CY, j // CX) for i, j in zip(p.point_i, p.point_j)})
        else:
            tiles = len(np.unique(p.lat_idx // CY)) * len(np.unique(p.lon_idx // CX))
        assert p.n_requests == 2 * n_t * tiles, (area.mode, p.n_requests, n_t, tiles)
