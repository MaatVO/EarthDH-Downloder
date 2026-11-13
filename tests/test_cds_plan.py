"""CDS request planning: fewest requests under the cost and size limits, exact selections, canonical requests."""

import pytest

from edh_downloader.job import AreaSpec, JobSpec, Point, TimeSpec
from edh_downloader.planner import PlanError
from edh_downloader.sources.cds import catalog
from edh_downloader.sources.cds.planner import CdsPlan

ERA = "reanalysis-era5-single-levels"
PL = "reanalysis-era5-pressure-levels"
CERRA = "reanalysis-cerra-single-levels"
SURFACE = ["2m_temperature", "total_precipitation", "10m_u_component_of_wind", "10m_v_component_of_wind",
           "surface_pressure"]
BOX = AreaSpec(mode="bbox", west=10, south=45, east=12, north=47)


def spec(dataset=ERA, variables=SURFACE, start="2020-01-01", end="2020-12-31", area=BOX, **kw):
    s = JobSpec(source="cds", dataset=dataset, variables=list(variables), time=TimeSpec(start, end, **kw.pop("time", {})),
                area=area)
    s.cds.options = kw.pop("options", {"product_type": ["reanalysis"]})
    for k, v in kw.items():
        setattr(s.cds, k, v)
    return s


def plan(s, limit=121000.0):
    return CdsPlan(s, catalog.describe(s.dataset), catalog.constraints(s.dataset), limit=limit)


def test_a_year_of_five_variables_is_one_request(cds):
    p = plan(spec())
    assert len(p.requests) == 1
    r = p.requests[0].inputs
    assert r["month"] == [f"{m:02d}" for m in range(1, 13)] and len(r["day"]) == 31 and len(r["time"]) == 24
    assert r["variable"] == sorted(SURFACE) and r["area"] == ["47", "10", "45", "12"]
    assert r["data_format"] == "grib" and r["download_format"] == "unarchived" and r["product_type"] == ["reanalysis"]
    assert p.requests[0].fields == 366 * 24 * 5  # real dates, as the CDS costing counts
    assert p.summary()["steps"] == 366 * 24


def test_years_merge_while_they_fit(cds):
    assert len(plan(spec(start="2019-01-01")).requests) == 1  # 2 × 44 640 fields < 121 000
    p = plan(spec(start="2019-01-01"), limit=60000)
    assert [r.inputs["year"] for r in p.requests] == [["2019"], ["2020"]]


def test_one_month_per_request_when_asked(cds):
    p = plan(spec(pack="month"))
    assert len(p.requests) == 12 and all(len(r.inputs["month"]) == 1 for r in p.requests)


def test_too_many_variables_split_the_months_not_the_variables(cds):
    names = [v["name"] for v in catalog.describe(ERA)["variables"]][:20]
    p = plan(spec(variables=names))
    assert len(p.requests) == 2  # 14 880 fields a month: 8 months, then 4
    assert [len(r.inputs["month"]) for r in p.requests] == [8, 4]
    assert all(r.fields <= 121000 for r in p.requests)
    assert all(r.inputs["variable"] == sorted(names) for r in p.requests)


def test_pressure_levels_split_by_variable_when_a_month_is_too_big(cds):
    levels = catalog.describe(PL)["levels"]["values"]
    vars_ = [v["name"] for v in catalog.describe(PL)["variables"]][:10]
    p = plan(spec(dataset=PL, variables=vars_, start="2020-01-01", end="2020-01-31", area=BOX), limit=60000)
    s = spec(dataset=PL, variables=vars_, start="2020-01-01", end="2020-01-31")
    s.levels = [float(l) for l in levels]
    p = plan(s, limit=60000)
    assert len(p.requests) == 5  # 27 528 fields per variable and month: 2 variables per request
    assert all(len(r.inputs["variable"]) == 2 and len(r.inputs["pressure_level"]) == len(levels) for r in p.requests)
    assert all(r.fields <= 60000 for r in p.requests)


def test_partial_months_are_asked_whole_when_that_saves_requests(cds):
    p = plan(spec(start="2020-01-15", end="2020-03-10"))
    assert len(p.requests) == 1 and p.rounded  # one queue slot instead of three, for 26 more days of data
    r = p.requests[0]
    assert r.inputs["month"] == ["01", "02", "03"] and len(r.inputs["day"]) == 31
    assert r.fields == (31 + 29 + 31) * 24 * 5
    assert p.summary()["first"] == "2020-01-15 00:00:00"  # the job's own period is unchanged


