"""NetCDF products converted into stores, end to end against the fake CDS (CMIP6-like files: mid-month times, a
noleap calendar, latitudes south to north, longitudes 0..360, bounds variables), and kept as files when they are
not a grid."""

import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fake_cds import nc_times, value
from test_cds_engine import fast, spec  # noqa: F401  (fixture)

from edh_downloader import runner
from edh_downloader.job import AreaSpec
from edh_downloader.runner import create_job, run_foreground
from edh_downloader.sources.cds import engine as cds_engine
from edh_downloader.sources.cds import netcdf

CMIP = "projections-cmip6"
OPTS = {"temporal_resolution": "monthly", "experiment": "historical", "model": "access_cm2"}


@pytest.fixture(autouse=True)
def cmip_licence(cds):
    cds.licences.append({"id": "cmip6-wps", "revision": 1})


def cmip(home, variables=("near_surface_air_temperature",), start="2000-01-01", end="2001-06-30", **kw):
    return spec(home, dataset=CMIP, variables=list(variables), start=start, end=end,
                options=dict(OPTS, **kw.pop("options", {})), **kw)


def months(start, end):
    return nc_times({"year": [str(y) for y in range(int(start[:4]), int(end[:4]) + 1)],
                     "month": [f"{m:02d}" for m in range(1, 13)]})


def test_monthly_grid_converted(home, fast):
    s = cmip(home)
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path, consolidated=True)
    assert set(ds.data_vars) == {"tas"}  # the file's own name; bounds variables are not stored
    assert ds.tas.dims == ("valid_time", "latitude", "longitude")
    assert ds.tas.attrs["units"] == "K" and ds.tas.attrs["cds_variable"] == "near_surface_air_temperature"
    t = pd.DatetimeIndex(ds.valid_time.values)
    want = [x for x in months("2000", "2001") if x < pd.Timestamp("2001-07-01")]
    assert list(t) == want  # mid-month stamps, only the months asked for
    lat, lon = ds.latitude.values, ds.longitude.values
    assert list(lat) == [45, 46, 47] and list(lon) == [10, 11, 12]
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    for k in (1, 7, 17):
        np.testing.assert_allclose(ds.tas.values[k], value(167, t[k], LAT, LON), atol=1e-3)
    assert np.isnan(ds.tas.values[0, 0, 0]) and not np.isnan(ds.tas.values[0, 1, 1])  # missing stays missing
    st = runner.status(job)
    assert st["status"] == "completed" and st["cds_convert"] and not (job / "spool").exists()
    assert not (home / "out" / "c.files").exists()
    log = (job / "log.txt").read_text()
    assert "noleap calendar" in log


def test_yearly_mean_and_zipped_results(home, fast):
    fast.netcdf_kind = "zip"
    s = cmip(home, time={"freq": "year"})
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    t = pd.DatetimeIndex(ds.valid_time.values)
    assert list(t) == [pd.Timestamp("2000-01-01"), pd.Timestamp("2001-01-01")]
    lat, lon = ds.latitude.values, ds.longitude.values
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    want = np.mean([value(167, x, LAT, LON) for x in months("2001", "2001")], axis=0)  # a year step: whole years
    np.testing.assert_allclose(ds.tas.values[1], want, atol=1e-3)


def test_levels_from_the_files(home, fast):
    s = cmip(home, variables=["air_temperature"], end="2000-03-31", options={"level": ["500", "850"]})
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert ds.ta.dims == ("valid_time", "plev", "latitude", "longitude")
    assert list(ds.plev.values) == [50000.0, 85000.0] and ds.plev.attrs["units"] == "Pa"
    t = pd.Timestamp(ds.valid_time.values[2])
    lat, lon = ds.latitude.values, ds.longitude.values
    LAT, LON = np.meshgrid(lat, lon, indexing="ij")
    np.testing.assert_allclose(ds.ta.values[2, 1], value(130, t, LAT, LON) + 850 / 10000, atol=1e-3)


