"""One damaged file must not take a whole screen down."""

import json

import pytest

from edh_downloader import keys, quota, runner, service
from edh_downloader.job import JobSpec


def test_one_unreadable_job_does_not_hide_the_others(home):
    good = runner.create_job(JobSpec(variables=["t2m"]))
    bad = runner.jobs_dir() / "bad"
    bad.mkdir()
    (bad / "job.json").write_text("[]")
    (runner.jobs_dir() / "bad" / "state.json").write_text("[]")
    ids = {j["id"] for j in service.jobs_list()}
    assert good.name in ids and "bad" in ids


@pytest.mark.parametrize("content", ["[]", "{}", '{"keys": null}', "null", "\x00\xff garbage"])
def test_damaged_keys_file_is_set_aside_not_fatal(home, content):
    (keys._path()).write_bytes(content.encode("latin1"))
    assert service.keys_list()["keys"] == []
    keys.add_key("n", "s")
    assert [k.name for k in keys.list_keys()] == ["n"]


@pytest.mark.parametrize("content", ["[]", "null", '{"fp": null}', '{"fp": {"2026-11": {}}}'])
def test_damaged_usage_file_is_tolerated(home, content):
    (quota.data_dir() / "usage.json").write_text(content)
    assert quota.info("fp")["used"] >= 0


def test_unknown_job_fields_from_another_version_load():
    s = JobSpec.from_dict({"future": 1, "time": {"start": "2021-01-01", "later": 2}})
    assert s.time.start == "2021-01-01"


def test_void_service_calls_report_success(home):
    keys.add_key("a", "s")
    assert service.key_set_quota("a", 5) == {"ok": True}
    with pytest.raises(ValueError):
        service.key_set_quota("a", 0)


def test_unreadable_job_cannot_be_resumed(home):
    bad = runner.jobs_dir() / "bad"
    bad.mkdir()
    (bad / "job.json").write_text("[]")
    (bad / "state.json").write_text(json.dumps({"status": "failed"}))
    st = runner.status(bad)
    assert st["status"] == "unreadable" and st["error"]
    with pytest.raises(RuntimeError):
        runner.launch(bad)
    service.job_delete("bad")
    assert not bad.exists()
