"""Climate Data Store client, keys and catalogue: the hardening rules (non-JSON answers, redirects and the token,
relative links, download retries, resume and checksums, key forms, unknown providers, option states, widgets).

The HTTP cases use small aiohttp servers of their own, so each case controls every status and header."""

import asyncio
import hashlib
import random

import pytest
from aiohttp import web

from edh_downloader import keys
from edh_downloader.sources import cds
from edh_downloader.sources.cds import catalog
from edh_downloader.sources.cds.client import MAX_REDIRECTS, CDSClient, CDSError, expected_md5, same_origin

TOKEN = "hardening-token"
BODY = bytes(range(256)) * 40  # 10240 bytes of result "file"


def run(coro):
    return asyncio.run(coro)


async def serve(routes):
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def file_handler(body: bytes, script=()):
    """Serves ``body``: the first requests get the statuses in ``script`` (status, headers), then the file, with
    ranges honoured. Records the Range header of every request."""
    seen = []

    async def handler(req):
        seen.append(req.headers.get("Range"))
        if len(seen) <= len(script):
            status, headers = script[len(seen) - 1]
            return web.Response(status=status, headers=headers, text="busy")
        rng = req.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            return web.Response(status=206, body=body[start:],
                                headers={"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"})
        return web.Response(body=body)
    return handler, seen


# ------------------------------------------------------------------ 1. answers that are not JSON

def test_non_json_success_is_an_error():
    async def go():
        async def page(req):
            return web.Response(text="<html>maintenance</html>", content_type="text/html")
        runner, base = await serve([web.get("/api/x", page)])
        try:
            async with CDSClient(TOKEN, api=f"{base}/api", backoff=0.01) as c:
                with pytest.raises(CDSError, match="not JSON"):
                    await c._call("GET", "/x")
        finally:
            await runner.cleanup()
    run(go())


def test_retry_after_zero_still_waits_a_second():
    c = CDSClient(TOKEN, api="http://127.0.0.1:9/api")
    assert c._wait(0, 0.0) >= 1.0
    assert c._wait(4, 0.0) >= 1.0


# ------------------------------------------------------------------ 2. the token and redirects

def test_token_is_not_sent_to_another_origin_on_redirect():
    async def go():
        seen = []

        async def elsewhere(req):
            seen.append(req.headers.get("PRIVATE-TOKEN"))
            return web.json_response({"where": "elsewhere"})

        other_runner, other = await serve([web.get("/elsewhere", elsewhere)])

        async def cross(req):
            raise web.HTTPFound(f"{other}/elsewhere")

        async def same(req):
            raise web.HTTPFound("/api/final")

        async def final(req):
            seen.append(req.headers.get("PRIVATE-TOKEN"))
            return web.json_response({"where": "final"})

        async def loop(req):
            raise web.HTTPFound("/api/loop")

        runner, base = await serve([web.get("/api/cross", cross), web.get("/api/same", same),
                                    web.get("/api/final", final), web.get("/api/loop", loop)])
        try:
            async with CDSClient(TOKEN, api=f"{base}/api", backoff=0.01) as c:
                assert (await c._call("GET", "/cross"))[1] == {"where": "elsewhere"}
                assert (await c._call("GET", "/same"))[1] == {"where": "final"}
                with pytest.raises(CDSError, match="too many redirects"):
                    await c._call("GET", "/loop")
        finally:
            await runner.cleanup()
            await other_runner.cleanup()
        # the other host got no token; the API's own origin got it
        assert seen == [None, TOKEN]
    run(go())


def test_validate_does_not_send_the_token_across_origins():
    async def go():
        seen = []

        async def elsewhere(req):
            seen.append(req.headers.get("PRIVATE-TOKEN"))
            return web.json_response({})

        other_runner, other = await serve([web.get("/elsewhere", elsewhere)])

        async def account(req):
            raise web.HTTPFound(f"{other}/elsewhere")

        runner, base = await serve([web.get("/api/profiles/v1/account", account)])
        try:
            await cds.validate(TOKEN, base=f"{base}/api")
        finally:
            await runner.cleanup()
            await other_runner.cleanup()
        assert seen == [None]
    run(go())


