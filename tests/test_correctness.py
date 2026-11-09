"""Output values against an independent numpy reference, over the combinations that cut periods and space."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from edh_downloader import catalog, keys
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, Point, TimeSpec
from edh_downloader.planner import Plan
from edh_downloader.runner import create_job, run_foreground

REDUCE = {"mean": np.nanmean, "min": np.nanmin, "max": np.nanmax, "sum": np.nansum, "first": lambda a, axis: a[0]}


def _times(fake):
    return pd.DatetimeIndex(pd.Timestamp("1940-01-01") + pd.to_timedelta(fake.t, unit="h"))


def _labels(tt, freq, hours, tz):
    local = tt + pd.Timedelta(hours=tz)
    return {"none": local, "hours": local.floor(f"{hours}h"), "day": local.floor("D"),
            "month": local.to_period("M").to_timestamp(), "year": local.to_period("Y").to_timestamp()}[freq]


def _space(fake, area, conv="-180..180"):
    lon_out = fake.lon % 360 if conv == "0..360" else (fake.lon + 180) % 360 - 180
    if area.mode == "global":
        return np.arange(len(fake.lat)), np.argsort(lon_out, kind="stable"), lon_out
    lat_i = np.where((fake.lat >= area.south) & (fake.lat <= area.north))[0]
    l180 = (fake.lon + 180) % 360 - 180
    if area.west <= area.east:
        lon_i = np.where((l180 >= area.west) & (l180 <= area.east))[0]
        key = lon_out[lon_i]
    else:
        lon_i = np.where((l180 >= area.west) | (l180 <= area.east))[0]
        key = lon_out[lon_i] if conv == "0..360" else (l180[lon_i] - area.west) % 360
    return lat_i, lon_i[np.argsort(key, kind="stable")], lon_out


CASES = [
    ("hours7_tz-5", TimeSpec("2019-11-09", "2019-12-03", "hours", hours=7, tz_offset=-5,
                             stats={"t2m": ["mean", "min", "max", "first"]}), AreaSpec("global"), "-180..180"),
    ("day_tz+3_partial_hours", TimeSpec("2019-11-10", "2019-12-12", "day", tz_offset=3, start_hour=5, end_hour=17,
                                        stats={"t2m": ["mean", "sum"]}), AreaSpec("bbox", -60, -40, 60, 50), "-180..180"),
    ("month_antimeridian_360", TimeSpec("2019-11-01", "2019-12-31", "month", stats={"t2m": ["max"]}),
     AreaSpec("bbox", 150, -30, -150, 30), "0..360"),
    ("asstored_antimeridian_180", TimeSpec("2019-11-20", "2019-11-25", "none"),
     AreaSpec("bbox", 150, -30, -150, 30), "-180..180"),
]


@pytest.mark.parametrize("name,t,area,conv", CASES, ids=[c[0] for c in CASES])
def test_values_match_reference(home, fake_edh, name, t, area, conv):
    area.lon_convention = conv
    s = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m"], time=t, area=area,
                output=OutputSpec(directory=str(home / "out"), name=name, export_zarr=True),
                download=DownloadSpec(key="test", connections=4, min_free_gb=0.1))
    assert run_foreground(create_job(s)) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    tt = _times(fake_edh)
    lab = _labels(tt, t.freq, t.hours, t.tz_offset)
    start = pd.Timestamp(t.start) + pd.Timedelta(hours=t.start_hour)
    end = pd.Timestamp(t.end) + pd.Timedelta(hours=t.end_hour + 1)
    sel = np.where((lab >= start) & (lab < end))[0]
    periods = pd.unique(lab[sel])
    lat_i, lon_i, lon_out = _space(fake_edh, area, conv)
    src = fake_edh.data["t2m"][:, lat_i][:, :, lon_i].astype("f8")
    np.testing.assert_allclose(ds.latitude.values, fake_edh.lat[lat_i])
    np.testing.assert_allclose(ds.longitude.values, lon_out[lon_i])
    assert len(ds.valid_time) == len(periods)
    np.testing.assert_array_equal(ds.valid_time.values, pd.DatetimeIndex(periods).values)
    stats = t.stats.get("t2m") or (["none"] if t.freq == "none" else ["mean"])
    for st in stats:
        name_ = "t2m" if len(stats) == 1 and st in ("none", "mean") and not t.stats else f"t2m_{st}"
        if name_ not in ds:
            name_ = "t2m" if "t2m" in ds else name_
        out = ds[name_].values
        for k, p in enumerate(periods):
            idx = sel[lab[sel] == p]
            ref = src[idx][0] if st == "none" else REDUCE[st](src[idx], axis=0)
            np.testing.assert_allclose(out[k], ref, rtol=2e-6, atol=1e-5, err_msg=f"{name_} period {p}")


def test_partial_first_period_is_flagged(home, fake_edh):
    """Data start 2019-11-08 16:00: a daily mean for 2019-11-08 would cover only 8 hours."""
    ds = catalog.load_dataset(fake_edh.url, keys.get_key("test").secret)
    s = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m"],
                time=TimeSpec("2019-11-08", "2019-11-12", "day"), output=OutputSpec(directory=str(home), name="x"),
                download=DownloadSpec(key="test"))
    p = Plan(s, ds)
    assert any("incomplete" in w or "partial" in w or "clipped" in w for w in p.warnings), p.warnings


def test_coordinates_decode_cleanly(home, fake_edh):
    """Integer coordinates must not carry a fill value that masks real values (point 0, the time origin)."""
    s = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m", "t"], levels=[1000, 500],
                time=TimeSpec("2019-11-10", "2019-11-12", "day"),
                area=AreaSpec("points", points=[Point("a", 10, 10), Point("b", 20, 20)]),
                output=OutputSpec(directory=str(home / "out"), name="coords", export_zarr=True),
                download=DownloadSpec(key="test", connections=4, min_free_gb=0.1))
    assert run_foreground(create_job(s)) == "completed"
    for opener in (lambda: xr.open_zarr(s.zarr_path), lambda: xr.open_dataset(s.zarr_path, engine="zarr")):
        ds = opener()
        assert ds.point.values.tolist() == [0, 1], ds.point.values
        assert not np.isnat(ds.valid_time.values).any()
        assert ds.isobaricInhPa.values.tolist() == [1000, 500]
        assert ds.point.dtype.kind == "i"
