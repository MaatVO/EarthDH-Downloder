"""Test fixtures: an isolated app home and a fake Earth Data Hub serving a synthetic ERA5-like Zarr."""

import asyncio
import threading

import numpy as np
import pytest
import zarr
from aiohttp import web
from zarr.codecs.numcodecs import BitRound
from zarr.codecs import BloscCodec

NT, NLAT, NLON, NLEV = 24 * 50, 21, 40, 3  # 50 days hourly, 1-degree-ish grid
TC, CY, CX = 240, 6, 8


def make_store(path):
    g = zarr.open_group(str(path), mode="w", zarr_format=3, attributes={"institution": "test"})
    lat = np.linspace(90, -90, NLAT)
    lon = np.arange(NLON) * (360 / NLON)
    t = np.arange(NT, dtype="i8") + 700000  # hours since 1940 (~2019-11)

    def coord(name, vals, attrs):
        a = g.create_array(name, shape=vals.shape, dtype=vals.dtype, chunks=vals.shape,
                           dimension_names=[name], attributes=attrs, compressors=[zarr.codecs.ZstdCodec(level=0)])
        a[:] = vals

    coord("valid_time", t, {"units": "hours since 1940-01-01 00:00:00", "calendar": "proleptic_gregorian"})
    coord("latitude", lat, {"units": "degrees_north"})
    coord("longitude", lon, {"units": "degrees_east"})
    coord("isobaricInhPa", np.array([1000, 850, 500], dtype="i4"), {})
    rng = np.random.default_rng(0)
    data = {}
    comp = dict(compressors=[BloscCodec(cname="zstd", clevel=5, shuffle="shuffle", typesize=4)],
                filters=[BitRound(keepbits=13)], fill_value=np.nan, dtype="f4")
    for name, step in (("t2m", "instant"), ("tp", "accum")):
        a = g.create_array(name, shape=(NT, NLAT, NLON), chunks=(TC, CY, CX),
                           dimension_names=["valid_time", "latitude", "longitude"],
                           attributes={"long_name": name, "units": "K", "GRIB_stepType": step}, **comp)
        v = (rng.standard_normal((NT, NLAT, NLON)) + np.arange(NT)[:, None, None] / 100).astype("f4")
        a[:] = v
        data[name] = a[:]
    a = g.create_array("t", shape=(NT, NLEV, NLAT, NLON), chunks=(TC, 1, CY, CX),
                       dimension_names=["valid_time", "isobaricInhPa", "latitude", "longitude"],
                       attributes={"long_name": "temperature", "units": "K"}, **comp)
    a[:] = rng.standard_normal((NT, NLEV, NLAT, NLON)).astype("f4")
    data["t"] = a[:]
    a = g.create_array("lsm", shape=(NLAT, NLON), chunks=(CY, CX), dimension_names=["latitude", "longitude"],
                       attributes={"long_name": "land sea mask"}, **comp)
    a[:] = rng.random((NLAT, NLON)).astype("f4")
    data["lsm"] = a[:]
    zarr.consolidate_metadata(str(path))
    return data, lat, lon, t


class FakeEDH:
    def __init__(self, root):
        self.root = root
        self.requests = []
        self.url = None

    def start(self):
        ready = threading.Event()

        async def handler(request):
            self.requests.append(request.path)
            if request.headers.get("Authorization") != "Bearer test-key":
                return web.Response(status=401)
            p = self.root / request.match_info["tail"]
            if not p.is_file():
                return web.Response(status=404)
            return web.Response(body=p.read_bytes())

        def run():
            loop = asyncio.new_event_loop()
            app = web.Application()
            app.router.add_get("/ds.zarr/{tail:.*}", handler)
            runner = web.AppRunner(app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            port = site._server.sockets[0].getsockname()[1]
            self.url = f"http://127.0.0.1:{port}/ds.zarr"
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        return self


@pytest.fixture
def home(tmp_path, monkeypatch):
    for v, d in (("EDH_DL_CONFIG_DIR", "cfg"), ("EDH_DL_DATA_DIR", "data"), ("EDH_DL_CACHE_DIR", "cache")):
        monkeypatch.setenv(v, str(tmp_path / d))
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))
    from edh_downloader import keys

    keys.add_key("test", "test-key", quota=100_000)
    return tmp_path


@pytest.fixture(scope="session")
def fake_edh(tmp_path_factory):
    root = tmp_path_factory.mktemp("edh") / "ds.zarr"
    data, lat, lon, t = make_store(root)
    srv = FakeEDH(root).start()
    srv.data, srv.lat, srv.lon, srv.t = data, lat, lon, t
    return srv