def test_partial_months_stay_exact_when_rounding_saves_nothing(cds):
    p = plan(spec(start="2020-01-15", end="2020-03-10"), limit=24 * 31 * 5)  # one month per request at most
    got = [(r.inputs["month"], r.inputs["day"][0], r.inputs["day"][-1]) for r in p.requests]
    assert got == [(["01"], "15", "31"), (["02"], "01", "29"), (["03"], "01", "10")] and not p.rounded


def test_only_the_needed_hours(cds):
    p = plan(spec(end="2020-01-31", time={"freq": "hours", "hours": 6,
                                          "stats": {v: ["first"] for v in SURFACE}}))
    assert p.requests[0].inputs["time"] == ["00:00", "06:00", "12:00", "18:00"]
    p = plan(spec(end="2020-01-31", time={"freq": "hours", "hours": 6}))  # means need every hour
    assert len(p.requests[0].inputs["time"]) == 24
    p = plan(spec(start="2020-01-01", end="2020-01-01", time={"start_hour": 6, "end_hour": 11}))
    assert p.requests[0].inputs["time"] == [f"{h:02d}:00" for h in range(6, 12)]


def test_local_days_reach_into_the_previous_utc_day(cds):
    p = plan(spec(end="2020-01-31", time={"freq": "day", "tz_offset": 1}))
    first, second = p.requests
    assert (first.inputs["year"], first.inputs["month"], first.inputs["day"], first.inputs["time"]) == (
        ["2019"], ["12"], ["31"], ["23:00"])
    assert second.inputs["month"] == ["01"] and second.inputs["day"][-1] == "31"


def test_antimeridian_box_and_grid(cds):
    p = plan(spec(end="2020-01-31", area=AreaSpec(mode="bbox", west=170, south=-50, east=-170, north=-30),
                  grid=[1.0, 1.0]))
    r = p.requests[0].inputs
    assert r["area"] == ["-30", "170", "-50", "190"]
    assert r["grid"] == "1/1"  # a string: a list would count twice in the cost
    assert p.points == 21 * 21


def test_size_limit_splits_big_global_requests(cds):
    p = plan(spec(area=AreaSpec(mode="global")))  # 0.25°: ~2 MB per field
    assert "area" not in p.requests[0].inputs
    assert all(r.bytes_est <= 10e9 for r in p.requests)
    # all five variables: one month (3 720 fields, ~7.7 GB) per request, 12 requests; split in groups of three
    # and two variables, two and three months fit: 6 + 4 requests
    assert len(p.requests) == 10
    assert sorted({len(r.inputs["variable"]) for r in p.requests}) == [2, 3]
    assert p.summary()["download_bytes_est"] == sum(r.bytes_est for r in p.requests)


def test_cerra_is_cut_locally(cds):
    p = plan(spec(dataset=CERRA, variables=["2m_temperature"], end="2020-01-31",
                  options={"level_type": "surface_or_atmosphere", "data_type": ["reanalysis"],
                           "product_type": "analysis"}), limit=90000)
    r = p.requests[0].inputs
    assert "area" not in r and r["time"] == [f"{h:02d}:00" for h in range(0, 24, 3)]
    assert p.bytes_per_field() == 1069 * 1069 * 2 + 200
    assert any("whole domain" in w for w in p.warnings)
    with pytest.raises(PlanError, match="regridded"):
        plan(spec(dataset=CERRA, variables=["2m_temperature"], end="2020-01-31", grid=[0.1, 0.1],
                  options={"level_type": "surface_or_atmosphere", "data_type": ["reanalysis"],
                           "product_type": "analysis"}))


