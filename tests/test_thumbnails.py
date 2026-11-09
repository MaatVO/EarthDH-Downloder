"""Catalogue images: read from STAC, kept in the cache, and safe to place in the option template."""

import html
import json
import time

import pytest

from edh_downloader import catalog
from edh_downloader.gui.app import option_template
from edh_downloader.util import write_json_atomic

PNG = "https://static.example.org/era5/thumb.png"


@pytest.mark.parametrize("stac_object, expected", [
    ({"assets": {"thumbnail": {"href": PNG}}}, PNG),
    ({"assets": {"thumbnail": {"href": "http://static.example.org/t.png"}}}, None),
    ({"assets": {"thumbnail": {"href": "javascript:alert(1)"}}}, None),
    ({"assets": {"thumbnail": {}}}, None),
    ({"assets": {"data": {"href": PNG}}}, None),
    ({"assets": None}, None),
    ({}, None),
])
def test_thumbnail_only_accepts_https_images(stac_object, expected):
    assert catalog._thumbnail(stac_object) == expected


def test_items_carry_their_thumbnail(monkeypatch):
    feature = {"id": "d", "properties": {}, "assets": {"data": {"href": "https://x/d.zarr/"}, "thumbnail": {"href": PNG}}}

    async def fake_get(_session, _url):
        return {"features": [feature], "links": []}

    monkeypatch.setattr(catalog, "_stac_get", fake_get)
    import asyncio
    assert asyncio.run(catalog.fetch_items("era5"))[0]["thumbnail"] == PNG


def _old_cache(name):
    write_json_atomic(catalog.cache_dir() / name, {"t": time.time(), "v": [{"id": "d", "title": "old"}]})


def test_cache_from_before_thumbnails_is_fetched_again(home, monkeypatch):
    _old_cache("stac_collections.json")

    async def fresh():
        return [{"id": "d", "title": "new", "thumbnail": PNG}]

    monkeypatch.setattr(catalog, "fetch_collections", fresh)
    assert catalog.collections()[0]["thumbnail"] == PNG
    monkeypatch.setattr(catalog, "fetch_collections", None)  # now answered from the upgraded cache
    assert catalog.collections()[0]["title"] == "new"


def test_cache_from_before_thumbnails_still_serves_offline(home, monkeypatch):
    _old_cache("stac_collections.json")

    async def offline():
        raise OSError("no route to host")

    monkeypatch.setattr(catalog, "fetch_collections", offline)
    assert catalog.collections()[0]["title"] == "old"


def test_option_template_cannot_be_broken_out_of():
    hostile = 'https://x/a.png"><script>alert(1)</script>'
    template = option_template([hostile, None])
    assert "<script>" not in template
    assert html.escape(json.dumps([hostile, None])) in template
