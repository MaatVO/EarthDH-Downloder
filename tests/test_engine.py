import numpy as np
import pandas as pd
import xarray as xr

from edh_downloader import quota, keys
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, Point, TimeSpec
from edh_downloader.runner import create_job, run_foreground


def spec(fake, out, **kw):
    base = dict(dataset="fake", dataset_url=fake.url, variables=["t2m"],
                time=TimeSpec("2019-11-10", "2019-11-30", "none"),
                area=AreaSpec("global"), output=OutputSpec(directory=str(out), name="o", keepbits=None),
                download=DownloadSpec(key="test", connections=4, min_free_gb=0.1))
    base.update(kw)
    return JobSpec(**base)


def times(fake):
    return pd.Timestamp("1940-01-01") + pd.to_timedelta(fake.t, unit="h")


def test_hourly_bbox_exact(home, fake_edh):
    s = spec(fake_edh, home / "out", area=AreaSpec("bbox", west=-60, east=60, south=-40, north=50))
    first = len(fake_edh.requests)
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path, consolidated=True)
    tt = times(fake_edh)
    i = np.where((tt >= "2019-11-10") & (tt < "2019-12-01"))[0]
    lat_i = np.where((fake_edh.lat >= -40) & (fake_edh.lat <= 50))[0]
    lon180 = (fake_edh.lon + 180) % 360 - 180
    lon_i = np.where((lon180 >= -60) & (lon180 <= 60))[0]
    lon_i = lon_i[np.argsort(lon180[lon_i])]
    expect = fake_edh.data["t2m"][i][:, lat_i][:, :, lon_i]
    np.testing.assert_array_equal(ds.t2m.values, expect)
    assert np.all(np.diff(ds.longitude.values) > 0)
    assert str(ds.valid_time.values[0])[:13] == "2019-11-10T00"
    assert ds.attrs["edh_status"] == "complete"
    # one request per needed chunk (+ metadata), nothing twice
    chunk_reqs = [p for p in fake_edh.requests[first:] if "/t2m/c/" in p]
    assert len(chunk_reqs) == len(set(chunk_reqs))
    assert quota.used(keys.get_key("test").fingerprint) > 0


def test_daily_stats_tz_levels_static(home, fake_edh):
    s = spec(fake_edh, home / "out", variables=["t2m", "tp", "t", "lsm"], levels=[850, 500],
             time=TimeSpec("2019-11-12", "2019-11-28", "day", tz_offset=1, stats={"t2m": ["mean", "max"]}),
             area=AreaSpec("global"), output=OutputSpec(directory=str(home / "out"), name="d", keepbits=None,
                                                        export_zarr=True, keep_icechunk=False))
    job = create_job(s)
    assert run_foreground(job) == "completed"
    assert not s.icechunk_path.exists()
    ds = xr.open_zarr(s.zarr_path)
    assert set(ds.data_vars) == {"t2m_mean", "t2m_max", "tp", "t", "lsm"}
    tt = times(fake_edh) + pd.Timedelta(hours=1)
    day = tt.floor("D")
    lon_order = np.argsort((fake_edh.lon + 180) % 360 - 180, kind="stable")
    for k, d in enumerate(pd.date_range("2019-11-12", "2019-11-28")):
        m = day == d
        np.testing.assert_allclose(ds.t2m_mean.values[k], fake_edh.data["t2m"][m].mean(0)[:, lon_order], rtol=1e-5)
        np.testing.assert_allclose(ds.t2m_max.values[k], fake_edh.data["t2m"][m].max(0)[:, lon_order])
        np.testing.assert_allclose(ds.tp.values[k], fake_edh.data["tp"][m].sum(0)[:, lon_order], rtol=1e-4)
        np.testing.assert_allclose(ds.t.values[k, 1], fake_edh.data["t"][m][:, 2].mean(0)[:, lon_order], rtol=1e-5)
    assert ds.isobaricInhPa.values.tolist() == [850, 500]
    np.testing.assert_array_equal(ds.lsm.values, fake_edh.data["lsm"][:, lon_order])


