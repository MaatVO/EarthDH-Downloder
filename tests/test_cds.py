"""Climate Data Store client and catalogue, against the fake CDS (tests/fake_cds.py)."""

import asyncio
import json

import pytest
from fake_cds import TOKEN, _canon

from edh_downloader import keys
from edh_downloader.sources.cds import catalog
from edh_downloader.sources.cds.client import (AuthError, CDSClient, JobFailed, NoSuchJob, ResultsExpired,
                                               Throttled, canonical, request_hash)

ERA = "reanalysis-era5-single-levels"
REQ = {"product_type": ["reanalysis"], "variable": ["2m_temperature"], "year": ["2020"], "month": ["01"],
       "day": ["01"], "time": ["12:00"], "data_format": "grib", "area": [47, 10, 45, 12]}


def run(coro):
    return asyncio.run(coro)


def client(**kw):
    return CDSClient(TOKEN, backoff=0.01, max_backoff=0.05, **kw)


async def wait_done(c, jid, timeout=5.0):
    t = 0.0
    while t < timeout:
        j = (await c.statuses([jid]))[jid]
        if j["status"] in ("successful", "failed"):
            return j
        await asyncio.sleep(0.02)
        t += 0.02
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------ requests

def test_canonical_requests_are_order_independent():
    a = dict(REQ, variable=["b", "a", "a"], day=["02", "01"], year=2020)
    b = {k: a[k] for k in reversed(list(a))}
    b["variable"] = ["a", "b"]
    assert canonical(a) == canonical(b)
    assert canonical(a)["variable"] == ["a", "b"] and canonical(a)["year"] == "2020"
    assert canonical(a)["area"] == ["47", "10", "45", "12"]  # positional: never sorted
    assert request_hash(ERA, a) == request_hash(ERA, b) != request_hash("other", a)
    assert "empty" not in canonical(dict(a, empty=[]))


# ------------------------------------------------------------------ account and free checks

def test_key_check_uses_the_cds(cds):
    assert run(keys.validate(TOKEN, "cds"))[0]
    ok, msg = run(keys.validate("wrong", "cds"))
    assert not ok and "rejected" in msg


def test_costing_and_licences(cds):
    async def go():
        async with client() as c:
            month = dict(REQ, day=[f"{d:02d}" for d in range(1, 32)], time=[f"{h:02d}:00" for h in range(24)])
            return (await c.costing(ERA, REQ), await c.costing(ERA, month),
                    await c.costing("reanalysis-era5-pressure-levels", REQ), await c.licences())
    one, month, pl, lic = run(go())
    assert one == {"cost": 1.0, "limit": 121000.0}
    assert month["cost"] == 31 * 24
    assert pl["limit"] == 60000.0
    assert ("cc-by", 1) in lic


def test_bad_token_is_fatal_at_once(cds):
    async def go():
        async with CDSClient("wrong", backoff=0.01) as c:
            await c.costing(ERA, REQ)
    with pytest.raises(AuthError):
        run(go())
    assert len([x for x in cds.calls if "costing" in x[1]]) == 1


def test_server_errors_are_retried(cds):
    cds.fail_next_api_calls = 2

    async def go():
        async with client() as c:
            return await c.costing(ERA, REQ)
    assert run(go())["cost"] == 1.0


# ------------------------------------------------------------------ jobs

def test_submit_poll_download(cds, tmp_path):
    async def go():
        async with client() as c:
            j = await c.submit(ERA, REQ)
            assert j["status"] == "accepted"
            assert (await wait_done(c, j["jobID"]))["status"] == "successful"
            res = await c.results(j["jobID"])
            n = await c.download(res["href"], tmp_path / "a.grib", res["file:size"])
            echo = await c.job(j["jobID"], request=True)
            return n, res, echo
    n, res, echo = run(go())
    assert n == res["file:size"] == (tmp_path / "a.grib").stat().st_size
    assert not (tmp_path / "a.grib.part").exists()
    assert echo["metadata"]["request"]["ids"]["variable"] == ["2m_temperature"]


def test_identical_request_is_served_from_the_cache(cds):
    async def go():
        async with client() as c:
            j = await c.submit(ERA, REQ)
            await wait_done(c, j["jobID"])
            again = await c.submit(ERA, dict(REQ, variable=["2m_temperature", "2m_temperature"]))
            return j, again
    j, again = run(go())
    assert again["status"] == "successful" and again["jobID"] != j["jobID"]


