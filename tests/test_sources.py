"""Data sources: keys per source, ~/.cdsapirc, job specs with a source, and what this version refuses."""

import json

import pytest

from edh_downloader import keys, quota, service, sources
from edh_downloader.job import JobSpec


@pytest.fixture
def no_network(monkeypatch):
    """Key checks answer without the internet; records which source was asked."""
    asked = []

    async def fake(secret, provider="edh"):
        asked.append(provider)
        return secret != "bad", "ok" if secret != "bad" else "rejected"

    monkeypatch.setattr(keys, "validate", fake)
    return asked


def test_old_keys_file_is_earth_data_hub(home):
    keys._path().write_text(json.dumps({"default": "a", "keys": {"a": {"secret": "s1", "quota": 7},
                                                                 "b": {"secret": "s2"}}}))
    assert [(k.name, k.provider) for k in keys.list_keys()] == [("a", "edh"), ("b", "edh")]
    assert keys.get_key().name == "a" and keys.get_key().quota == 7
    assert keys.list_keys("cds") == []
    assert keys.default_name("cds") is None


def test_each_source_has_its_own_default(home):
    keys.add_key("c1", "tok1", provider="cds")
    keys.add_key("c2", "tok2", provider="cds")
    assert keys.default_name("edh") == "test"  # the fixture's key is untouched
    assert keys.default_name("cds") == "c1"
    keys.set_default("c2")
    assert (keys.default_name("edh"), keys.default_name("cds")) == ("test", "c2")
    keys.remove_key("c2")
    assert keys.default_name("cds") == "c1"
    assert keys.default_name("edh") == "test"
    # older versions read "default": it stays the Earth Data Hub key
    assert json.loads(keys._path().read_text())["default"] == "test"


def test_a_key_of_another_source_is_refused(home):
    keys.add_key("cds-key", "tok", provider="cds")
    with pytest.raises(KeyError, match="Climate Data Store key"):
        keys.get_key("cds-key")  # an Earth Data Hub job naming a CDS key
    with pytest.raises(KeyError, match="Earth Data Hub key"):
        keys.get_key("test", "cds")
    with pytest.raises(ValueError, match="already called"):
        keys.add_key("test", "other", provider="cds")


def test_cds_keys_have_no_quota(home):
    k = keys.add_key("c", "tok", quota=123, provider="cds")
    assert k.quota is None and keys.get_key("c", "cds").quota is None
    with pytest.raises(ValueError, match="no monthly request quota"):
        keys.set_quota("c", 10)
    row = next(r for r in service.keys_list()["keys"] if r["name"] == "c")
    assert row["provider"] == "cds" and row["usage"] is None and row["quota"] is None


def test_cdsapirc_is_read(home, monkeypatch):
    rc = home / "cdsapirc"
    rc.write_text("url: https://cds.climate.copernicus.eu/api\nkey: abc-123\n")
    monkeypatch.setenv("CDSAPI_RC", str(rc))
    k = keys.get_key(provider="cds")
    assert (k.name, k.secret, k.source, k.provider) == ("cdsapirc", "abc-123", "cdsapirc", "cds")
    assert all(k.provider == "edh" for k in keys.list_keys("edh"))
    with pytest.raises(ValueError, match="reserved"):
        keys.add_key("cdsapirc", "x", provider="cds")


def test_cdsapirc_for_another_store_is_ignored(home, monkeypatch):
    rc = home / "cdsapirc"
    rc.write_text("url: https://ads.atmosphere.copernicus.eu/api\nkey: abc\n")
    monkeypatch.setenv("CDSAPI_RC", str(rc))
    assert keys.list_keys("cds") == []


def test_cds_environment_variables(home, monkeypatch):
    monkeypatch.setenv("CDSAPI_URL", "https://cds.climate.copernicus.eu/api")
    monkeypatch.setenv("CDSAPI_KEY", "from-env")
    assert keys.get_key(provider="cds").secret == "from-env"


def test_service_adds_and_tests_cds_keys_without_counting_quota(home, no_network):
    r = service.key_add("c", "tok", None, check=True, provider="cds")
    assert r == {"ok": True, "name": "c"}
    assert service.key_test("c")["ok"]
    assert no_network == ["cds", "cds"]
    assert quota.used(keys.fingerprint("tok")) == 0
    assert service.key_add("d", "bad", None, check=True, provider="cds")["ok"] is False
    assert service.key_add("e", "tok", None, provider="nope")["ok"] is False
    d = service.keys_list()
    assert d["default"] == "test" and d["defaults"] == {"edh": "test", "cds": "c"}


def test_service_still_counts_the_earth_data_hub_check(home, no_network):
    assert service.key_add("e", "edh-secret", 1000, check=True)["ok"]
    assert no_network == ["edh"]
    assert quota.used(keys.fingerprint("edh-secret")) == 1


def test_jobs_without_a_source_are_earth_data_hub():
    s = JobSpec.from_dict({"dataset": "x", "variables": ["t2m"]})
    assert s.source == "edh" and s.cds.grid is None and s.cds.max_inflight == 4
    s2 = JobSpec.from_dict(dict(s.to_dict(), source="cds", cds={"grid": [1.0, 1.0], "options": {"a": 1},
                                                               "future_field": 3}))
    assert (s2.source, s2.cds.grid, s2.cds.options) == ("cds", [1.0, 1.0], {"a": 1})


def test_a_cds_job_needs_a_cds_key(home):
    spec = JobSpec(source="cds", variables=["2m_temperature"], dataset="reanalysis-era5-single-levels")
    spec.output.directory = str(home / "out")
    with pytest.raises(ValueError, match="no Copernicus Climate Data Store API key"):
        service.job_start(spec.to_dict())
    spec.download.key = "test"  # an Earth Data Hub key
    with pytest.raises(ValueError, match="Earth Data Hub key"):
        service.job_start(spec.to_dict())
    with pytest.raises(ValueError, match="unknown data source"):
        sources.get("nope")


def test_an_earth_data_hub_job_with_a_cds_key_is_refused(home):
    keys.add_key("c", "tok", provider="cds")
    spec = JobSpec(variables=["t2m"])
    spec.output.directory = str(home / "out")
    spec.download.key = "c"
    with pytest.raises(ValueError, match="Climate Data Store key"):
        service.job_start(spec.to_dict())


def test_sources_list_is_plain_data():
    rows = service.sources_list()
    assert [r["id"] for r in rows] == ["edh", "cds"]
    assert json.loads(json.dumps(rows)) == rows