def test_points_monthly_and_resume(home, fake_edh):
    pts = [Point("a", 46.1, 11.1), Point("b", -33.9, 151.2)]
    s = spec(fake_edh, home / "out", variables=["t2m"], time=TimeSpec("2019-11-01", "2019-12-31", "none"),
             area=AreaSpec("points", points=pts), output=OutputSpec(directory=str(home / "out"), name="p"))
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert ds.point_name.values.tolist() == ["a", "b"]
    i = np.argmin(np.abs(fake_edh.lat - 46.1))
    j = np.argmin(np.abs(fake_edh.lon - 11.1))
    tt = times(fake_edh)
    sel = np.where((tt >= "2019-11-01") & (tt < "2020-01-01"))[0]
    np.testing.assert_allclose(ds.t2m.values[:, 0], fake_edh.data["t2m"][sel, i, j], rtol=2e-4)
    # running a completed job again is a no-op
    n = len(fake_edh.requests)
    assert run_foreground(job) == "completed"
    assert len(fake_edh.requests) - n <= 0


def test_sample_estimate(home, fake_edh):
    """A small sample of the job's own chunks gives measured download and Zarr sizes and a suggestion."""
    import asyncio

    from edh_downloader import catalog, chunking, quota, sample
    from edh_downloader.http import RequestCounter
    from edh_downloader.planner import Plan

    key = keys.get_key("test")
    ds = catalog.load_dataset(fake_edh.url, key.secret)
    s = spec(fake_edh, home, variables=["t2m", "tp", "lsm"], time=TimeSpec("2019-11-03", "2019-12-20", "day"))
    plan = Plan(s, ds)
    before = quota.used(key.fingerprint)
    c = RequestCounter(key.fingerprint)
    r = asyncio.run(sample.run(plan, key.secret, c, n=6))
    c.flush()
    assert r["requests"] == 6 and quota.used(key.fingerprint) - before == 6
    assert 1 <= r["suggested"] <= sample.MAX_CONNECTIONS
    assert set(r["bytes_per_chunk"]) <= {"t2m", "tp", "lsm"} and "t2m" in r["bytes_per_chunk"]
    est = sample.estimate(plan, sample.last_result(ds.url), 4, chunking.compression_ratio(plan))
    assert 0 < est["zarr"] <= est["raw"] and est["download"] > 0 and est["eta_s"]
    assert "t2m_mean" in est["ratios"] or any(n.startswith("t2m") for n in est["ratios"])
    # changing the encoding re-estimates offline from the cached samples
    s.output.compressor = "none"
    assert all(v == 1.0 for v in sample.output_ratio(Plan(s, ds)).values())


def test_chunk_layouts_and_lossless_default(home, fake_edh):
    from edh_downloader import catalog
    from edh_downloader.planner import Plan

    assert OutputSpec().keepbits is None  # lossless by default
    ds = catalog.load_dataset(fake_edh.url, "test-key")
    s = spec(fake_edh, home, time=TimeSpec("2019-11-10", "2019-12-20", "none"))
    shapes = {}
    for layout in ("auto", "timeseries", "maps"):
        s.output.chunk_layout = layout
        p = Plan(s, ds)
        shapes[layout] = p.export_chunks(p.vars[0])
        assert p.out_chunks(p.vars[0]) == p.export_chunks(p.vars[0]) or layout != "auto"
    assert shapes["maps"][1:] == p.shape_space and shapes["maps"][0] <= 24
    assert shapes["timeseries"][0] == p.n_periods  # whole (short) series in one chunk
    s.output.chunk_layout, s.output.chunk_time = "custom", 10
    p = Plan(s, ds)
    assert p.export_chunks(p.vars[0])[0] == 10


def test_rechunked_sharded_export(home, fake_edh):
    import zarr

    s = spec(fake_edh, home / "out", variables=["t2m", "t", "lsm"], levels=[1000, 500],
             time=TimeSpec("2019-11-03", "2019-12-20", "none"), area=AreaSpec("bbox", west=-90, east=90, south=-60, north=70),
             output=OutputSpec(directory=str(home / "out"), name="rc", chunk_layout="timeseries", sharding=True,
                               compressor="gzip", clevel=4, dtype="float64", extra_attrs={"project": "X"}))
    job = create_job(s)
    assert run_foreground(job) == "completed"
    g = zarr.open_group(str(s.zarr_path), mode="r")
    a = g["t2m"]
    assert a.dtype == np.float64 and a.shards is not None and a.chunks[0] == a.shape[0]
    assert g.attrs["project"] == "X"
    ds = xr.open_zarr(s.zarr_path)
    ic = xr.open_zarr(__import__("icechunk").Repository.open(__import__("icechunk").local_filesystem_storage(
        str(s.icechunk_path))).readonly_session("main").store, consolidated=False)
    for v in ("t2m", "t", "lsm"):
        np.testing.assert_array_equal(ds[v].values, ic[v].values)  # rechunk is exact
    tt = times(fake_edh)
    i = np.where((tt >= "2019-11-03") & (tt < "2019-12-21"))[0]
    lat_i = np.where((fake_edh.lat >= -60) & (fake_edh.lat <= 70))[0]
    lon180 = (fake_edh.lon + 180) % 360 - 180
    lon_i = np.where((lon180 >= -90) & (lon180 <= 90))[0]
    lon_i = lon_i[np.argsort(lon180[lon_i])]
    np.testing.assert_array_equal(ds.t2m.values, fake_edh.data["t2m"][i][:, lat_i][:, :, lon_i].astype("f8"))