def test_many_statuses_with_few_calls(cds):
    async def go():
        async with client() as c:
            ids = [(await c.submit(ERA, dict(REQ, day=[f"{d:02d}"])))["jobID"] for d in range(1, 26)]
            before = c.calls
            st = await c.statuses(ids, limit=10)
            return ids, st, c.calls - before
    ids, st, calls = run(go())
    assert set(st) == set(ids)
    assert calls == 3  # three pages of the job list, no call per job


def test_queue_limit_on_submit_raises_at_once(cds):
    cds.max_active = 1
    cds.queue_delay = 5

    async def go():
        async with client() as c:
            await c.submit(ERA, REQ)
            await c.submit(ERA, dict(REQ, day=["02"]))
    with pytest.raises(Throttled):
        run(go())
    assert cds.submissions == 1  # the refused submission was not repeated by the client


def test_queue_limit_as_a_failed_job(cds):
    cds.max_active, cds.queue_delay, cds.throttle_mode = 1, 5, "job"

    async def go():
        async with client() as c:
            await c.submit(ERA, REQ)
            j = await c.submit(ERA, dict(REQ, day=["02"]))
            assert (await c.statuses([j["jobID"]]))[j["jobID"]]["status"] == "failed"
            with pytest.raises(JobFailed) as e:
                await c.results(j["jobID"])
            return e.value, await c.failure(j["jobID"])
    err, why = run(go())
    assert err.throttled and "temporarily limited" in why


def test_failed_job_is_not_throttling(cds):
    cds.fail_requests.add(_canon(REQ))

    async def go():
        async with client() as c:
            j = await c.submit(ERA, REQ)
            await wait_done(c, j["jobID"])
            with pytest.raises(JobFailed) as e:
                await c.results(j["jobID"])
            return e.value
    err = run(go())
    assert not err.throttled and "no data" in str(err)


def test_expired_results(cds, tmp_path):
    async def go():
        async with client() as c:
            j = await c.submit(ERA, REQ)
            await wait_done(c, j["jobID"])
            res = await c.results(j["jobID"])
            cds.expire_results = True
            with pytest.raises(ResultsExpired):
                await c.results(j["jobID"])
            with pytest.raises(ResultsExpired):
                await c.download(res["href"], tmp_path / "x.grib", res["file:size"])
    run(go())


def test_download_resumes_after_a_broken_connection(cds, tmp_path):
    cds.file_size = 300_000

    async def go():
        async with client() as c:
            j = await c.submit(ERA, REQ)
            await wait_done(c, j["jobID"])
            res = await c.results(j["jobID"])
            cds.truncate_next_download = True
            return res, await c.download(res["href"], tmp_path / "a.grib", res["file:size"], block=4096)
    res, n = run(go())
    assert n == res["file:size"] == 300_000
    assert cds.ranges[0] is None and cds.ranges[-1].startswith("bytes=") and cds.ranges[-1] != "bytes=0-"


def test_delete(cds):
    async def go():
        async with client() as c:
            j = await c.submit(ERA, REQ)
            await c.delete(j["jobID"])
            await c.delete(j["jobID"])  # twice is fine
            with pytest.raises(NoSuchJob):
                await c.job(j["jobID"])
            return await c.statuses([j["jobID"]])
    assert run(go()) == {}


# ------------------------------------------------------------------ catalogue

def test_datasets_and_categories(cds):
    ds = catalog.datasets()
    assert len(ds) == 7 and ds == sorted(ds, key=lambda x: x["id"])
    era = catalog.find(ERA)
    assert era["category"] == "Reanalysis" and era["start"].startswith("1940") and era["thumbnail"]
    cats = {c["id"]: c["count"] for c in catalog.categories()}
    assert cats["Reanalysis"] == 4 and "In-situ observations" in cats


def test_catalogue_is_cached(cds, monkeypatch):
    catalog.describe(ERA)
    n = len(cds.calls)
    catalog.describe(ERA)
    catalog.constraints(ERA)
    catalog.constraints(ERA)
    assert len(cds.calls) == n + 1  # only the first constraints read
    monkeypatch.setenv("EDH_DL_OFFLINE", "1")
    monkeypatch.setenv("EDH_DL_CDS_API", "http://127.0.0.1:9")  # nothing there
    assert catalog.describe(ERA)["tier"] == "grib"


