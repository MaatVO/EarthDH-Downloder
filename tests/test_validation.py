"""Inputs that must be refused with a clear message (not accepted and silently turned into something else)."""

import pytest

from edh_downloader import catalog, keys, runner
from edh_downloader.job import AreaSpec, DownloadSpec, JobSpec, OutputSpec, TimeSpec
from edh_downloader.planner import Plan, PlanError


def _plan(home, fake, **kw):
    ds = catalog.load_dataset(fake.url, keys.get_key("test").secret)
    base = dict(dataset="fake", dataset_url=fake.url, variables=["t2m"], time=TimeSpec("2019-11-10", "2019-11-20"),
                area=AreaSpec("global"), output=OutputSpec(directory=str(home / "o"), name="v"),
                download=DownloadSpec(key="test"))
    base.update(kw)
    return Plan(JobSpec(**base), ds)


@pytest.mark.parametrize("t", [
    TimeSpec("2019-11-10", "2019-11-20", start_hour=30),
    TimeSpec("2019-11-10", "2019-11-20", start_hour=-1),
    TimeSpec("2019-11-10", "2019-11-20", end_hour=24),
    TimeSpec("2019-11-10", "2019-11-20", tz_offset=48),
])
def test_time_fields_out_of_range_are_refused(home, fake_edh, t):
    with pytest.raises(PlanError):
        _plan(home, fake_edh, time=t)


@pytest.mark.parametrize("name", ["", "  ", "a/b", "..", ".", "x\0y"])
def test_output_name_must_be_a_plain_file_name(home, fake_edh, name):
    from edh_downloader import service

    s = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m"], time=TimeSpec("2019-11-10", "2019-11-20"),
                output=OutputSpec(directory=str(home / "o"), name=name), download=DownloadSpec(key="test"))
    with pytest.raises(ValueError):
        service.job_start(s.to_dict())
    assert not (home / "o").exists() or not any((home / "o").iterdir())


@pytest.mark.parametrize("conn", [0, -3])
def test_nonpositive_connections_do_not_stall_the_job(home, fake_edh, conn):
    from test_faults import _run_bounded

    s = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m"], time=TimeSpec("2019-11-10", "2019-11-12"),
                output=OutputSpec(directory=str(home / "o"), name="c"),
                download=DownloadSpec(key="test", connections=conn, min_free_gb=0.1))
    assert _run_bounded(runner.create_job(s), timeout=40) == "completed"
