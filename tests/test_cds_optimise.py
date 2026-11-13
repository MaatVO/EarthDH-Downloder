"""Fewer CDS requests (whole months, variable groups, request size free of the memory budget) and the processing
that makes it possible (slabs, periods accumulated in pieces), plus the guards around them."""

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from fake_cds import value
from test_cds_engine import fast, ledger, spec  # noqa: F401  (fixture)
from test_cds_plan import BOX, ERA, SURFACE, plan
from test_cds_plan import spec as pspec

from edh_downloader.aggregate import reduce_periods
from edh_downloader.job import AreaSpec
from edh_downloader.planner import PlanError
from edh_downloader.runner import create_job, run_foreground, status
from edh_downloader.sources.cds import catalog, grib
from edh_downloader.sources.cds.engine import Running, period_groups
from edh_downloader.sources.cds.grib import Grid, Message
from edh_downloader.sources.cds.output import CdsOutput
from edh_downloader.sources.cds.planner import CdsPlan, default_limit, fit_costs

GLOBAL = AreaSpec(mode="global")


# ------------------------------------------------------------------ planning
def test_request_size_does_not_depend_on_memory(cds):
    s = pspec(variables=["2m_temperature"], start="2011-01-01", end="2020-12-31", area=GLOBAL)
    s.download.memory_gb = 1.0
    p = plan(s)
    # 10 GB per request (~4 800 global fields): six months each, whatever the memory
    assert len(p.requests) == 20 and all(len(r.inputs["month"]) == 6 for r in p.requests)


@pytest.mark.parametrize("time, n", [({"start_hour": 6}, 1), ({"freq": "hours", "hours": 5,
                                                                  "stats": {v: ["first"] for v in SURFACE}}, 1)])
def test_edges_asked_whole(cds, time, n):
    p = plan(pspec(time=time))
    assert len(p.requests) == n and p.rounded
    assert p.summary()["steps"] < 366 * 24 or time.get("freq")  # the job keeps its own steps


def test_edges_never_beyond_the_coverage(cds):
    end = pd.Timestamp(catalog.describe(ERA)["end"]).tz_localize(None)
    s = pspec(start=str((end - pd.Timedelta(days=40)).date()), end=str(end.date()))
    p = plan(s)
    last = max(pd.Timestamp(r.end) for r in p.requests)
    assert last <= end + pd.Timedelta(hours=23)


def test_variables_split_when_that_packs_more_years(cds):
    p = plan(pspec(start="1991-01-01", end="2020-12-31"))
    assert len(p.requests) == 13  # all five together: 15 (two years each)
    assert all(r.fields <= 121000 for r in p.requests)
    assert sorted({v for r in p.requests for v in r.variables}) == sorted(SURFACE)
    years = {}
    for r in p.requests:
        for v in r.variables:
            years.setdefault(v, []).extend(r.inputs["year"])
    assert all(sorted(y) == [str(x) for x in range(1991, 2021)] for y in years.values())  # each once


def test_known_limits_before_any_costing():
    assert default_limit(ERA) == 121000 and default_limit("reanalysis-era5-pressure-levels") == 60000
    assert default_limit(ERA, {ERA: 100000}) == 100000 and default_limit("x") == 60000


def test_several_products_refused(cds):
    with pytest.raises(PlanError, match="choose one value"):
        plan(pspec(options={"product_type": ["reanalysis", "ensemble_mean"]}))
    with pytest.raises(PlanError, match="cannot be stored"):
        plan(pspec(options={"product_type": ["ensemble_members"]}))


def test_frozen_period(cds):
    s = pspec(start="2020-01-01", end="2020-12-31")
    p = CdsPlan(s, catalog.describe(ERA), catalog.constraints(ERA), limit=121000,
                frozen=("2020-01-01 00:00:00", "2020-06-30 23:00:00"))
    assert str(p.instants[-1]) == "2020-06-30 23:00:00"


def test_fit_costs_plans_again_when_the_cds_counts_more(cds):
    s = pspec(start="2019-01-01")
    desc, table = catalog.describe(ERA), catalog.constraints(ERA)
    make = lambda lim: CdsPlan(s, desc, table, limit=lim)  # noqa: E731
    calls = []

    async def costing(r):
        calls.append(r.fields)
        return {"cost": 2 * r.fields, "limit": 121000}

    p = asyncio.run(fit_costs(make(60000), make, costing))
    assert p.limit == 121000 and all(r.cost <= 121000 for r in p.requests)
    assert len(p.requests) > 1 and calls


# ------------------------------------------------------------------ processing
def test_running_matches_reduce_periods():
    rng = np.random.default_rng(0)
    a = rng.normal(size=(50, 3, 4)).astype(np.float32)
    a[rng.random(a.shape) < 0.2] = np.nan
    a[:, 0, 0] = np.nan
    for stat in ("mean", "sum", "min", "max", "first"):
        acc = Running([stat])
        for c in range(0, 50, 7):
            acc.add(a[c:c + 7])
        np.testing.assert_allclose(acc.result(stat), reduce_periods(a, np.array([0]), stat)[0], rtol=1e-5)