def test_same_origin():
    assert same_origin("https://cds.climate.copernicus.eu/api/x", "https://cds.climate.copernicus.eu/api")
    assert same_origin("https://a.example:443/x", "https://A.example/y")
    assert not same_origin("https://a.example/x", "http://a.example/x")
    assert not same_origin("https://a.example/x", "https://a.example:8443/x")
    assert not same_origin("https://a.example/x", "https://b.example/x")


# ------------------------------------------------------------------ 3. relative links and absolute paths

def test_relative_next_links_resolve_on_the_api_origin():
    async def go():
        paths = []

        async def jobs(req):
            paths.append(req.path_qs)
            limit = int(req.query.get("limit", 10))
            cursor = int(req.query.get("cursor", 0))
            ids = ["j2", "j1"]  # newest first
            links = []
            if cursor + limit < len(ids):
                links.append({"rel": "next", "href": f"/api/retrieve/v1/jobs?limit={limit}&cursor={cursor + limit}"})
            return web.json_response({"jobs": [{"jobID": i, "status": "successful"} for i in ids[cursor:cursor + limit]],
                                      "links": links})

        async def account(req):
            return web.json_response({"ok": True})

        runner, base = await serve([web.get("/api/retrieve/v1/jobs", jobs),
                                    web.get("/api/profiles/v1/account", account)])
        try:
            async with CDSClient(TOKEN, api=f"{base}/api", backoff=0.01) as c:
                found = await c.jobs(limit=1)
                # an absolute path from the origin reaches the same place as the API-relative one
                assert (await c._call("GET", "/api/profiles/v1/account"))[1] == {"ok": True}
        finally:
            await runner.cleanup()
        assert [j["jobID"] for j in found] == ["j2", "j1"]
        assert paths == ["/api/retrieve/v1/jobs?limit=1", "/api/retrieve/v1/jobs?limit=1&cursor=1"]
    run(go())


# ------------------------------------------------------------------ 4. downloads

def test_download_retries_5xx_and_429_honouring_retry_after(tmp_path):
    async def go():
        handler, seen = file_handler(BODY, script=[(503, {"Retry-After": "0"}), (429, {"Retry-After": "0"})])
        runner, base = await serve([web.get("/files/a.grib", handler)])
        try:
            async with CDSClient(TOKEN, api=f"{base}/api", backoff=0.01, max_backoff=0.05) as c:
                n = await c.download(f"{base}/files/a.grib", tmp_path / "a.grib", len(BODY))
        finally:
            await runner.cleanup()
        assert n == len(BODY) and (tmp_path / "a.grib").read_bytes() == BODY
        assert len(seen) == 3
    run(go())


def test_download_gives_up_after_retries(tmp_path):
    async def go():
        handler, _ = file_handler(BODY, script=[(503, {})] * 10)
        runner, base = await serve([web.get("/files/a.grib", handler)])
        try:
            async with CDSClient(TOKEN, backoff=0.01, max_backoff=0.02, retries=2) as c:
                with pytest.raises(CDSError, match="HTTP 503"):
                    await c.download(f"{base}/files/a.grib", tmp_path / "a.grib", len(BODY))
        finally:
            await runner.cleanup()
        assert not (tmp_path / "a.grib").exists()
    run(go())


def test_download_resumes_with_a_matching_content_range(tmp_path):
    async def go():
        handler, seen = file_handler(BODY)
        runner, base = await serve([web.get("/files/a.grib", handler)])
        (tmp_path / "a.grib.part").write_bytes(BODY[:1000])
        try:
            async with CDSClient(TOKEN, backoff=0.01) as c:
                n = await c.download(f"{base}/files/a.grib", tmp_path / "a.grib", len(BODY))
        finally:
            await runner.cleanup()
        assert n == len(BODY) and (tmp_path / "a.grib").read_bytes() == BODY
        assert seen == ["bytes=1000-"]
    run(go())


