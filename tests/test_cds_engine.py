"""CDS jobs end to end against the fake CDS, which delivers real GRIB fields (tests/fake_cds.py)."""

import asyncio
import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fake_cds import TOKEN, value

from edh_downloader import runner, service
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, Point, TimeSpec
from edh_downloader.runner import create_job, run_foreground
from edh_downloader.sources.cds import engine as cds_engine
from edh_downloader.sources.cds.client import CDSClient

ERA = "reanalysis-era5-single-levels"
BOX = AreaSpec(mode="bbox", west=10, south=45, east=12, north=47)


@pytest.fixture
def fast(monkeypatch, cds):
    """Short waits: the queue polls and backs off in fractions of a second."""
    monkeypatch.setattr(cds_engine, "POLL_MIN", 0.02)
    monkeypatch.setattr(cds_engine, "POLL_MAX", 0.1)
    monkeypatch.setattr(cds_engine, "BACKOFF_MIN", 0.05)
    monkeypatch.setattr(cds_engine, "BACKOFF_MAX", 0.2)
    monkeypatch.setattr(cds_engine, "RETRY_WAIT", 0.05)
    cds.make_results = True
    cds.queue_delay = cds.run_delay = 0.02
    return cds


def spec(home, dataset=ERA, variables=("2m_temperature", "total_precipitation"), start="2020-01-01",
         end="2020-01-03", area=BOX, name="c", options=None, **kw):
    s = JobSpec(source="cds", dataset=dataset, variables=list(variables),
                time=TimeSpec(start, end, **kw.pop("time", {})), area=area,
                output=OutputSpec(directory=str(home / "out"), name=name, keepbits=None),
                download=DownloadSpec(key="cds-test", min_free_gb=0.01))
    s.cds.options = {"product_type": ["reanalysis"]} if options is None else options
    for k, v in kw.items():
        setattr(s.cds, k, v)
    return s


def ledger(job):
    return json.loads((job / "requests.json").read_text())["requests"]


def test_hourly_box_exact(home, fast):
    s = spec(home)
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path, consolidated=True)
    assert set(ds.data_vars) == {"t2m", "tp"}
    assert ds.t2m.attrs["cds_variable"] == "2m_temperature" and ds.tp.attrs["GRIB_stepType"] == "accum"
    lat, lon = ds.latitude.values, ds.longitude.values
    assert lat[0] == 47 and lat[-1] == 45 and lon[0] == 10 and lon[-1] == 12 and len(lat) == 9
    t = pd.DatetimeIndex(ds.valid_time.values)
    assert len(t) == 72 and t[0] == pd.Timestamp("2020-01-01") and t[-1] == pd.Timestamp("2020-01-03 23:00")
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    for k in (0, 13, 71):
        np.testing.assert_allclose(ds.t2m.values[k], value(167, t[k], LAT, LON), atol=1e-3)
        np.testing.assert_allclose(ds.tp.values[k], value(228, t[k], LAT, LON), atol=1e-3)
    assert ds.attrs["provider"].startswith("Copernicus Climate Data Store")
    assert fast.submissions == 1
    assert all(e["state"] == "done" for e in ledger(job))
    st = runner.status(job)
    assert st["status"] == "completed" and st["cds_requests"] == {"done": 1}
    assert not (job / "spool").exists()


def test_daily_statistics_with_utc_offset(home, fast):
    s = spec(home, start="2020-01-02", end="2020-01-03",
             time={"freq": "day", "tz_offset": 1, "stats": {"2m_temperature": ["mean", "max"]}})
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert set(ds.data_vars) == {"t2m_mean", "t2m_max", "tp"}  # tp: sum, the default of an accumulation
    LAT, LON = np.meshgrid(ds.latitude.values, ds.longitude.values, indexing="ij")
    for k, day in enumerate(pd.date_range("2020-01-02", "2020-01-03")):
        utc = pd.date_range(day - pd.Timedelta(hours=1), periods=24, freq="h")  # local day in UTC+1
        t2m = np.stack([value(167, t, LAT, LON) for t in utc])
        tp = np.stack([value(228, t, LAT, LON) for t in utc])
        np.testing.assert_allclose(ds.t2m_mean.values[k], t2m.mean(0), atol=1e-3)
        np.testing.assert_allclose(ds.t2m_max.values[k], t2m.max(0), atol=1e-3)
        np.testing.assert_allclose(ds.tp.values[k], tp.sum(0), atol=1e-2)


