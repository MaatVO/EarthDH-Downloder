"""Visual description of the Zarr store a plan will produce: a tree of arrays and a chunk-grid drawing."""

import html
import math

import numpy as np

from ..util import human_bytes

SHORT = {"valid_time": "time", "latitude": "lat", "longitude": "lon", "point": "point"}
MAX_LINES = 24  # grid lines drawn per axis; beyond that the grid is drawn sparse and labelled


def _short(d: str) -> str:
    return SHORT.get(d, "level" if d.startswith("isobaric") else d)


def _fmt_num(v) -> str:
    v = float(v)
    return f"{v:.2f}".rstrip("0").rstrip(".") if not v.is_integer() else f"{int(v)}"


def codecs_text(o) -> str:
    """Human description of the codec pipeline of the exported data arrays."""
    parts = []
    if o.keepbits is not None:
        parts.append(f"bitround {o.keepbits} bits")
    parts.append("bytes")
    if o.compressor == "none":
        pass
    elif o.compressor.startswith("blosc-"):
        parts.append(f"blosc {o.compressor.split('-', 1)[1]} L{o.clevel} {o.shuffle.replace('noshuffle', 'no shuffle')}")
    else:
        parts.append(f"{o.compressor} L{o.clevel}")
    return " → ".join(parts)


def describe(plan, ratio: float, ratios: dict | None = None) -> dict:
    """Everything the UI draws about the output store."""
    o = plan.spec.output
    item = 8 if o.dtype == "float64" else 4
    coords = []
    if plan.n_periods:
        lab = plan.labels
        step = (lab[1] - lab[0]) if len(lab) > 1 else None
        rng = f"{lab[0]:%Y-%m-%d %H:%M} … {lab[-1]:%Y-%m-%d %H:%M}"
        coords.append(dict(name="valid_time", dims=("valid_time",), shape=(len(lab),), dtype="int64",
                           detail=rng + (f" · {_step(plan)}" if step is not None else "")))
    if plan.has_levels:
        lv = plan.level_values
        coords.append(dict(name=plan.ds.meta.level_dim, dims=(plan.ds.meta.level_dim,), shape=(len(lv),),
                           dtype="int64", detail=", ".join(_fmt_num(v) for v in lv) + " hPa"))
    if plan.points is not None:
        n = len(plan.points)
        coords.append(dict(name="point", dims=("point",), shape=(n,), dtype="int32", detail="0 … " + str(n - 1)))
        coords.append(dict(name="point_name", dims=("point",), shape=(n,), dtype="str",
                           detail=", ".join(p.name for p in plan.points[:4]) + (" …" if n > 4 else "")))
        coords.append(dict(name="latitude", dims=("point",), shape=(n,), dtype="float64", detail="nearest cell"))
        coords.append(dict(name="longitude", dims=("point",), shape=(n,), dtype="float64", detail="nearest cell"))
        coords.append(dict(name="requested_latitude", dims=("point",), shape=(n,), dtype="float64", detail="as given"))
        coords.append(dict(name="requested_longitude", dims=("point",), shape=(n,), dtype="float64", detail="as given"))
    else:
        for nm, v in (("latitude", plan.out_lat), ("longitude", plan.out_lon)):
            v = np.asarray(v)
            res = abs(float(v[1] - v[0])) if len(v) > 1 else None
            coords.append(dict(name=nm, dims=(nm,), shape=v.shape, dtype="float64",
                               detail=f"{_fmt_num(v[0])}° … {_fmt_num(v[-1])}°" + (f" · {_fmt_num(res)}°" if res else "")))
    data = []
    for vp in plan.vars:
        dims, shape = plan.out_dims(vp), plan.out_shape(vp)
        chunks, shards = plan.export_chunks(vp), plan.export_shards(vp)
        n_chunks = math.prod(math.ceil(s / c) for s, c in zip(shape, chunks))
        n_files = math.prod(math.ceil(s / c) for s, c in zip(shape, shards)) if shards else n_chunks
        raw = math.prod(shape) * item
        a = vp.var.attrs
        for stat, name in zip(vp.stats, vp.out_names):
            data.append(dict(
                name=name, source=vp.var.name, stat=stat, kind=vp.var.kind,
                long_name=a.get("long_name", vp.var.name), units=a.get("units", ""),
                dims=dims, shape=shape, chunks=chunks, shards=shards, dtype=o.dtype,
                chunk_bytes=math.prod(chunks) * item, n_chunks=n_chunks, n_files=n_files,
                raw=raw, disk=raw * (ratios or {}).get(name, ratio)))
    attrs = ["title", "source", "Conventions", "license", "history", "edh_job (all settings)", "edh_plan",
             "edh_status"] + list((o.extra_attrs or {}).keys())
    return dict(name=f"{o.name}.zarr", coords=coords, data=data, attrs=attrs, codecs=codecs_text(o),
                raw=sum(d["raw"] for d in data), disk=sum(d["disk"] for d in data),
                files=sum(d["n_files"] for d in data))


