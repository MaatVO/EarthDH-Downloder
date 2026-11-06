"""The dataset-structure view: description, tree and drawing for grids, levels, points and sharding."""

from edh_downloader.job import AreaSpec, OutputSpec, Point, TimeSpec

from test_engine import spec


def _plan(fake_edh, home, **kw):
    from edh_downloader import catalog
    from edh_downloader.planner import Plan

    return Plan(spec(fake_edh, home, **kw), catalog.load_dataset(fake_edh.url, "test-key"))


def test_structure_grid_levels_sharded(home, fake_edh, tmp_path):
    from edh_downloader.gui import structure

    p = _plan(fake_edh, home, variables=["t2m", "t", "lsm"], levels=[1000, 500],
              time=TimeSpec("2019-11-03", "2019-12-20", "day", stats={"t2m": ["mean", "max"]}),
              output=OutputSpec(directory=str(tmp_path), name="x", sharding=True, keepbits=12))
    d = structure.describe(p, 0.5)
    names = [v["name"] for v in d["data"]]
    assert len(names) == len(set(names)) >= 4
    assert {c["name"] for c in d["coords"]} >= {"valid_time", "latitude", "longitude", p.ds.meta.level_dim}
    assert "bitround 12" in d["codecs"]
    lev = next(v for v in d["data"] if v["source"] == "t")
    assert lev["shards"] and lev["n_files"] <= lev["n_chunks"]
    html = structure.tree_html(d)
    assert all(n in html for n in names) and "x.zarr" in html
    for v in d["data"]:
        svg = structure.cube_svg(v)
        assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "levels" in structure.cube_svg(lev)


def test_structure_points(home, fake_edh):
    from edh_downloader.gui import structure

    p = _plan(fake_edh, home, area=AreaSpec("points", points=[Point("a", 1, 1), Point("b", 10, 20)]))
    d = structure.describe(p, 0.5)
    assert any(c["name"] == "point_name" for c in d["coords"])
    assert "points 2" in structure.cube_svg(d["data"][0])


def test_structure_from_written_store(home, fake_edh):
    """A completed job's store is described from disk: real chunks, codecs, sizes, both Zarr and Icechunk."""
    from edh_downloader.gui import structure
    from edh_downloader.runner import create_job, run_foreground

    s = spec(fake_edh, home / "out", variables=["t2m", "t", "lsm"], levels=[1000, 500],
             time=TimeSpec("2019-11-03", "2019-11-20", "day", stats={"t2m": ["mean", "max"]}),
             output=OutputSpec(directory=str(home / "out"), name="w", sharding=True, keepbits=12))
    assert run_foreground(create_job(s)) == "completed"
    z = structure.from_store(s.zarr_path)
    names = {v["name"] for v in z["data"]}
    assert {"t", "lsm"} <= names and len(names) >= 4
    assert {c["name"] for c in z["coords"]} >= {"valid_time", "latitude", "longitude"}
    assert z["measured"] and z["disk"] > 0 and all(v["disk"] > 0 for v in z["data"])
    assert "bitround 12" in z["codecs"] and any(v["shards"] for v in z["data"])
    assert "daily" in next(c for c in z["coords"] if c["name"] == "valid_time")["detail"]
    for v in z["data"]:
        assert "<svg" in structure.cube_svg(v)
    short = structure.tree_html(z, max_vars=2)
    assert "more variable" in short and short.count("st-var") == 2
    ic = structure.from_store(s.icechunk_path)
    assert ic["format"] == "Icechunk" and {v["name"] for v in ic["data"]} == names


def test_launcher_scripts():
    from edh_downloader import launcher

    sh = launcher.script("sh", "me@host", "/opt/edh-dl")
    assert 'HOST="${1:-me@host}"' in sh and 'CMD="/opt/edh-dl"' in sh and "serve --stdio" in sh
    ps = launcher.script("ps1", "me@host", "/opt/edh-dl")
    assert '$Server = "me@host"' in ps and "serve --stdio" in ps