def test_download_starts_again_when_the_range_does_not_continue_the_file(tmp_path):
    async def go():
        seen = []

        async def wrong(req):  # answers a Range with a range from the start: not where the file stops
            seen.append(req.headers.get("Range"))
            if req.headers.get("Range"):
                return web.Response(status=206, body=BODY,
                                    headers={"Content-Range": f"bytes 0-{len(BODY) - 1}/{len(BODY)}"})
            return web.Response(body=BODY)

        runner, base = await serve([web.get("/files/a.grib", wrong)])
        (tmp_path / "a.grib.part").write_bytes(BODY[:1000])
        try:
            async with CDSClient(TOKEN, backoff=0.01) as c:
                await c.download(f"{base}/files/a.grib", tmp_path / "a.grib", len(BODY))
        finally:
            await runner.cleanup()
        assert (tmp_path / "a.grib").read_bytes() == BODY
        assert seen == ["bytes=1000-", None]
    run(go())


def _multipart(body: bytes, part: int) -> str:
    """The ETag hash an object store gives a multipart upload: the MD5 of the parts' MD5s."""
    parts = [hashlib.md5(body[i:i + part]).digest() for i in range(0, len(body), part)]
    return hashlib.md5(b"".join(parts)).hexdigest()


def test_download_accepts_the_multipart_etag_cds_publishes(tmp_path):
    """A large CDS result's ``file:checksum`` is the object store's multipart ETag without its ``-<parts>``
    suffix (a 1.5 GB ERA5 file: 30 parts of 50 MiB), not the file's MD5."""
    from edh_downloader.sources.cds.client import MIB, checksum_matches

    body = (bytes(range(256)) * 4096 * 4)[: 3 * MIB + 12345]  # 4 parts of 1 MiB
    want = _multipart(body, MIB)
    assert want != hashlib.md5(body).hexdigest()

    async def go():
        async def handler(req):
            return web.Response(body=body, headers={"ETag": f'"{want}-4"'})

        runner, base = await serve([web.get("/files/big.grib", handler)])
        try:
            async with CDSClient(TOKEN, backoff=0.01) as c:
                await c.download(f"{base}/files/big.grib", tmp_path / "big.grib", len(body), checksum=want)
                with pytest.raises(CDSError, match="checksum mismatch"):
                    await c.download(f"{base}/files/big.grib", tmp_path / "bad.grib", len(body),
                                     checksum=_multipart(body[:-1] + b"x", MIB))
        finally:
            await runner.cleanup()
        assert (tmp_path / "big.grib").read_bytes() == body
    run(go())
    f = tmp_path / "big.grib"
    assert checksum_matches(f, want, f'"{want}-4"')
    big = _multipart(f.read_bytes(), 8 * MIB)  # one part of a usual size: found without an ETag
    assert checksum_matches(f, big) and checksum_matches(f, hashlib.md5(body).hexdigest())
    assert not checksum_matches(f, "0" * 32, f'"{want}-4"')


def test_download_checksum_accepts_md5_and_rejects_a_mismatch(tmp_path):
    async def go():
        handler, _ = file_handler(BODY)
        runner, base = await serve([web.get("/files/a.grib", handler)])
        digest = hashlib.md5(BODY).hexdigest()
        try:
            async with CDSClient(TOKEN, backoff=0.01) as c:
                await c.download(f"{base}/files/a.grib", tmp_path / "ok.grib", len(BODY), checksum=f"md5:{digest}")
                await c.download(f"{base}/files/a.grib", tmp_path / "bare.grib", len(BODY), checksum=digest)
                # a format that is not known is not checked
                await c.download(f"{base}/files/a.grib", tmp_path / "other.grib", len(BODY), checksum="sha256:00")
                with pytest.raises(CDSError, match="checksum mismatch"):
                    await c.download(f"{base}/files/a.grib", tmp_path / "bad.grib", len(BODY),
                                     checksum="md5:" + "0" * 32)
        finally:
            await runner.cleanup()
        assert (tmp_path / "ok.grib").read_bytes() == BODY and (tmp_path / "bare.grib").exists()
        assert not (tmp_path / "bad.grib").exists() and not (tmp_path / "bad.grib.part").exists()
    run(go())


def test_expected_md5_formats():
    h = "a" * 32
    assert expected_md5(f"md5:{h}") == h and expected_md5(h.upper()) == h
    assert expected_md5("sha256:" + "a" * 64) is None
    assert expected_md5(None) is None and expected_md5("") is None


# ------------------------------------------------------------------ 5. the cdsapirc key forms

