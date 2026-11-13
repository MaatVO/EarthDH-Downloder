import asyncio

from edh_downloader import backend, bundle
from edh_downloader.job import AreaSpec, JobSpec, TimeSpec


def test_plan_and_jobs_through_the_backend(home, fake_edh):
    be = backend.Local()

    async def go():
        info = {"id": "fake", "title": "Fake", "url": fake_edh.url}
        ds = await be.call("dataset_load", info=info, key="test")
        assert {v["name"] for v in ds["variables"]} >= {"t2m", "tp"}
        spec = JobSpec(dataset="fake", dataset_url=fake_edh.url, variables=["t2m"],
                       time=TimeSpec("2019-11-10", "2019-11-12", "day"), area=AreaSpec("global"))
        spec.output.directory = str(home / "out")
        spec.download.key = "test"
        res = await be.call("plan", spec=spec.to_dict(), connections=4, follow=True)
        assert res["ok"] and res["summary"]["periods"] == 3 and res["key"]["name"] == "test"
        assert res["struct"]["data"][0]["name"] == "t2m"
        bad = JobSpec(**{**spec.__dict__, "time": TimeSpec("2030-01-01", "2030-01-02", "day")})
        assert not (await be.call("plan", spec=bad.to_dict(), connections=4, follow=False))["ok"]
        job = await be.call("job_start", spec=spec.to_dict())
        assert any(j["id"] == job["id"] for j in await be.call("jobs_list"))
        try:
            await be.call("job_start", spec=spec.to_dict())  # output may exist, or the job may already run
        except backend.BackendError:
            pass

        # Inspect non-existent path
        r_miss = await be.call("store_inspect", path=str(home / "nonexistent.icechunk"))
        assert r_miss["ok"] is False

    asyncio.run(go())


def test_agent_bundle_is_small_and_deterministic():
    a, b = bundle.build(), bundle.build()
    assert a == b and len(a[0]) < 400_000


def test_list_dirs_for_the_folder_picker(tmp_path):
    from edh_downloader import service

    (tmp_path / "b_out").mkdir()
    (tmp_path / "A_out").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    r = service.list_dirs(str(tmp_path))
    assert r["path"] == str(tmp_path)
    assert r["dirs"] == ["A_out", "b_out"]  # folders only, no dotfolders, sorted ignoring case
    assert r["parent"] == str(tmp_path.parent)
    # a folder that does not exist yet opens at its nearest existing parent
    assert service.list_dirs(str(tmp_path / "new" / "deeper"))["path"] == str(tmp_path)
    assert service.list_dirs("")["path"] == r["home"]
    assert service.list_dirs("/")["parent"] is None