def test_options_are_checked(cds):
    assert plan(spec(options={})).options["product_type"] == ["reanalysis"]  # the form's default
    cerra = {"level_type": "surface_or_atmosphere", "data_type": ["reanalysis"], "product_type": "forecast"}
    with pytest.raises(PlanError, match="leadtime"):  # needed for forecasts only
        plan(spec(dataset=CERRA, variables=["2m_temperature"], end="2020-01-31", options=cerra))
    with pytest.raises(PlanError, match="not available"):
        plan(spec(options={"product_type": ["nope"]}))
    with pytest.raises(PlanError, match="unknown options"):
        plan(spec(options={"product_type": ["reanalysis"], "colour": "blue"}))
    with pytest.raises(PlanError, match="unknown variables"):
        plan(spec(variables=["nope"]))
    with pytest.raises(PlanError, match="not available"):
        s = spec(dataset=PL, variables=["temperature"])
        s.levels = [4]  # 3 hPa exists, 4 does not
        plan(s)


def test_requests_are_checked_against_the_constraints(cds):
    assert plan(spec()).invalid() == {}
    p = plan(spec(start="2018-12-01", end="2019-01-31"))  # the test table only knows 2019 and 2020
    assert p.invalid() == {0: {"year": ["2018"]}}


def test_same_selection_same_requests(cds):
    a = plan(spec())
    b = plan(spec(variables=list(reversed(SURFACE))))
    assert [r.hash for r in a.requests] == [r.hash for r in b.requests]


def test_points_become_one_box(cds):
    p = plan(spec(end="2020-01-31", area=AreaSpec(mode="points", points=[Point("a", 45.1, 11.2),
                                                                           Point("b", 46.0, 12.0)])))
    n, w, s, e = (float(x) for x in p.requests[0].inputs["area"])
    assert n >= 46 and s <= 45.1 and w <= 11.2 and e >= 12


def test_timeseries_one_request_per_point(cds):
    s = spec(dataset="reanalysis-era5-single-levels-timeseries", variables=["2m_temperature"],
             end="2020-12-31", area=AreaSpec(mode="points", points=[Point("a", 45.1, 11.2), Point("b", 46, 370)]),
             options={})
    p = plan(s)
    assert [r.inputs["location"] for r in p.requests] == [{"latitude": 45.1, "longitude": 11.2},
                                                          {"latitude": 46, "longitude": 10}]
    assert p.requests[0].inputs["date"] == "2020-01-01/2020-12-31"
    with pytest.raises(PlanError, match="points"):
        plan(spec(dataset="reanalysis-era5-single-levels-timeseries", variables=["2m_temperature"], options={}))


def test_tables_are_refused(cds):
    with pytest.raises(PlanError, match="in-situ"):
        plan(spec(dataset="insitu-observations-surface-land", variables=[], options={}))


# ------------------------------------------------------------------ the plan the form shows

def costing_calls(cds):
    return len([c for c in cds.calls if c[1].endswith("/costing")])


def test_service_plan_learns_the_limit_and_checks_costs(cds):
    from edh_downloader import service

    names = [v["name"] for v in catalog.describe(ERA)["variables"]][:20]
    s = spec(variables=names)
    s.download.key = "cds-test"
    r = service.plan(s.to_dict(), 8, True)
    assert r["ok"] and r["checked"] and r["licences_missing"] == []
    assert r["summary"]["requests"] == 2  # planned again with the real limit (121 000, not the default 60 000)
    assert all(q["cost"] == q["fields"] for q in r["requests"])
    n = costing_calls(cds)
    assert n <= 4
    r2 = service.plan(s.to_dict(), 8, True)  # the limit is remembered: no planning twice
    assert r2["summary"]["requests"] == 2 and costing_calls(cds) == n  # costs remembered: no calls
    assert r2["invalid"] == {}


def test_service_plan_shrinks_when_cds_counts_more(cds):
    from edh_downloader import service

    cds.cost_factor = 2.0
    s = spec(start="2019-01-01")  # 89 280 fields: one request by the local count, 178 560 by the server's
    s.download.key = "cds-test"
    r = service.plan(s.to_dict(), 8, True)
    assert r["summary"]["requests"] == 2
    assert all(q["cost"] <= 121000 for q in r["requests"])


def test_service_plan_without_a_key_or_licence(cds):
    from edh_downloader import keys, service

    cds.licences = []
    s = spec()
    s.download.key = "cds-test"
    r = service.plan(s.to_dict(), 8, True)
    assert [l["id"] for l in r["licences_missing"]] == ["cc-by"]
    keys.remove_key("cds-test")
    r = service.plan(s.to_dict(), 8, True)
    assert r["ok"] and not r["checked"] and "key" in r["note"]
    assert service.plan(spec(options={"product_type": ["nope"]}).to_dict(), 8, True)["ok"] is False