def test_cdsapi_key_alone_is_used(home, monkeypatch):
    monkeypatch.setenv("CDSAPI_KEY", "plain-key")
    assert keys.cdsapirc_secret() == "plain-key"


def test_legacy_uid_prefix_is_stripped_only_for_digits(home, monkeypatch):
    monkeypatch.setenv("CDSAPI_KEY", "12345:abcdef")
    assert keys.cdsapirc_secret() == "abcdef"
    monkeypatch.setenv("CDSAPI_KEY", "not-a-uid:abcdef")
    assert keys.cdsapirc_secret() == "not-a-uid:abcdef"


def test_key_for_another_store_is_not_used(home, monkeypatch):
    monkeypatch.setenv("CDSAPI_URL", "https://example.org/api")
    monkeypatch.setenv("CDSAPI_KEY", "k")
    assert keys.cdsapirc_secret() is None


# ------------------------------------------------------------------ 6. unknown providers in keys.json

def test_key_of_an_unknown_provider_is_listed_as_unknown(home):
    d = keys._load()
    d["keys"]["future"] = {"secret": "s3cret", "provider": "someday"}
    keys._save(d)
    listed = {k.name: k for k in keys.list_keys()}
    assert listed["future"].provider == "unknown"
    assert "test" in listed and listed["test"].provider == keys.EDH
    assert [k.name for k in keys.list_keys(provider=keys.CDS)] == []


# ------------------------------------------------------------------ 8. option states

def test_a_constrained_option_with_no_valid_value_shows_none():
    table = [{"A": "a9", "X": "x"}]
    desc = {"options": [{"name": "A", "values": ["a1", "a2"]}]}
    st = catalog.option_state(desc, table, {"X": ["x"]})
    assert [o["values"] for o in st["options"] if o["name"] == "A"] == [[]]
    assert "A" in catalog.invalid(table, {"X": ["x"], "A": ["a1"]})


def _random_case(rng: random.Random):
    names, vals = ["A", "B", "C", "D"], ["1", "2", "3"]
    table = []
    for _ in range(rng.randint(1, 5)):
        table.append({n: rng.choice(vals) for n in rng.sample(names, rng.randint(1, 3))})
    desc = {"options": [{"name": n, "values": vals + ["4"]} for n in names]}
    selection = {n: [rng.choice(vals + ["4"])] for n in rng.sample(names, rng.randint(0, 4))}
    return table, desc, selection


def test_option_state_terminates_and_never_presents_a_value_invalid_flags():
    rng = random.Random(7)
    for _ in range(300):
        table, desc, selection = _random_case(rng)
        st = catalog.option_state(desc, table, selection)  # must return: the hiding is monotone
        assert set(st["dropped"]) <= set(selection)
        kept = {k: v for k, v in selection.items() if k not in st["dropped"]}
        for o in st["options"]:
            for v in o["values"]:
                flagged = catalog.invalid(table, {**kept, o["name"]: [v]})
                assert o["name"] not in flagged, (table, selection, o["name"], v)


# ------------------------------------------------------------------ 9. licences and unsupported widgets

def test_describe_null_revision_and_unsupported_required_widgets(monkeypatch):
    widgets = [
        {"name": "variable", "type": "StringListWidget", "required": True,
         "details": {"values": ["t2m"], "labels": {"t2m": "2m temperature"}}},
        {"name": "licences", "type": "LicenceWidget", "required": True,
         "details": {"licences": [{"id": "cc-by", "revision": None, "label": "CC"},
                                  {"id": "lic", "revision": 2}]}},
        {"name": "mystery", "type": "MagicWidget", "required": True},
        {"name": "optional_magic", "type": "MagicWidget", "required": False},
    ]
    monkeypatch.setattr(catalog, "form", lambda dataset_id, refresh=False: widgets)

    def no_summary(dataset_id, refresh=False):
        raise ValueError("no catalogue entry in this test")
    monkeypatch.setattr(catalog, "find", no_summary)
    d = catalog.describe("ds")
    assert [x["revision"] for x in d["licences"]] == [0, 2]
    assert d["unsupported"] == ["mystery"]
    assert [v["name"] for v in d["variables"]] == ["t2m"]