def test_not_a_grid_is_kept_as_files(home, fast):
    fast.netcdf_kind = "stations"
    s = cmip(home, end="2000-02-28")
    job = create_job(s)
    assert run_foreground(job) == "completed"
    d = home / "out" / "c.files"
    names = sorted(p.name for p in d.iterdir())
    assert "manifest.json" in names and any(n.endswith(".nc") for n in names)
    man = json.loads((d / "manifest.json").read_text())
    assert "no gridded field" in man["not_converted"]
    st = runner.status(job)
    assert st["status"] == "completed" and "kept as files" in st["message"]
    assert not s.zarr_path.exists() and not s.icechunk_path.exists()


def test_paused_during_conversion_resumes(home, fast, monkeypatch):
    fast.limits[CMIP] = 12.0  # one request (one file, one unit) per year
    s = cmip(home, start="2000-01-01", end="2002-12-31")
    job = create_job(s)
    orig = cds_engine.CdsEngine._process_unit_nc
    calls = []

    def stop_after_first(self, u, index):
        orig(self, u, index)
        calls.append(u.index)
        if len(calls) == 1:
            self.request_stop("pause")

    monkeypatch.setattr(cds_engine.CdsEngine, "_process_unit_nc", stop_after_first)
    assert run_foreground(job) == "paused"
    assert len(runner.status(job)["units_done"]) == 1
    submitted = fast.submissions
    assert run_foreground(job) == "completed"
    assert fast.submissions == submitted  # nothing asked again
    assert sorted(calls) == sorted(set(calls)) and len(calls) == runner.status(job)["units_total"]
    ds = xr.open_zarr(s.zarr_path)
    nan = np.argwhere(np.isnan(ds.tas.values))
    assert len(ds.valid_time) == 36 and [list(x) for x in nan] == [[0, 0, 0], [12, 0, 0], [24, 0, 0]]  # one per file


def test_points_from_netcdf(home, fast):
    from edh_downloader.job import Point

    s = cmip(home, end="2000-03-31", area=AreaSpec(mode="points", points=[Point("a", 45.2, 10.1),
                                                                          Point("b", 46.9, 11.8)]))
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    assert ds.tas.dims == ("valid_time", "point")
    assert list(ds.latitude.values) == [45, 47] and list(ds.longitude.values) == [10, 12]


# ------------------------------------------------------------------ the reader alone

def test_unpack_and_scan(tmp_path):
    import zipfile

    from fake_cds import netcdf_result

    inputs = {"year": ["2000"], "month": ["01", "02"], "variable": "near_surface_air_temperature",
              "area": [47, 10, 45, 12]}
    z = tmp_path / "r.zip"
    z.write_bytes(netcdf_result(CMIP, inputs, "zip"))
    files = netcdf.unpack(z, tmp_path / "u")
    assert [p.name for p in files] == ["0001_tas_Amon_fake.nc"]  # the README is left out
    assert netcdf.unpack(z, tmp_path / "u") == files  # reused
    sc = netcdf.scan(files)
    assert list(sc.fields) == ["tas"] and sc.grid.kind == "regular" and sc.grid.shape == (3, 3)
    assert list(sc.times) == [pd.Timestamp("2000-01-15 12:00"), pd.Timestamp("2000-02-15 12:00")]
    with netcdf.Reader() as r:
        a = r.read(sc.fields["tas"][0], 0, 2, None)
    assert a.shape == (2, 3, 3) and a.dtype == np.float32 and np.isnan(a[0, 0, 0])
    with zipfile.ZipFile(z) as zz:  # an archive member cannot land outside the extraction folder
        assert all(not n.startswith("/") for n in zz.namelist())


def test_two_products_for_one_time_are_not_a_store(tmp_path):
    from fake_cds import netcdf_result

    inputs = {"year": ["2000"], "month": ["01"], "variable": "near_surface_air_temperature",
              "area": [47, 10, 45, 12]}
    a, b = tmp_path / "a.nc", tmp_path / "b.nc"
    a.write_bytes(netcdf_result(CMIP, inputs))
    b.write_bytes(netcdf_result(CMIP, inputs))
    with pytest.raises(netcdf.NotGridded, match="one value per variable and time"):
        netcdf.scan([a, b])