def test_service_plan_suggests_time_series_for_points(cds):
    from edh_downloader import service

    s = spec(end="2020-01-31", area=AreaSpec(mode="points", points=[Point("a", 45.1, 11.2)]))
    assert service.plan(s.to_dict(), 8, True)["alternative"]["dataset"] == ERA + "-timeseries"


# ------------------------------------------------------------------ the form: options that apply

def test_only_the_options_that_apply_are_shown(cds):
    from edh_downloader import service

    def names(sel):
        r = service.cds_options(CERRA, sel)
        return {o["name"]: o["values"] for o in r["options"]}, r["dropped"]

    shown, _ = names({"variable": ["2m_temperature"]})
    assert "leadtime_hour" in shown and "soil_layer" not in shown  # soil layers only for soil fields
    shown, dropped = names({"variable": ["2m_temperature"], "product_type": "analysis", "leadtime_hour": ["1"]})
    assert "leadtime_hour" not in shown and dropped == ["leadtime_hour"]  # a lead time is for forecasts only
    shown, _ = names({"variable": ["total_precipitation"]})
    assert shown["product_type"] == ["forecast"]  # precipitation is never an analysis
    era = service.cds_options(ERA, {"variable": ["2m_temperature"]})["options"]
    assert [o["name"] for o in era] == ["product_type"]


def test_an_option_chosen_for_another_kind_of_request_is_not_sent(cds):
    p = plan(spec(dataset=CERRA, variables=["2m_temperature"], end="2020-01-31",
                  options={"level_type": "surface_or_atmosphere", "data_type": ["reanalysis"],
                           "product_type": "analysis", "leadtime_hour": ["1"]}), limit=90000)
    assert "leadtime_hour" not in p.requests[0].inputs


# ------------------------------------------------------------------ the store, before any download

def test_the_store_is_previewed_from_the_plan(cds):
    from edh_downloader import service

    s = spec(variables=["2m_temperature", "total_precipitation"], time={"freq": "day"})
    r = service.plan(s.to_dict(), 8, True)
    st = r["struct"]
    assert r["rec"] and r["layout"] == r["rec"][0] and r["rows"]
    coords = {c["name"]: c for c in st["coords"]}
    assert coords["latitude"]["shape"] == (9,) and coords["longitude"]["shape"] == (9,)  # 47..45, 10..12 at 0.25°
    assert coords["valid_time"]["shape"] == (366,)
    data = {v["name"]: v for v in st["data"]}
    assert data["total_precipitation"]["stat"] == "sum" and data["2m_temperature"]["stat"] == "mean"
    assert any("GRIB" in n for n in st["notes"])  # names from the form until a file is read


def test_cerra_preview_shows_the_whole_projected_domain(cds):
    from edh_downloader import service

    s = spec(dataset=CERRA, variables=["2m_temperature"], end="2020-01-31",
             options={"level_type": "surface_or_atmosphere", "data_type": ["reanalysis"], "product_type": "analysis"})
    st = service.plan(s.to_dict(), 8, True)["struct"]
    v = st["data"][0]
    assert v["dims"] == ("valid_time", "y", "x") and v["shape"] == (31 * 8, 1069, 1069)
    assert any("projected" in n for n in st["notes"])
    from edh_downloader.gui import structure
    assert "<svg" in structure.cube_svg(v) and "y 1,069" in structure.cube_svg(v)


def test_preview_uses_what_an_earlier_job_learned(cds):
    import numpy as np

    from edh_downloader.sources.cds.grib import Grid
    from edh_downloader.sources.cds.output import CdsOutput

    s = spec(variables=["2m_temperature"], end="2020-01-31")
    p = plan(s)
    lat, lon = np.arange(47, 44.9, -0.25), np.arange(10, 12.1, 0.25)
    out = CdsOutput(p, Grid("regular", lat, lon), [])
    out.remember([{"param": 167, "name": "t2m", "short_name": "2t", "long_name": "2 metre temperature",
                   "units": "K", "step_type": "instant", "level_type": "surface", "form_name": "2m_temperature"}])
    prev = CdsOutput.preview(p)
    assert [vp.out_names[0] for vp in prev.vars] == ["t2m"] and prev.exact