def test_recommendations(home, fake_edh):
    from edh_downloader import catalog, chunking
    from edh_downloader.planner import Plan

    ds = catalog.load_dataset(fake_edh.url, "test-key")
    pts = AreaSpec("points", points=[Point("a", 1, 1)])
    assert chunking.recommend(Plan(spec(fake_edh, home, area=pts), ds))[0] == "timeseries"
    short = spec(fake_edh, home, time=TimeSpec("2019-11-10", "2019-11-11", "none"))
    assert chunking.recommend(Plan(short, ds))[0] == "maps"
    rows = chunking.analyze(Plan(short, ds))
    assert {r["layout"] for r in rows} == {"auto", "timeseries", "maps", "custom"}
    assert next(r for r in rows if r["layout"] == "auto")["export"] == "direct copy"


def test_extend_time_series_forward_and_backward(home, fake_edh):
    from edh_downloader import store

    # 1. Base job: 2019-11-15 to 2019-11-19
    s1 = spec(fake_edh, home / "out", time=TimeSpec("2019-11-15", "2019-11-19", "none"),
              area=AreaSpec("bbox", west=-30, east=30, south=-20, north=20),
              output=OutputSpec(directory=str(home / "out"), name="ext_test", keep_icechunk=True, export_zarr=True))
    j1 = create_job(s1)
    assert run_foreground(j1) == "completed"

    # Inspect store
    info = store.inspect_icechunk_for_extend(s1.icechunk_path)
    assert info["ok"] is True
    assert info["time_len"] == 5 * 24

    # Also test inspection of companion zarr
    info_zarr = store.inspect_icechunk_for_extend(s1.zarr_path)
    assert info_zarr["ok"] is True

    # 2. Extend Forward (append): 2019-11-20 to 2019-11-24
    s2 = spec(fake_edh, home / "out", time=TimeSpec("2019-11-20", "2019-11-24", "none"),
              area=AreaSpec("bbox", west=-30, east=30, south=-20, north=20),
              output=OutputSpec(directory=str(home / "out"), name="ext_test",
                                target_mode="extend", target_store=str(s1.icechunk_path),
                                extend_direction="forward", keep_icechunk=True, export_zarr=True))
    j2 = create_job(s2)
    assert run_foreground(j2) == "completed"

    ds2 = xr.open_zarr(s1.zarr_path)
    assert len(ds2.valid_time) == 10 * 24
    assert str(ds2.valid_time.values[0])[:10] == "2019-11-15"
    assert str(ds2.valid_time.values[-1])[:10] == "2019-11-24"

    # 3. Extend Backward (prepend): 2019-11-10 to 2019-11-14
    s3 = spec(fake_edh, home / "out", time=TimeSpec("2019-11-10", "2019-11-14", "none"),
              area=AreaSpec("bbox", west=-30, east=30, south=-20, north=20),
              output=OutputSpec(directory=str(home / "out"), name="ext_test",
                                target_mode="extend", target_store=str(s1.icechunk_path),
                                extend_direction="backward", keep_icechunk=True, export_zarr=True))
    j3 = create_job(s3)
    assert run_foreground(j3) == "completed"

    ds3 = xr.open_zarr(s1.zarr_path)
    assert len(ds3.valid_time) == 15 * 24
    assert str(ds3.valid_time.values[0])[:10] == "2019-11-10"
    assert str(ds3.valid_time.values[-1])[:10] == "2019-11-24"

    # Verify data matches fake_edh over the whole 15 days
    tt = times(fake_edh)
    i = np.where((tt >= "2019-11-10") & (tt < "2019-11-25"))[0]
    lat_i = np.where((fake_edh.lat >= -20) & (fake_edh.lat <= 20))[0]
    lon180 = (fake_edh.lon + 180) % 360 - 180
    lon_i = np.where((lon180 >= -30) & (lon180 <= 30))[0]
    lon_i = lon_i[np.argsort(lon180[lon_i])]
    expect = fake_edh.data["t2m"][i][:, lat_i][:, :, lon_i]
    np.testing.assert_array_equal(ds3.t2m.values, expect)