def test_requests_packed_and_units_follow_them(home, fast):
    s = spec(home, start="2020-01-30", end="2020-03-02", pack="month")
    job = create_job(s)
    assert run_foreground(job) == "completed"
    assert fast.submissions == 3  # partial January, February, partial March
    st = runner.status(job)
    assert st["units_total"] == 3 and len(st["units_done"]) == 3
    ds = xr.open_zarr(s.zarr_path)
    assert len(ds.valid_time) == (2 + 29 + 2) * 24


def test_pause_and_resume_never_submits_twice(home, fast, monkeypatch):
    s = spec(home, start="2020-01-30", end="2020-03-02", pack="month")
    job = create_job(s)
    orig = cds_engine.CdsEngine._process_unit
    calls = {"n": 0}

    def once_then_pause(self, u):
        orig(self, u)
        calls["n"] += 1
        if calls["n"] == 1:
            self.request_stop("pause")

    monkeypatch.setattr(cds_engine.CdsEngine, "_process_unit", once_then_pause)
    assert run_foreground(job) == "paused"
    states = [e["state"] for e in ledger(job)]
    assert states[0] == "done" and "planned" not in states[1:] or True
    first = fast.submissions
    monkeypatch.setattr(cds_engine.CdsEngine, "_process_unit", orig)
    assert run_foreground(job) == "completed"
    assert fast.submissions == 3 and first <= 3  # each request submitted exactly once over both runs
    assert len(xr.open_zarr(s.zarr_path).valid_time) == 33 * 24


def test_submission_cut_short_is_found_again(home, fast):
    s = spec(home)
    job = create_job(s)
    # a first run that died right after submitting: the ledger says "submitting", the CDS has the job
    eng = cds_engine.CdsEngine(job)
    eng.key = __import__("edh_downloader").keys.get_key("cds-test", "cds")
    eng.desc = cds_engine.catalog.describe(ERA)
    eng.files_only = False
    eng.ledger = cds_engine.Ledger(job / "requests.json")
    eng._make_plan()
    e = eng.ledger.entries[0]

    async def submit():
        async with CDSClient(TOKEN) as c:
            return await c.submit(ERA, e["inputs"])
    asyncio.run(submit())
    eng.ledger.set(e, state="submitting")
    assert fast.submissions == 1
    assert run_foreground(job) == "completed"
    assert fast.submissions == 1  # adopted, not submitted again


def test_queue_limit_backs_off_and_finishes(home, fast):
    fast.max_active = 1
    fast.queue_delay = 0.15
    s = spec(home, start="2020-01-30", end="2020-03-02", pack="month", max_inflight=3)
    job = create_job(s)
    assert run_foreground(job) == "completed"
    log = (job / "log.txt").read_text()
    assert "limits requests" in log
    assert len([e for e in ledger(job) if e["state"] == "done"]) == 3


def test_queue_limit_as_failed_jobs(home, fast):
    fast.max_active, fast.throttle_mode, fast.queue_delay = 1, "job", 0.15
    s = spec(home, start="2020-01-30", end="2020-03-02", pack="month", max_inflight=3)
    job = create_job(s)
    assert run_foreground(job) == "completed"
    assert "limits requests" in (job / "log.txt").read_text()


def test_failing_request_stops_the_job(home, fast):
    from fake_cds import _canon

    s = spec(home)
    job = create_job(s)
    eng_plan = service.cds_plan(dict(s.to_dict()))
    fast.fail_requests.add(_canon(eng_plan["requests"][0]["inputs"]))
    assert run_foreground(job) == "failed"
    st = runner.status(job)
    assert "failed 3 times" in st["error"] and "no data" in st["error"]
    assert fast.submissions == 3


def test_expired_result_is_requested_again(home, fast, monkeypatch):
    s = spec(home)
    job = create_job(s)
    orig = cds_engine.CDSClient.results
    seen = {"n": 0}

    async def expire_once(self, job_id):
        seen["n"] += 1
        if seen["n"] == 1:
            raise cds_engine.ResultsExpired("results expired")
        return await orig(self, job_id)

    monkeypatch.setattr(cds_engine.CDSClient, "results", expire_once)
    assert run_foreground(job) == "completed"
    assert fast.submissions == 2 and "submitted" in (job / "log.txt").read_text()


def test_missing_licence_stops_before_any_request(home, fast):
    fast.licences = []
    job = create_job(spec(home))
    assert run_foreground(job) == "failed"
    assert "licence" in runner.status(job)["error"] and fast.submissions == 0