def _step(plan) -> str:
    t = plan.spec.time
    return {"none": "hourly", "hours": f"{t.hours}-hourly", "day": "daily", "month": "monthly",
            "year": "yearly"}.get(t.freq, t.freq)


# ------------------------------------------------------------------ tree (HTML)

def tree_html(d: dict, max_vars: int | None = None) -> str:
    """The store as a tree. With ``max_vars`` only that many data variables are listed, plus a "… N more" row."""
    e = html.escape
    rows = [f'<div class="st-root"><span class="material-icons st-ic">folder</span>{e(d["name"])}'
            f'<span class="st-type">{d.get("format", "Zarr v3")}</span></div>']

    def item(br, cont, icon, cls, name, dims, dtype, detail):
        rows.append(f'<div class="st-row"><span class="st-br">{br}</span><span class="material-icons st-ic {cls}">'
                    f'{icon}</span><b>{e(name)}</b>' + (f'<span class="st-dims">{dims}</span>' if dims else "") +
                    (f'<span class="st-type">{dtype}</span>' if dtype else "") + '</div>')
        if detail:
            rows.append(f'<div class="st-row st-sub"><span class="st-br">{cont}</span><span class="st-detail">{detail}</span></div>')

    attrs = "".join(f'<span class="st-chip">{e(a)}</span>' for a in d["attrs"])
    item("├─", "│     ", "description", "st-attr", "zarr.json", "", "", "global attributes " + attrs)
    rows.append('<div class="st-row"><span class="st-br">├─</span><span class="st-group">coordinates</span></div>')
    for i, c in enumerate(d["coords"]):
        last = i == len(d["coords"]) - 1
        item("│  " + ("└─" if last else "├─"), "│  " + ("   " if last else "│  ") + "   ", "straighten", "st-coord",
             c["name"], f'({", ".join(_short(x) for x in c["dims"])}) {", ".join(f"{s:,}" for s in c["shape"])}',
             c["dtype"], e(c["detail"]))
    rows.append('<div class="st-row"><span class="st-br">└─</span><span class="st-group">data variables</span></div>')
    shown = d["data"] if max_vars is None else d["data"][:max_vars]
    hidden = len(d["data"]) - len(shown)
    for i, v in enumerate(shown):
        last = i == len(shown) - 1 and not hidden
        dims = " × ".join(f"{_short(x)} {s:,}" for x, s in zip(v["dims"], v["shape"]))
        stat = "" if v["stat"] in ("none", None) else f' · {e(v["stat"])}'
        ch = " × ".join(f"{c:,}" for c in v["chunks"])
        det = (f'{e(v["long_name"])}{" [" + e(v["units"]) + "]" if v["units"] else ""}{stat}<br>'
               f'chunk <b>{ch}</b> · {human_bytes(v["chunk_bytes"])} · {v["n_chunks"]:,} chunks')
        if v["shards"]:
            det += f'<br>shard <b>{" × ".join(f"{c:,}" for c in v["shards"])}</b> · {v["n_files"]:,} files'
        if v["disk"] is not None:
            det += (f'<br>{human_bytes(v["raw"])} raw → <b>{"" if d.get("measured") else "≈ "}'
                    f'{human_bytes(v["disk"])}</b> on disk')
        else:
            det += f'<br>{human_bytes(v["raw"])} raw'
        item("   " + ("└─" if last else "├─"), "   " + ("   " if last else "│  ") + "   ", "grid_on", "st-var",
             v["name"], f"({dims})", v["dtype"], det)
    if hidden:
        rows.append(f'<div class="st-row st-more"><span class="st-br">   └─</span>… {hidden:,} more '
                    f'variable{"s" if hidden != 1 else ""}</div>')
    rows.append(f'<div class="st-row st-sub st-codecs"><span class="material-icons st-ic st-attr">compress</span>'
                f'codecs <b>{e(d["codecs"])}</b></div>')
    return '<div class="edh-tree">' + "".join(rows) + "</div>"


