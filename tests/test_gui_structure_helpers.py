"""Small helpers of the structure view: dimension names, escaping in the tree, coordinates that are empty or all NaN."""

import numpy as np


def test_short_maps_only_real_level_dims():
    from edh_downloader.gui.structure import _short

    assert _short("valid_time") == "time"
    assert _short("latitude") == "lat" and _short("longitude") == "lon"
    assert _short("y") == "y" and _short("point") == "point"
    for lev in ("isobaricInhPa", "pressure_level", "hybrid", "model_level", "heightAboveGround", "soil_level"):
        assert _short(lev) == "level", lev
    # an unlisted dimension keeps its name instead of becoming "level"
    assert _short("realization") == "realization"
    assert _short("number") == "number"


def _store(dims="d", dtype="i4", name="v"):
    var = dict(name=name, dims=[dims], shape=[3], chunks=[3], chunk_bytes=12, n_chunks=1, stat="none",
               long_name="", units="", shards=None, n_files=1, disk=None, raw=12, dtype=dtype, source=name)
    return dict(name="store.zarr", attrs=[], coords=[], data=[var], codecs="bytes", format="Zarr v3",
                measured=False)


def test_tree_html_escapes_dims_and_dtype():
    from edh_downloader.gui.structure import tree_html

    html = tree_html(_store(dims="<img src=x>", dtype="<script>"))
    assert "<img src=x>" not in html and "<script>" not in html
    assert "&lt;img src=x&gt;" in html and "&lt;script&gt;" in html


def test_tree_html_plain_store_still_renders():
    from edh_downloader.gui.structure import tree_html

    html = tree_html(_store())
    assert 'class="edh-tree"' in html and "v" in html


def test_coord_detail_empty_and_nan_coordinates():
    xr = __import__("xarray")
    from edh_downloader.gui.structure import _coord_detail

    assert _coord_detail(xr.Dataset({"empty": ("x", np.array([], dtype="float64"))}), "empty") == ""
    assert _coord_detail(xr.Dataset({"nan2d": (("y", "x"), np.full((2, 2), np.nan))}), "nan2d") == ""
    assert _coord_detail(xr.Dataset({"scalar": ((), 3.0)}), "scalar") == ""
    ok = xr.Dataset({"ok2d": (("y", "x"), np.array([[1.0, 2.0], [3.0, 4.0]]))})
    assert _coord_detail(ok, "ok2d") == "1° … 4°"