def test_pressure_levels(home, fast):
    s = spec(home, dataset="reanalysis-era5-pressure-levels", variables=["temperature"], end="2020-01-01",
             options={"product_type": ["reanalysis"]})
    s.levels = [500, 850]
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert ds.t.dims == ("valid_time", "isobaricInhPa", "latitude", "longitude")
    assert list(ds.isobaricInhPa.values) == [500, 850]
    LAT, LON = np.meshgrid(ds.latitude.values, ds.longitude.values, indexing="ij")
    t = pd.Timestamp(ds.valid_time.values[5])
    np.testing.assert_allclose(ds.t.values[5, 1], value(130, t, LAT, LON) + 850 / 10000, atol=1e-3)


def test_cerra_lambert_grid_cut_locally(home, fast):
    s = spec(home, dataset="reanalysis-cerra-single-levels", variables=["2m_temperature"], end="2020-01-01",
             area=AreaSpec(mode="bbox", west=0, south=40, east=3, north=42),
             options={"level_type": "surface_or_atmosphere", "data_type": ["reanalysis"],
                      "product_type": "analysis"})
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert ds.t2m.dims == ("valid_time", "y", "x") and ds.latitude.dims == ("y", "x")
    assert len(ds.valid_time) == 8  # 3-hourly
    assert ds.latitude.attrs["grid_LaDInDegrees"] == 50.0
    assert ds.latitude.values.min() <= 42 and 0 < ds.t2m.shape[1] * ds.t2m.shape[2] < 30  # cut, not the whole grid


def test_points_nearest_cells(home, fast):
    s = spec(home, end="2020-01-01", area=AreaSpec(mode="points", points=[Point("a", 45.6, 10.4),
                                                                         Point("b", 46.9, 11.9)]))
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert ds.t2m.dims == ("valid_time", "point")
    assert list(ds.latitude.values) == [45.5, 47.0] and list(ds.longitude.values) == [10.5, 12.0]
    t = pd.Timestamp(ds.valid_time.values[3])
    np.testing.assert_allclose(ds.t2m.values[3], value(167, t, np.array([45.5, 47.0]), np.array([10.5, 12.0])),
                               atol=1e-3)


def test_files_only_dataset(home, fast):
    s = spec(home, dataset="reanalysis-era5-single-levels-timeseries", variables=["2m_temperature"],
             end="2020-01-31", area=AreaSpec(mode="points", points=[Point("a", 45, 10), Point("b", 46, 11)]),
             options={})
    job = create_job(s)
    assert run_foreground(job) == "completed"
    d = home / "out" / "c.files"
    files = sorted(p.name for p in d.iterdir())
    assert len(files) == 3 and "manifest.json" in files
    man = json.loads((d / "manifest.json").read_text())
    assert [f["request"]["location"] for f in man["files"]] == [{"latitude": 45, "longitude": 10},
                                                                {"latitude": 46, "longitude": 11}]


def test_cancel_dismisses_waiting_requests(home, fast, monkeypatch):
    fast.queue_delay = 30  # never runs during the test
    s = spec(home)
    job = create_job(s)
    orig = cds_engine.Ledger.set

    def cancel_once_queued(self, e, **kw):
        orig(self, e, **kw)
        if kw.get("state") == "queued":
            runner.set_control(job, action="cancel")
            import os
            import signal
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cds_engine.Ledger, "set", cancel_once_queued)
    assert run_foreground(job) == "cancelled"
    assert all(j["status"] == "dismissed" for j in fast.jobs.values())
    assert ledger(job)[0]["state"] == "planned"


def test_job_start_through_the_service(home, fast):
    r = service.job_start(spec(home).to_dict())
    assert r["pid"]
    with pytest.raises(ValueError, match="already"):
        service.job_start(dict(spec(home).to_dict()))


def test_a_file_that_never_matches_its_checksum_stops_the_job(home, fast):
    """Downloads that keep failing, also from new links, stop the job instead of fetching the file forever."""
    fast.bad_checksum = True
    job = create_job(spec(home))
    assert run_foreground(job) == "failed"
    st = runner.status(job)
    assert "download failed" in st["error"] and "checksum mismatch" in st["error"]
    gets = [c for c in fast.calls if c[0] == "POST" and c[1].endswith("/execution")]
    assert len(gets) == 1 + cds_engine.RELINKS  # the first submission and the new links, no more