def test_period_groups():
    starts, ends = np.array([0, 3, 6, 30, 33]), np.array([3, 6, 30, 33, 36])
    assert list(period_groups(starts, ends, 6)) == [(0, 2), (2, 3), (3, 5)]


def test_small_memory_budget_streams_long_periods(home, fast):
    # 81 points: a slab of 5 steps for this budget, shorter than one local day
    s = spec(home, start="2020-01-02", end="2020-01-03",
             time={"freq": "day", "tz_offset": 1, "stats": {"2m_temperature": ["mean", "max"]}})
    s.download.memory_gb = 5 * 81 * 4 * 6 / 1e9
    job = create_job(s)
    assert run_foreground(job) == "completed"
    ds = xr.open_zarr(s.zarr_path)
    LAT, LON = np.meshgrid(ds.latitude.values, ds.longitude.values, indexing="ij")
    for k, day in enumerate(pd.date_range("2020-01-02", "2020-01-03")):
        utc = pd.date_range(day - pd.Timedelta(hours=1), periods=24, freq="h")
        t2m = np.stack([value(167, t, LAT, LON) for t in utc])
        tp = np.stack([value(228, t, LAT, LON) for t in utc])
        np.testing.assert_allclose(ds.t2m_mean.values[k], t2m.mean(0), atol=1e-3)
        np.testing.assert_allclose(ds.t2m_max.values[k], t2m.max(0), atol=1e-3)
        np.testing.assert_allclose(ds.tp.values[k], tp.sum(0), atol=1e-2)


def test_whole_months_asked_and_extra_steps_dropped(home, fast):
    s = spec(home, start="2020-01-30", end="2020-03-02")
    job = create_job(s)
    assert run_foreground(job) == "completed"
    assert len(ledger(job)) == 1  # January to March in one request
    ds = xr.open_zarr(s.zarr_path)
    t = pd.DatetimeIndex(ds.valid_time.values)
    assert len(t) == (2 + 29 + 2) * 24 and t[0] == pd.Timestamp("2020-01-30") and t[-1] == pd.Timestamp("2020-03-02 23:00")
    LAT, LON = np.meshgrid(ds.latitude.values, ds.longitude.values, indexing="ij")
    np.testing.assert_allclose(ds.t2m.values[-1], value(167, t[-1], LAT, LON), atol=1e-3)
    d = json.loads((job / "requests.json").read_text())
    assert d["period"] == ["2020-01-30 00:00:00", "2020-03-02 23:00:00"]


def test_missing_ecccodes_stops_before_any_request(home, fast, monkeypatch):
    def broken():
        raise RuntimeError("reading GRIB needs the ecCodes library, which could not be loaded: nope")

    monkeypatch.setattr(grib, "_eccodes", broken)
    job = create_job(spec(home))
    assert run_foreground(job) == "failed"
    assert not (job / "requests.json").exists() and "ecCodes" in status(job)["error"]  # nothing planned


# ------------------------------------------------------------------ output layout
def _msg(param=130, level_type="heightAboveGround", level=10.0):
    return Message(0, param, "t", "t", "Temperature", "K", "instant", level_type, level,
                   pd.Timestamp("2020-01-01"), 4, 3, "regular_ll")


def test_height_levels_are_a_level_dimension():
    p = SimpleNamespace(level_field="height_level", levels=["10", "50"], variables=["temperature"])
    v = CdsOutput.describe_variables([_msg(level=10.0), _msg(level=50.0)], p)
    assert v[0]["kind"] == "level"
    p = SimpleNamespace(level_field=None, levels=[], variables=["2m_temperature"])
    assert CdsOutput.describe_variables([_msg(167, level=2.0)], p)[0]["kind"] == "time"


VAR = {"param": 167, "name": "t2m", "short_name": "2t", "long_name": "T", "units": "K", "step_type": "instant",
       "level_type": "surface", "kind": "time", "form_name": "2m_temperature"}


@pytest.mark.parametrize("west, east, n, first", [(10, 12, 3, 10), (170, -170, 21, 170)])
def test_whole_domain_box_cut_in_longitude(cds, west, east, n, first):
    p = plan(pspec(variables=["2m_temperature"], end="2020-01-01",
                   area=AreaSpec(mode="bbox", west=west, south=44, east=east, north=47)))
    p.area = None  # as for a dataset delivered whole
    g = Grid("regular", np.arange(90, -91, -1.0), np.arange(0, 360, 1.0))
    out = CdsOutput(p, g, [dict(VAR)])
    assert out.shape_space == (4, n) and out.out_lon[0] == first


def test_projected_grids_are_sharded_in_space():
    from edh_downloader import chunking

    class P:
        def out_dims(self, vp):
            return ("valid_time", "y", "x")

        def out_shape(self, vp):
            return (8760, 1069, 1069)

    ft, fy, fx = chunking.suggest_shard_factors(P(), None, (24, 64, 64))
    assert fy > 1 and fx > 1
    out = SimpleNamespace(sharding=True, shard_auto=True)
    assert chunking.shard_shape(P(), None, (24, 64, 64), out)[1:] == (64 * fy, 64 * fx)