# ------------------------------------------------------------------ chunk grid drawing (SVG)

def _axis(n: int, c: int, length: float):
    """Positions of chunk boundaries along an axis drawn ``length`` px long, plus the first chunk's length.
    Beyond MAX_LINES chunks the boundaries are drawn evenly as a schematic (the label gives the real count)."""
    k = math.ceil(n / max(1, c))
    if k <= MAX_LINES:
        pos = [length * min(n, i * c) / n for i in range(1, k)]
        first = length * min(n, c) / n
    else:
        pos = [length * i / MAX_LINES for i in range(1, MAX_LINES)]
        first = length / MAX_LINES
    return pos, first, k


def _per(k: int, c: int) -> str:
    return f"{k:,} chunk{'s' if k != 1 else ''} of {c:,}"


def _vis_len(n: int, lo: float, hi: float) -> float:
    """Drawn length grows with log(size) so tiny and huge axes both stay readable."""
    return lo + (hi - lo) * min(1.0, math.log10(max(1, n)) / 5.5)


def cube_svg(v: dict) -> str:
    dims, shape, chunks, shards = v["dims"], v["shape"], v["chunks"], v["shards"]
    size = dict(zip(dims, shape))
    ch = dict(zip(dims, chunks))
    sh = dict(zip(dims, shards)) if shards else {}
    has_t = "valid_time" in size
    lev = next((d for d in dims if d.startswith("isobaric")), None)
    if "point" in size:
        return _rect_svg(v, size, ch, sh)
    ny, nx = size["latitude"], size["longitude"]
    asp = nx / ny
    W = 190 if asp >= 1 else max(60, 190 * asp)
    H = 190 / asp if asp >= 1 else 190
    H = max(50, min(190, H))
    W = max(60, W)
    D = _vis_len(size["valid_time"], 60, 170) if has_t else 0
    dx, dy = 0.62 * D, -0.42 * D  # oblique depth vector for the whole time axis
    pad = 40
    ox, oy = pad + 10, pad + 20 - dy
    Wt, Ht = ox + W + dx + (170 if lev else 125), oy + H + 50
    out = [f'<svg viewBox="0 0 {Wt:.0f} {Ht:.0f}" width="{Wt:.0f}" style="max-width:100%;height:auto" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="Roboto, sans-serif" font-size="13">',
           '<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#ef2b89"/>'
           '<stop offset="1" stop-color="#7b34db"/></linearGradient></defs>']

    def P(x, y, z):  # x: lon px, y: lat px from top, z: time fraction 0..1
        return ox + x + dx * z, oy + y + dy * z

    def poly(pts, **kw):
        a = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        attrs = " ".join(f'{k.replace("_", "-")}="{val}"' for k, val in kw.items())
        out.append(f'<polygon points="{a}" {attrs}/>')

    def ln(p, q, color="rgba(255,255,255,.18)", w=0.8):
        out.append(f'<line x1="{p[0]:.1f}" y1="{p[1]:.1f}" x2="{q[0]:.1f}" y2="{q[1]:.1f}" stroke="{color}" stroke-width="{w}"/>')

    face = dict(stroke="rgba(255,255,255,.35)", stroke_width="1")
    if has_t:
        poly([P(0, 0, 0), P(W, 0, 0), P(W, 0, 1), P(0, 0, 1)], fill="#26324b", **face)  # top
        poly([P(W, 0, 0), P(W, H, 0), P(W, H, 1), P(W, 0, 1)], fill="#1c263b", **face)  # right
    poly([P(0, 0, 0), P(W, 0, 0), P(W, H, 0), P(0, H, 0)], fill="#222d45", **face)  # front

    xs, fx, kx = _axis(nx, ch["longitude"], W)
    ys, fy, ky = _axis(ny, ch["latitude"], H)
    shx = _axis(nx, sh["longitude"], W)[0] if sh and math.ceil(nx / sh["longitude"]) <= MAX_LINES else []
    shy = _axis(ny, sh["latitude"], H)[0] if sh and math.ceil(ny / sh["latitude"]) <= MAX_LINES else []
    for x in xs:
        ln(P(x, 0, 0), P(x, H, 0))
        if has_t:
            ln(P(x, 0, 0), P(x, 0, 1))
    for y in ys:
        ln(P(0, y, 0), P(W, y, 0))
        if has_t:
            ln(P(W, y, 0), P(W, y, 1))
    if has_t:
        zs, fz, kz = _axis(size["valid_time"], ch["valid_time"], 1.0)
        for z in zs:
            ln(P(0, 0, z), P(W, 0, z))
            ln(P(W, 0, z), P(W, H, z))
        shz = (_axis(size["valid_time"], sh["valid_time"], 1.0)[0]
               if sh and math.ceil(size["valid_time"] / sh["valid_time"]) <= MAX_LINES else [])
        for z in shz:
            ln(P(0, 0, z), P(W, 0, z), "#c58cff", 1.6)
            ln(P(W, 0, z), P(W, H, z), "#c58cff", 1.6)
    else:
        fz = 0
    for x in shx:
        ln(P(x, 0, 0), P(x, H, 0), "#c58cff", 1.6)
        if has_t:
            ln(P(x, 0, 0), P(x, 0, 1), "#c58cff", 1.6)
    for y in shy:
        ln(P(0, y, 0), P(W, y, 0), "#c58cff", 1.6)
        if has_t:
            ln(P(W, y, 0), P(W, y, 1), "#c58cff", 1.6)

    # one chunk highlighted at the front-top-left corner
    fx, fy = max(fx, 5), max(fy, 5)
    hl = dict(fill="url(#g)", stroke="#fff", stroke_width="1.2")
    if has_t:
        fz = max(fz, 0.04)
        poly([P(0, 0, 0), P(fx, 0, 0), P(fx, 0, fz), P(0, 0, fz)], fill_opacity=".95", **hl)
        poly([P(fx, 0, 0), P(fx, fy, 0), P(fx, fy, fz), P(fx, 0, fz)], fill_opacity=".75", **hl)
    poly([P(0, 0, 0), P(fx, 0, 0), P(fx, fy, 0), P(0, fy, 0)], **hl)

    def text(x, y, s, anchor="middle", color="#e9edf5", weight="400", size_=13):
        out.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" fill="{color}" font-weight="{weight}" '
                   f'font-size="{size_}">{html.escape(s)}</text>')

    mut = "#97a3b8"
    # axis labels: size and chunks along each axis
    bx, by = P(W / 2, H, 0)
    text(bx, by + 20, f"longitude {nx:,}", weight="500")
    text(bx, by + 35, _per(kx, ch['longitude']), color=mut, size_=12)
    lx, lyy = P(0, H / 2, 0)
    out.append(f'<g transform="translate({lx - 14:.1f},{lyy:.1f}) rotate(-90)">')
    out.append(f'<text text-anchor="middle" fill="#e9edf5" font-weight="500">latitude {ny:,}</text>')
    out.append(f'<text y="-16" text-anchor="middle" fill="{mut}" font-size="12">{_per(ky, ch["latitude"])}</text></g>')
    if has_t:
        tx, ty = P(W, H, 0.5)
        text(tx + 12, ty + 4, f"time {size['valid_time']:,}", anchor="start", weight="500")
        text(tx + 12, ty + 19, _per(kz, ch['valid_time']), anchor="start", color=mut, size_=12)
    if lev:  # a small stack of layers: one such cube per pressure level
        lx0, ly0 = P(W, H, 1 if has_t else 0)
        lx0, ly0 = lx0 + 14, ly0 - 64
        for i in range(min(size[lev], 6) - 1, -1, -1):
            yy = ly0 + i * 5
            out.append(f'<polygon points="{lx0:.1f},{yy + 6:.1f} {lx0 + 14:.1f},{yy:.1f} {lx0 + 28:.1f},{yy + 6:.1f} '
                       f'{lx0 + 14:.1f},{yy + 12:.1f}" fill="{'url(#g)' if i == 0 else '#26324b'}" '
                       f'stroke="rgba(255,255,255,.5)" stroke-width=".8"/>')
        text(lx0 + 36, ly0 + 12, f"× {size[lev]} level{'s' if size[lev] != 1 else ''}", anchor="start", weight="500")
        text(lx0 + 36, ly0 + 27, "1 level per chunk", anchor="start", color=mut, size_=12)
    # callout for the highlighted chunk
    cx, cy = P(fx / 2, 0, fz / 2)
    text(cx, cy - 12, "1 chunk", color="#ffcd57", weight="500")
    out.append("</svg>")
    return "".join(out)


def _rect_svg(v, size, ch, sh) -> str:
    """Point datasets: a 2-D grid, time down the x axis, points on y."""
    has_t = "valid_time" in size
    nt = size.get("valid_time", 1)
    npnt = size["point"]
    W = _vis_len(nt, 120, 360) if has_t else 40
    H = _vis_len(npnt, 60, 180)
    ox, oy = 70, 20
    out = [f'<svg viewBox="0 0 {ox + W + 40:.0f} {oy + H + 60:.0f}" width="{ox + W + 40:.0f}" style="max-width:100%;height:auto" xmlns="http://www.w3.org/2000/svg" '
           'font-family="Roboto, sans-serif" font-size="13">',
           '<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#ef2b89"/>'
           '<stop offset="1" stop-color="#7b34db"/></linearGradient></defs>',
           f'<rect x="{ox}" y="{oy}" width="{W:.1f}" height="{H:.1f}" fill="#222d45" stroke="rgba(255,255,255,.35)"/>']
    xs, fx, kx = _axis(nt, ch.get("valid_time", 1), W) if has_t else ([], W, 1)
    ys, fy, ky = _axis(npnt, ch["point"], H)
    for x in xs:
        out.append(f'<line x1="{ox + x:.1f}" y1="{oy}" x2="{ox + x:.1f}" y2="{oy + H:.1f}" stroke="rgba(255,255,255,.18)"/>')
    for y in ys:
        out.append(f'<line x1="{ox}" y1="{oy + y:.1f}" x2="{ox + W:.1f}" y2="{oy + y:.1f}" stroke="rgba(255,255,255,.18)"/>')
    out.append(f'<rect x="{ox}" y="{oy}" width="{max(fx, 5):.1f}" height="{max(fy, 5):.1f}" fill="url(#g)" stroke="#fff"/>')
    if has_t:
        out.append(f'<text x="{ox + W / 2:.1f}" y="{oy + H + 20:.1f}" text-anchor="middle" fill="#e9edf5" '
                   f'font-weight="500">time {nt:,}</text>')
        out.append(f'<text x="{ox + W / 2:.1f}" y="{oy + H + 35:.1f}" text-anchor="middle" fill="#97a3b8" '
                   f'font-size="12">{_per(kx, ch["valid_time"])}</text>')
    out.append(f'<g transform="translate({ox - 14},{oy + H / 2:.1f}) rotate(-90)"><text text-anchor="middle" '
               f'fill="#e9edf5" font-weight="500">points {npnt:,}</text><text y="-16" text-anchor="middle" '
               f'fill="#97a3b8" font-size="12">{_per(ky, ch["point"])}</text></g></svg>')
    return "".join(out)


# ------------------------------------------------------------------ an existing store (completed jobs)

STATS = ("mean", "min", "max", "sum", "std", "median")


def _codec_text(codecs: list[dict], keepbits) -> str:
    parts = [f"bitround {keepbits} bits"] if keepbits is not None else []
    for c in codecs:
        n, cfg = c.get("name"), c.get("configuration", {}) or {}
        if n == "sharding_indexed":
            parts.append(_codec_text(cfg.get("codecs", []), None) + " (in shards)")
        elif n == "blosc":
            parts.append(f"blosc {cfg.get('cname')} L{cfg.get('clevel')} {str(cfg.get('shuffle')).replace('noshuffle', 'no shuffle')}")
        elif n in ("zstd", "gzip"):
            parts.append(f"{n} L{cfg.get('level')}")
        elif n == "bytes":
            parts.append("bytes")
        elif n:
            parts.append(n)
    return " → ".join(parts)


def _du(p) -> tuple[int, int]:
    """(bytes, files) under a directory."""
    from pathlib import Path

    size = files = 0
    for f in Path(p).rglob("*"):
        if f.is_file():
            size += f.stat().st_size
            files += f.name != "zarr.json"
    return size, files


def from_store(path) -> dict:
    """The same description as ``describe`` but read from a written Zarr directory or Icechunk repository:
    real shapes, chunks, codecs and (for Zarr) real sizes on disk."""
    from pathlib import Path

    import xarray as xr
    import zarr

    path = Path(path)
    is_ic = path.suffix == ".icechunk"
    if is_ic:
        import icechunk as ic

        store = ic.Repository.open(ic.local_filesystem_storage(str(path))).readonly_session("main").store
        g = zarr.open_group(store, mode="r")
        ds = xr.open_zarr(store, consolidated=False)
    else:
        g = zarr.open_group(str(path), mode="r")
        ds = xr.open_zarr(str(path))
    job = g.attrs.get("edh_job") or {}
    keepbits = (job.get("output") or {}).get("keepbits") if isinstance(job, dict) else None
    arrays = dict(g.arrays())
    dimnames = {d for a in arrays.values() for d in (a.metadata.dimension_names or ())}
    coords, data, codecs = [], [], ""
    for name, a in sorted(arrays.items()):
        dims = tuple(a.metadata.dimension_names or ())
        is_coord = name in dimnames or (len(dims) == 1 and dims[0] == "point")
        if is_coord:
            coords.append(dict(name=name, dims=dims, shape=a.shape, dtype=str(a.dtype), detail=_coord_detail(ds, name)))
            continue
        cd = [c.to_dict() for c in a.metadata.codecs]
        codecs = codecs or _codec_text(cd, keepbits)
        item = a.dtype.itemsize
        n_chunks = math.prod(math.ceil(s / c) for s, c in zip(a.shape, a.chunks))
        raw = math.prod(a.shape) * item
        disk, files = (None, None) if is_ic else _du(path / name)
        at = dict(a.attrs)
        stat = next((s for s in STATS if name.endswith("_" + s)), None)
        data.append(dict(
            name=name, source=name, stat=stat, kind="static" if "valid_time" not in dims else "time",
            long_name=at.get("long_name", name), units=at.get("units", ""),
            dims=dims, shape=a.shape, chunks=a.chunks, shards=a.shards, dtype=str(a.dtype),
            chunk_bytes=math.prod(a.chunks) * item, n_chunks=n_chunks,
            n_files=files if files is not None else (math.prod(math.ceil(s / c) for s, c in zip(a.shape, a.shards))
                                                     if a.shards else n_chunks),
            raw=raw, disk=disk))
    order = {"valid_time": 0, "latitude": 2, "longitude": 3, "point": 1}
    coords.sort(key=lambda c: (order.get(c["name"], 1 if c["name"].startswith("isobaric") else 5), c["name"]))
    total_disk, total_files = _du(path)
    return dict(name=path.name, coords=coords, data=data, attrs=list(g.attrs.keys()), codecs=codecs,
                raw=sum(d["raw"] for d in data), disk=total_disk, files=total_files, measured=True,
                format="Icechunk" if is_ic else "Zarr v3")


def _coord_detail(ds, name) -> str:
    try:
        v = ds[name].values
    except Exception:
        return ""
    if not len(v):
        return ""
    if np.issubdtype(v.dtype, np.datetime64):
        import pandas as pd

        t = pd.to_datetime(v)
        step = ""
        if len(t) > 1:
            d = (t[1] - t[0])
            step = " · " + ("hourly" if d == pd.Timedelta("1h") else "daily" if d == pd.Timedelta("1D")
                            else f"{d}" if d < pd.Timedelta("28D") else "monthly" if d <= pd.Timedelta("31D") else "yearly")
        return f"{t[0]:%Y-%m-%d %H:%M} … {t[-1]:%Y-%m-%d %H:%M}{step}"
    if name.startswith("isobaric"):
        return ", ".join(_fmt_num(x) for x in v[:12]) + (" …" if len(v) > 12 else "") + " hPa"
    if v.dtype.kind == "f" and name in ("latitude", "longitude") and ds[name].dims == (name,):
        res = abs(float(v[1] - v[0])) if len(v) > 1 else None
        return f"{_fmt_num(v[0])}° … {_fmt_num(v[-1])}°" + (f" · {_fmt_num(res)}°" if res else "")
    if v.dtype.kind in "OUS":
        return ", ".join(map(str, v[:4])) + (" …" if len(v) > 4 else "")
    return f"{_fmt_num(v[0])} … {_fmt_num(v[-1])}" if v.dtype.kind in "fi" else ""