def test_describe_era5_single_levels(cds):
    d = catalog.describe(ERA)
    assert d["tier"] == "grib" and d["formats"] == ["grib", "netcdf"] and d["download_format"]
    assert {"name": "2m_temperature", "label": "2m temperature"} in d["variables"]
    assert d["levels"] is None
    assert d["time"]["kind"] == "ymd" and "12:00" in d["time"]["times"] and "2020" in d["time"]["years"]
    assert d["area"]["step"] == [0.25, 0.25]
    assert [l["id"] for l in d["licences"]] == ["cc-by"]
    opts = {o["name"]: o for o in d["options"]}
    assert "product_type" in opts and "reanalysis" in opts["product_type"]["values"]
    assert "variable" not in opts and "year" not in opts


def test_describe_pressure_levels_and_cerra(cds):
    pl = catalog.describe("reanalysis-era5-pressure-levels")
    assert pl["levels"]["field"] == "pressure_level" and "500" in pl["levels"]["values"]
    cerra = catalog.describe("reanalysis-cerra-single-levels")
    assert cerra["tier"] == "grib" and cerra["area"] is None  # CDS sends the whole domain
    names = {o["name"] for o in cerra["options"]}
    assert {"level_type", "product_type", "data_type"} <= names and "leadtime_hour" in names


def test_describe_other_tiers(cds):
    assert catalog.describe("reanalysis-era5-single-levels-timeseries")["tier"] == "timeseries"
    assert catalog.describe("reanalysis-era5-single-levels-timeseries")["location"]["range"]
    assert catalog.describe("insitu-observations-surface-land")["tier"] == "table"
    assert catalog.describe("satellite-sea-ice-concentration")["tier"] == "netcdf"
    cmip = catalog.describe("projections-cmip6")
    assert cmip["tier"] == "netcdf"
    assert all(o["values"] or o["groups"] for o in cmip["options"])
    assert json.loads(json.dumps(cmip)) == cmip


def test_local_constraints(cds):
    table = catalog.constraints(ERA)
    ok = dict(REQ)
    assert catalog.invalid(table, ok) == {}
    assert catalog.invalid(table, dict(ok, variable=["2m_temperature", "nope"])) == {"variable": ["nope"]}
    assert catalog.invalid(table, dict(ok, year=["1900"])) == {"year": ["1900"]}
    allowed = catalog.allowed(table, {"product_type": ["reanalysis"]})
    assert "2m_temperature" in allowed["variable"] and "reanalysis" in allowed["product_type"]
    # same answer as the server's constraints endpoint
    async def server():
        async with client() as c:
            return await c.constraints(ERA, {"product_type": ["reanalysis"], "variable": ["2m_temperature"]})
    srv = run(server())
    loc = catalog.allowed(table, {"product_type": ["reanalysis"], "variable": ["2m_temperature"]})
    assert {k: sorted(v) for k, v in srv.items()} == loc


# ------------------------------------------------------------------ service (what the UI calls)

def test_service_browses_the_cds_catalogue(cds):
    from edh_downloader import service

    cats = service.collections(source="cds")
    assert cats[0]["id"] == "Reanalysis"
    rows = service.datasets("Reanalysis", source="cds")
    assert {r["id"] for r in rows} >= {ERA, "reanalysis-cerra-single-levels"}
    assert all(r["collection"] == "Reanalysis" and r["deprecated"] is False for r in rows)


def test_service_describes_with_missing_licences(cds):
    from edh_downloader import service

    d = service.cds_describe(ERA, key="cds-test")
    assert d["licences_missing"] == []
    cds.licences = []
    d = service.cds_describe(ERA, key="cds-test")
    assert [l["id"] for l in d["licences_missing"]] == ["cc-by"]


def test_service_checks_requests_locally(cds):
    from edh_downloader import service

    n = len(cds.calls)
    r = service.cds_check(ERA, dict(REQ, variable=["nope"]))
    assert r["invalid"] == {"variable": ["nope"]}
    service.cds_check(ERA, REQ)
    assert len([c for c in cds.calls[n:] if c[0] == "POST"]) == 0  # no request to CDS for checking


def test_dataset_without_a_form(cds):
    with pytest.raises(ValueError, match="no download form"):
        catalog.describe("provider-c3s-data-rescue-without")
