"""Draw the documentation diagrams (docs/diagrams/*.svg).

Plain SVG with no dependencies, so the diagrams stay editable: change a layout here and run

    python docs/diagrams/build.py

Every diagram sits on a white card, so it stays readable in GitHub's dark mode too.
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent
FONT = "Inter, 'Segoe UI', Helvetica, Arial, sans-serif"
INK, MUTED = "#1F2A37", "#5B6675"

# kind: (fill, stroke)
KINDS = {
    "ui": ("#E8F0FF", "#3B7BF0"),      # interface, you
    "edh": ("#FFF1E3", "#E8780C"),     # Earth Data Hub
    "cds": ("#E0F5F7", "#0E8E9A"),     # Climate Data Store
    "proc": ("#F1E8FF", "#7B34DB"),    # processing
    "store": ("#E3F7EC", "#1C9A5A"),   # storage
    "file": ("#F3F5F8", "#7A8696"),    # files on disk
    "ext": ("#EEF1F6", "#44546A"),     # outside services
    "warn": ("#FDECEE", "#D9363E"),    # failures
    "ok": ("#E3F7EC", "#1C9A5A"),
    "ink": ("#FFFFFF", "#44546A"),
}


class D:
    def __init__(self, w, h, title, subtitle=None):
        self.w, self.h = w, h
        self.parts: list[str] = []
        self.title, self.subtitle = title, subtitle

    # ------------------------------------------------------------ primitives
    def text(self, x, y, s, size=13, weight=400, fill=INK, anchor="start", italic=False):
        st = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
                          f'text-anchor="{anchor}"{st}>{escape(s)}</text>')

    def rect(self, x, y, w, h, fill, stroke, rx=12, dash=False, sw=2):
        d = ' stroke-dasharray="7 6"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                          f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def box(self, x, y, w, h, title, lines=(), kind="ui", size=15):
        fill, stroke = KINDS[kind]
        self.rect(x, y, w, h, fill, stroke)
        n = len(lines)
        block = 20 + 18 * n
        ty = y + (h - block) / 2 + 15
        self.text(x + w / 2, ty, title, size=size, weight=650, anchor="middle")
        for i, line in enumerate(lines):
            self.text(x + w / 2, ty + 21 + 18 * i, line, size=12.5, fill=MUTED, anchor="middle")

    def frame(self, x, y, w, h, label, kind):
        fill, stroke = KINDS[kind]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{fill}" fill-opacity="0.45" '
                          f'stroke="{stroke}" stroke-width="2" stroke-dasharray="8 6"/>')
        self.text(x + 16, y + 24, label, size=13.5, weight=700, fill=stroke)

    def pill(self, x, y, w, h, label, kind, size=14):
        fill, stroke = KINDS[kind]
        self.rect(x, y, w, h, fill, stroke, rx=h / 2)
        self.text(x + w / 2, y + h / 2 + 5, label, size=size, weight=650, anchor="middle")

    def arrow(self, pts, kind="ink", label=None, at=None, dash=False, head=True, lsize=12, lanchor="middle"):
        stroke = KINDS[kind][1]
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        dd = ' stroke-dasharray="6 5"' if dash else ""
        mk = f' marker-end="url(#a-{kind})"' if head else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="2.2" '
                          f'stroke-linejoin="round"{dd}{mk}/>')
        if label:
            if at is None:
                (x1, y1), (x2, y2) = pts[len(pts) // 2 - 1], pts[len(pts) // 2]
                at = ((x1 + x2) / 2, (y1 + y2) / 2 - 8)
            self.label(at[0], at[1], label, kind, lsize, lanchor)

    def label(self, x, y, s, kind="ink", size=12, anchor="middle"):
        w = len(s) * size * 0.56 + 10
        x0 = x - w / 2 if anchor == "middle" else (x - 5 if anchor == "start" else x - w + 5)
        self.parts.append(f'<rect x="{x0:.1f}" y="{y - size + 1:.1f}" width="{w:.1f}" height="{size + 6}" rx="4" '
                          f'fill="#FFFFFF" fill-opacity="0.92"/>')
        self.text(x, y + 2, s, size=size, weight=600, fill=KINDS[kind][1], anchor=anchor)

    def num(self, x, y, n, kind):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="12" fill="{KINDS[kind][1]}"/>')
        self.text(x, y + 4.5, str(n), size=13, weight=700, fill="#FFFFFF", anchor="middle")

    def raw(self, s):
        self.parts.append(s)

    # ------------------------------------------------------------ output
    def save(self, name):
        markers = "".join(
            f'<marker id="a-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{s}"/></marker>'
            for k, (_, s) in KINDS.items())
        head = [f'<text x="28" y="40" font-size="21" font-weight="750" fill="{INK}">{escape(self.title)}</text>']
        if self.subtitle:
            head.append(f'<text x="28" y="62" font-size="13.5" fill="{MUTED}">{escape(self.subtitle)}</text>')
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
               f'height="{self.h}" font-family="{FONT}" role="img" aria-label="{escape(self.title)}">\n'
               f'<defs>{markers}</defs>\n'
               f'<rect x="1" y="1" width="{self.w - 2}" height="{self.h - 2}" rx="18" fill="#FFFFFF" stroke="#DDE3EA" '
               f'stroke-width="2"/>\n' + "\n".join(head + self.parts) + "\n</svg>\n")
        (OUT / f"{name}.svg").write_text(svg)


# ====================================================================== diagrams
def overview():
    d = D(960, 570, "How a download works",
          "One job model for both sources; everything after the download is shared.")
    d.box(40, 84, 260, 80, "You: web UI or CLI", ["source, dataset, variables, levels", "time, area, grid, storage"], "ui")
    d.arrow([(300, 124), (346, 124)])
    d.box(350, 84, 230, 80, "job.json", ["the whole selection", "names the key, never holds it"], "file")
    d.arrow([(580, 124), (626, 124)])
    d.box(630, 84, 290, 80, "Plan and cost first", ["requests, sizes, quota, licences", "nothing is downloaded yet"], "ui")
    d.arrow([(775, 164), (775, 184), (255, 184), (255, 210)], "edh", head=True)
    d.arrow([(775, 184), (705, 184), (705, 210)], "cds")
    d.label(420, 180, "source = edh", "edh")
    d.label(860, 200, "source = cds", "cds")
    d.frame(40, 210, 430, 160, "Earth Data Hub", "edh")
    d.box(60, 248, 185, 102, "Planner", ["every source chunk", "the selection touches"], "edh")
    d.arrow([(245, 299), (263, 299)], "edh")
    d.box(265, 248, 185, 102, "Engine", ["N parallel HTTP reads", "of Zarr chunks"], "edh")
    d.frame(490, 210, 430, 160, "Climate Data Store", "cds")
    d.box(510, 248, 185, 102, "Planner", ["fewest requests under", "the cost limit"], "cds")
    d.arrow([(695, 299), (713, 299)], "cds")
    d.box(715, 248, 185, 102, "Engine", ["request ledger and", "adaptive queue"], "cds")
    d.arrow([(255, 370), (255, 392), (180, 392)], "edh", head=False)
    d.arrow([(705, 370), (705, 392), (180, 392), (180, 416)], "cds")
    d.box(40, 418, 280, 100, "Processor (shared)", ["decode Zarr chunks, GRIB, NetCDF", "cut area, aggregate time, encode"], "proc")
    d.arrow([(320, 468), (346, 468)], "store")
    d.box(350, 418, 260, 100, "Icechunk repository", ["one commit per unit", "a stopped job resumes here"], "store")
    d.arrow([(610, 468), (636, 468)], "store")
    d.box(640, 418, 280, 100, "Zarr v3 export", ["your chunk layout, codec, shards", "then xr.open_zarr(...)"], "store")
    d.text(40, 550, "Downloads run as detached jobs: close the browser or the SSH session and the job keeps going.",
           size=13, fill=MUTED, italic=True)
    d.save("overview")


def where_it_runs():
    d = D(960, 540, "Where things run",
          "The interface runs on your computer; keys, jobs and data stay on the machine that downloads.")
    d.frame(30, 84, 410, 410, "Your computer", "ui")
    d.box(60, 124, 350, 62, "Browser", ["one tab per connection"], "file")
    d.arrow([(235, 186), (235, 212)])
    d.box(60, 214, 350, 80, "edh-dl app (NiceGUI)", ["gui/connect.py: servers and tabs", "gui/app.py: the job form, keys, jobs"], "ui")
    d.arrow([(235, 294), (235, 320)])
    d.box(60, 322, 350, 80, "backend.Local / backend.Remote", ["await call(method, **kwargs)", "same JSON answers either way"], "ui")
    d.text(60, 440, "This computer: Local runs service.py", size=12.5, fill=MUTED)
    d.text(60, 458, "in the same process, no SSH involved.", size=12.5, fill=MUTED)
    d.frame(520, 84, 410, 410, "Server (or this computer)", "proc")
    d.box(550, 124, 350, 80, "edh-dl agent", ["JSON lines on stdin / stdout", "one thread per request"], "proc")
    d.arrow([(725, 204), (725, 230)], "proc")
    d.box(550, 232, 350, 80, "service.py", ["keys, quota, catalogues, plans,", "jobs, output stores"], "proc")
    d.arrow([(640, 312), (640, 346)], "proc", label="launch", at=(605, 334))
    d.arrow([(810, 312), (810, 346)], "file")
    d.box(550, 348, 170, 110, "Job workers", ["detached _worker", "processes (setsid),", "survive logout"], "proc")
    d.arrow([(720, 403), (728, 403)], "file")
    d.box(730, 348, 170, 110, "Files", ["keys.json, usage.json", "jobs/<id>/", "output stores"], "file")
    d.arrow([(410, 362), (470, 362), (470, 164), (546, 164)], "proc", label="SSH (asyncssh)", at=(470, 268))
    d.save("where-it-runs")


def job_states():
    d = D(960, 450, "Job states",
          "The worker writes state.json; the UI and the CLI read it, and ask for changes through control.json.")
    top = [("created", 40), ("starting", 230), ("running", 420), ("exporting", 610), ("completed", 800)]
    for name, x in top:
        d.pill(x, 112, 150, 44, name, "ok" if name == "completed" else ("proc" if name in ("running", "exporting") else "ui"))
    for (_, a), (_, b) in zip(top, top[1:]):
        d.arrow([(a + 150, 134), (b - 4, 134)], "ink")
    d.pill(230, 200, 150, 40, "waiting_quota", "edh", size=13)
    d.arrow([(440, 156), (380, 210)], "edh")
    d.arrow([(380, 230), (452, 158)], "edh")
    d.text(232, 258, "EDH: monthly limit reached,", size=11.5, fill=MUTED)
    d.text(232, 273, "waits for the reset", size=11.5, fill=MUTED)
    bottom = [("paused", 40, "Pause, or Ctrl+C", "ui"), ("paused_quota", 220, "quota limit, no waiting", "edh"),
              ("cancelled", 400, "Cancel", "file"), ("failed", 580, "an error (see the log)", "warn"),
              ("interrupted", 760, "the worker died", "warn")]
    d.arrow([(495, 156), (495, 300)], "ink", head=False)
    d.arrow([(115, 300), (855, 300)], "ink", head=False)
    for name, x, cap, kind in bottom:
        cx = x + 80
        d.arrow([(cx, 300), (cx, 318)], "ink")
        d.pill(x, 320, 160, 42, name, kind, size=13.5)
        d.text(cx, 380, cap, size=11.5, fill=MUTED, anchor="middle")
    for x in (115, 300, 660, 840):
        d.arrow([(x, 386), (x, 408)], "ui", head=False, dash=True)
    d.arrow([(840, 408), (18, 408), (18, 92), (305, 92), (305, 108)], "ui", dash=True)
    d.label(160, 92, "Resume: a new worker continues", "ui")
    d.save("job-states")


def edh_engine():
    d = D(960, 560, "Earth Data Hub engine",
          "Async downloads fill a spool a few units ahead; one processor thread commits the units in order.")
    d.frame(30, 84, 430, 440, "asyncio event loop", "edh")
    d.box(50, 120, 390, 74, "Producer", ["queues the chunks of the next units:", "at most 2 units ahead, spool and disk limits"], "edh")
    d.arrow([(245, 194), (245, 214)], "edh")
    d.box(50, 216, 390, 46, "work queue", [], "file", size=14)
    d.arrow([(245, 262), (245, 286)], "edh")
    d.box(50, 288, 390, 88, "Workers", ["N connections, changeable while running", "GET, retry, count the request, write file"], "edh")
    d.box(50, 410, 390, 88, "Monitor (every 2 s)", ["reads control.json: connections, pause", "rates, ETA, waits for the quota reset"], "ui")
    d.arrow([(245, 410), (245, 380)], "ui")
    d.box(490, 112, 150, 84, "Earth Data Hub", ["Zarr chunks", "over HTTPS"], "ext", size=14)
    d.arrow([(440, 312), (466, 312), (466, 154), (486, 154)], "edh")
    d.box(490, 286, 150, 110, "Spool", ["one file per", "source chunk", "(.miss = empty)"], "file")
    d.arrow([(440, 352), (486, 352)], "edh")
    d.frame(668, 84, 266, 440, "Processor thread", "proc")
    d.box(688, 120, 226, 74, "Wait", ["until every chunk of the", "next unit is in the spool"], "proc")
    d.arrow([(640, 330), (660, 330), (660, 157), (684, 157)], "proc")
    d.arrow([(801, 194), (801, 214)], "proc")
    d.box(688, 216, 226, 88, "Decode, cut, aggregate", ["space groups in a thread", "pool, within memory_gb"], "proc")
    d.arrow([(801, 304), (801, 324)], "store")
    d.box(688, 326, 226, 70, "Commit the unit", ["one Icechunk commit"], "store")
    d.arrow([(801, 396), (801, 416)], "file")
    d.box(688, 418, 226, 74, "Free the spool", ["chunks no later", "unit needs"], "file")
    d.arrow([(914, 455), (926, 455), (926, 157), (918, 157)], "proc", dash=True)
    d.save("edh-engine")


def edh_units():
    d = D(960, 420, "Units: how a long period is cut",
          "Illustration: 4 output periods per source time chunk, shifted by a UTC offset.")
    x0, cw, n = 80, 140, 6
    d.text(x0, 104, "Source time chunks (each downloaded once)", size=13, weight=650, fill=KINDS["edh"][1])
    for i in range(n):
        f = "#FFE2C2" if i % 2 == 0 else "#FFD0A0"
        d.rect(x0 + i * cw, 114, cw, 40, f, KINDS["edh"][1], rx=6)
        d.text(x0 + i * cw + cw / 2, 139, f"chunk {i}", size=13, weight=600, anchor="middle")
    d.text(x0, 192, "Output periods (days, months, ...)", size=13, weight=650, fill=KINDS["proc"][1])
    pw, off = 35, 17
    k = 0
    while x0 + off + (k + 1) * pw <= x0 + n * cw:
        px = x0 + off + k * pw
        u = int((px - x0) // cw)
        d.rect(px + 1, 202, pw - 2, 30, "#EADCFF" if u % 2 == 0 else "#D8C2FF", KINDS["proc"][1], rx=4, sw=1.4)
        k += 1
    d.text(x0, 270, "Units: the periods starting inside one source chunk, one Icechunk commit each",
           size=13, weight=650, fill=KINDS["store"][1])
    for u in range(n):
        starts = [x0 + off + j * pw for j in range(k) if x0 + u * cw <= x0 + off + j * pw < x0 + (u + 1) * cw]
        if not starts:
            continue
        a, b = starts[0], starts[-1] + pw
        d.raw(f'<path d="M{a + 2} 282 v10 H{b - 2} v-10" fill="none" stroke="{KINDS["store"][1]}" stroke-width="2.4"/>')
        d.text((a + b) / 2, 312, f"unit {u}", size=13, weight=650, anchor="middle")
    d.raw(f'<path d="M{x0 + off + 3 * pw} 233 L{x0 + off + 3 * pw + pw} 233" stroke="{KINDS["warn"][1]}" stroke-width="4"/>')
    bx = x0 + cw
    d.raw(f'<path d="M{bx} 156 V244" stroke="{KINDS["warn"][1]}" stroke-width="2" stroke-dasharray="4 4"/>')
    d.label(bx + 4, 252, "crosses into chunk 1", "warn", 11.5, "start")
    d.text(x0, 350, "The last period of unit 0 reaches into chunk 1, so chunk 1 serves units 0 and 1: it stays in the spool",
           size=12.5, fill=MUTED)
    d.text(x0, 368, "until the last unit that needs it is committed. Inside a unit, space is processed in groups that fit",
           size=12.5, fill=MUTED)
    d.text(x0, 386, "the memory budget. A unit is also the checkpoint: a resumed job starts at the first unit not committed.",
           size=12.5, fill=MUTED)
    d.save("edh-units")


def cds_engine():
    d = D(960, 610, "Climate Data Store engine",
          "Few big requests, recorded before they are sent, downloaded as soon as they are ready.")
    d.box(30, 86, 280, 104, "1 · Plan (free)", ["months and years packed under the", "cost limit; costing, constraints, licences"], "cds")
    d.arrow([(310, 138), (336, 138)], "cds")
    d.box(340, 86, 260, 104, "2 · Ledger", ["requests.json: every request", "and its state, saved first"], "file")
    d.arrow([(600, 138), (626, 138)], "cds")
    d.box(630, 86, 300, 104, "3 · Queue", ["at most max_inflight on the CDS;", "halves when throttled, grows back"], "cds")
    d.arrow([(780, 190), (780, 232)], "cds", label="submit", at=(820, 214))
    d.box(630, 236, 300, 104, "Climate Data Store", ["accepted, running, successful:", "seconds to hours per request"], "ext")
    d.arrow([(630, 288), (604, 288)], "cds")
    d.box(340, 236, 260, 104, "4 · Poll", ["one job-list call for all requests,", "every 5 s, up to 120 s when quiet"], "cds")
    d.arrow([(340, 288), (314, 288)], "cds")
    d.box(30, 236, 280, 104, "5 · Download", ["2 at a time, resumable, checksum;", "expired result: ask again"], "cds")
    d.arrow([(170, 340), (170, 382)], "file")
    d.box(30, 386, 280, 92, "Spool", ["result files, up to max_spool_gb"], "file")
    d.arrow([(310, 432), (336, 432)], "proc")
    d.box(340, 386, 260, 92, "6 · Processor", ["GRIB: message by message", "NetCDF: slabs of time steps"], "proc")
    d.arrow([(600, 432), (626, 432)], "store")
    d.box(630, 386, 300, 92, "7 · Icechunk, then Zarr", ["one commit per unit, files freed;", "export with your layout at the end"], "store")
    d.arrow([(470, 478), (470, 516)], "warn", dash=True)
    d.box(340, 518, 590, 64, "Not a grid? (station tables, time series, mixed products)",
          ["kept as delivered in <name>.files/ with a manifest.json"], "warn", size=14)
    d.save("cds-engine")


def cds_ledger():
    d = D(960, 430, "Life of one CDS request",
          "Each change is saved to requests.json before the next step: a request is never sent twice.")
    names = ["planned", "submitting", "queued", "ready", "downloaded", "done"]
    xs = [20 + i * 160 for i in range(6)]
    for name, x in zip(names, xs):
        d.pill(x, 140, 120, 44, name, "ok" if name == "done" else ("file" if name == "planned" else "cds"), size=13.5)
    for a, b in zip(xs, xs[1:]):
        d.arrow([(a + 120, 162), (b - 4, 162)], "cds")
    # crash between POST and save: found on the CDS
    d.arrow([(240, 140), (240, 108), (400, 108), (400, 136)], "ui", dash=True)
    d.label(320, 100, "crash after the POST: found on the CDS, not resent", "ui")
    # back to planned
    d.arrow([(400, 184), (400, 224), (80, 224), (80, 188)], "warn", dash=True)
    d.label(240, 220, "throttled, gone, or failed (under 3 tries)", "warn")
    d.arrow([(560, 184), (560, 262), (80, 262), (80, 224)], "warn", dash=True, head=False)
    d.label(470, 258, "result expired", "warn")
    d.arrow([(420, 184), (420, 316)], "warn")
    d.pill(360, 320, 120, 40, "failed", "warn", size=13.5)
    d.text(432, 304, "3rd failure: the job stops", size=12, fill=KINDS["warn"][1], weight=600)
    d.text(20, 396, "On resume, queued requests are polled again and downloaded files are reused: the CDS only sees",
           size=12.5, fill=MUTED)
    d.text(20, 414, "a request again when its result is gone, and an identical request is usually served from its cache.",
           size=12.5, fill=MUTED)
    d.save("cds-ledger")


def cds_packing():
    d = D(960, 500, "Fewest requests: packing",
          "Illustration: 4 years of ERA5 single levels, 5 variables, hourly, over Europe (limit 121,000 fields).")
    years = ["2020", "2021", "2022", "2023"]
    c = 28
    for gx, title, sub in ((80, "One month per request", "48 requests, about 3,720 fields each"),
                           (540, "Packed by the planner", "2 requests, about 87,700 fields each")):
        d.text(gx, 108, title, size=14.5, weight=700)
        for m, ml in enumerate("JFMAMJJASOND"):
            d.text(gx + m * c + c / 2, 132, ml, size=11, fill=MUTED, anchor="middle")
        for r, y in enumerate(years):
            d.text(gx - 8, 140 + r * c + c / 2 + 5, y, size=11.5, fill=MUTED, anchor="end")
            for m in range(12):
                if gx == 80:
                    d.rect(gx + m * c + 2, 140 + r * c + 2, c - 4, c - 4, "#CFEFF3", KINDS["cds"][1], rx=4, sw=1.4)
        if gx == 540:
            for b, col in ((0, "#7FD3DC"), (1, "#F6B26B")):
                d.rect(gx + 2, 140 + b * 2 * c + 2, 12 * c - 4, 2 * c - 4, col, INK, rx=8, sw=1.6)
                d.text(gx + 6 * c, 140 + b * 2 * c + c + 5, f"request {b + 1}: {years[2 * b]} and {years[2 * b + 1]}",
                       size=13, weight=650, anchor="middle")
        d.text(gx, 140 + 4 * c + 26, sub, size=13, fill=KINDS["cds"][1], weight=650)
    d.text(80, 330, "Request sizes the planner tries, from the biggest to the smallest:", size=13.5, weight=650)
    steps = [("whole years", "cds"), ("months of a year", "cds"), ("variables", "edh"), ("levels", "edh"), ("days", "warn")]
    x = 80
    for i, (s, k) in enumerate(steps):
        w = 30 + len(s) * 8.2
        d.pill(x, 346, w, 38, s, k, size=13)
        if i < len(steps) - 1:
            d.arrow([(x + w + 4, 365), (x + w + 30, 365)])
        x += w + 34
    d.text(80, 418, "Months only partly selected are asked for whole when that saves a request: a few extra fields cost",
           size=12.5, fill=MUTED)
    d.text(80, 436, "little, a queue slot can cost hours. The area is cut and the grid changed on the CDS side when the",
           size=12.5, fill=MUTED)
    d.text(80, 454, "dataset allows it. Every request is then priced by the CDS costing endpoint (free) before launch.",
           size=12.5, fill=MUTED)
    d.save("cds-packing")


def cds_queue():
    d = D(960, 400, "Adaptive queue",
          "How many requests may wait on the CDS at once, over a job's life (setting: 4).")
    X0, X1, Y0 = 90, 920, 320
    yv = lambda cap: Y0 - cap * 50  # noqa: E731
    d.raw(f'<path d="M{X0} {Y0} H{X1}" stroke="{MUTED}" stroke-width="1.6"/>')
    d.raw(f'<path d="M{X0} {Y0} V{yv(4.6)}" stroke="{MUTED}" stroke-width="1.6"/>')
    for k in range(1, 5):
        d.text(X0 - 12, yv(k) + 4, str(k), size=12, fill=MUTED, anchor="end")
        d.raw(f'<path d="M{X0} {yv(k)} H{X1}" stroke="#E4E8EE" stroke-width="1"/>')
    d.text(X0 - 50, yv(2.3), "in flight", size=12, fill=MUTED, anchor="middle")
    d.text(X1, Y0 + 26, "time", size=12, fill=MUTED, anchor="end")
    d.raw(f'<path d="M{X0} {yv(4)} H{X1}" stroke="{KINDS["ui"][1]}" stroke-width="1.4" stroke-dasharray="5 5"/>')
    d.label(X1 - 70, yv(4) - 10, "job setting", "ui")
    # back-off windows
    for a, b in ((250, 380), (640, 720)):
        d.rect(a, yv(4.5), b - a, Y0 - yv(4.5), "#FDECEE", "#FDECEE", rx=0, sw=0)
        d.text((a + b) / 2, yv(0.7), "back off", size=11.5, fill=KINDS["warn"][1], anchor="middle", weight=600)
    steps = [(X0, 4), (250, 2), (470, 3), (560, 4), (640, 2), (800, 3), (X1, 3)]
    path = f"M{steps[0][0]} {yv(steps[0][1])}"
    for (xa, ca), (xb, cb) in zip(steps, steps[1:]):
        path += f" H{xb} V{yv(cb)}"
    d.raw(f'<path d="{path}" fill="none" stroke="{KINDS["cds"][1]}" stroke-width="3.5" stroke-linejoin="round"/>')
    for x in (250, 640):
        d.raw(f'<g stroke="{KINDS["warn"][1]}" stroke-width="3.5" stroke-linecap="round">'
              f'<path d="M{x - 7} {yv(4) - 7} l14 14 M{x + 7} {yv(4) - 7} l-14 14"/></g>')
    for x in (400, 430, 460, 490, 520, 550, 740, 770, 800):
        d.raw(f'<circle cx="{x}" cy="{Y0 - 14}" r="5" fill="{KINDS["ok"][1]}"/>')
    d.label(250, yv(4) - 22, "CDS refuses: halve", "warn")
    d.label(515, yv(3) - 14, "3 successes: +1", "ok")
    d.text(400, Y0 + 26, "green dots: requests that went through", size=11.5, fill=MUTED)
    d.text(28, 376, "Back-off starts at 60 s and doubles up to 30 min. You can change the setting while the job runs "
           "(job card, Queue menu).", size=12.5, fill=MUTED)
    d.save("cds-queue")


def storage():
    d = D(960, 470, "Storage: Icechunk first, Zarr at the end",
          "Units are committed as they finish; the layout you chose is applied once, in a local pass.")
    d.text(40, 106, "Committed units", size=13, weight=650, fill=KINDS["store"][1])
    for i in range(6):
        d.rect(40 + i * 30, 116, 24, 24, "#BDEBD1" if i < 4 else "#FFFFFF", KINDS["store"][1], rx=5, sw=1.6)
    d.text(40, 160, "4 of 6: a resumed job continues at 5", size=11.5, fill=MUTED)
    d.arrow([(230, 128), (266, 128)], "store")
    d.box(270, 86, 300, 120, "Icechunk repository", ["<name>.icechunk", "staging layout: one download unit", "of time per chunk, written once"], "store")
    d.arrow([(570, 146), (606, 146)], "proc")
    d.box(610, 86, 320, 120, "Export", ["after the last unit: a local rechunk,", "memory-bounded, aligned to the", "destination's chunks or shards"], "proc")
    d.arrow([(770, 206), (770, 242)], "store")
    d.box(560, 246, 370, 130, "Zarr v3 store: <name>.zarr", ["layout: automatic, time series, maps, custom",
                                                                 "codec and level, optional shards",
                                                                 "float32 / float64, optional bit-rounding",
                                                                 "consolidated metadata, provenance attrs"], "store")
    d.box(40, 246, 470, 130, "Extend an existing store (Earth Data Hub jobs)",
          ["append after (forward) or before (backward) its times",
           "checked first: same variables, area, grid and levels,",
           "no repeated times; the repository is then resized",
           "and the new units are committed as usual"], "edh", size=14)
    d.arrow([(320, 246), (360, 210)], "edh")
    d.text(40, 420, "keep_icechunk = false: the repository is removed after a successful export (keeps disk use low).",
           size=12.5, fill=MUTED)
    d.text(40, 440, "Only Icechunk: no export; open the repository read-only with icechunk and xarray.",
           size=12.5, fill=MUTED)
    d.save("storage")


def chunk_layouts():
    d = D(960, 400, "Chunk layouts: same data, different reads",
          "Each rectangle is one variable (time across, space down); lines are chunk boundaries.")
    panels = [("Time series", 40, "rows", "a point over all years: 1 chunk"),
              ("Maps", 350, "cols", "one map at one date: 1 chunk"),
              ("Automatic", 660, "grid", "balanced; download-aligned")]
    W, H, Y = 260, 170, 110
    pink = "#EF2B89"
    for title, x, kind, read in panels:
        d.text(x, Y - 12, title, size=15, weight=700)
        d.rect(x, Y, W, H, "#F1E8FF", KINDS["proc"][1], rx=6)
        lines = []
        if kind == "rows":
            lines = [f"M{x} {Y + H * k / 6} H{x + W}" for k in range(1, 6)]
            d.rect(x + 3, Y + H * 2 / 6 + 10, W - 6, 6, pink, pink, rx=3, sw=0)
        elif kind == "cols":
            lines = [f"M{x + W * k / 8} {Y} V{Y + H}" for k in range(1, 8)]
            d.rect(x + W * 3 / 8 + 12, Y + 3, 6, H - 6, pink, pink, rx=3, sw=0)
        else:
            lines = [f"M{x + W * k / 4} {Y} V{Y + H}" for k in range(1, 4)] + [f"M{x} {Y + H * k / 3} H{x + W}" for k in range(1, 3)]
        d.raw(f'<path d="{" ".join(lines)}" stroke="{KINDS["proc"][1]}" stroke-width="1.6"/>')
        d.text(x, Y + H + 22, "time →", size=11.5, fill=MUTED)
        d.text(x + W, Y + H + 22, read, size=12, fill=pink if kind != "grid" else MUTED, anchor="end", weight=600)
    d.text(40, 340, "The download costs the same either way. Icechunk always uses the automatic layout, so each chunk is",
           size=12.5, fill=MUTED)
    d.text(40, 358, "written exactly once while units arrive; the export rechunks to the layout you picked. The form shows",
           size=12.5, fill=MUTED)
    d.text(40, 376, "each layout's chunk size, chunk count and the cost of reading a time series or a map.",
           size=12.5, fill=MUTED)
    d.save("chunk-layouts")


def remote_connect():
    d = D(960, 590, "Connecting to a server",
          "What the app does over one SSH connection (asyncssh: no ssh binary needed).")
    L, R = 200, 740
    d.box(L - 150, 80, 300, 48, "Your computer: edh-dl app", [], "ui", size=14.5)
    d.box(R - 150, 80, 300, 48, "Server", [], "proc", size=14.5)
    for x, k in ((L, "ui"), (R, "proc")):
        d.raw(f'<path d="M{x} 128 V530" stroke="{KINDS[k][1]}" stroke-width="2" stroke-dasharray="4 6"/>')
    steps = [
        ("→", "connect: ~/.ssh/config, ProxyJump, key, agent or password", "ui"),
        ("→", "unknown host key? you check the fingerprint once", "ui"),
        ("→", "look for venv-<deps id> in ~/.local/share/edh-dl", "proc"),
        ("→", "missing: create it with uv (or python3 -m venv), once", "proc"),
        ("→", "upload agent.zip, only when the build id changed", "proc"),
        ("→", "start: python -m edh_downloader agent", "proc"),
        ("←", "hello: version and capabilities", "ok"),
        ("⇄", "JSON requests and answers: keys, plans, jobs, logs", "cds"),
    ]
    for i, (dirn, text, k) in enumerate(steps):
        y = 168 + i * 46
        d.num(L - 30, y, i + 1, k)
        if dirn == "→":
            d.arrow([(L, y), (R - 4, y)], k)
        elif dirn == "←":
            d.arrow([(R, y), (L + 4, y)], k)
        else:
            d.raw(f'<path d="M{L + 4} {y} L{R - 4} {y}" stroke="{KINDS[k][1]}" stroke-width="2.2" '
                  f'marker-start="url(#a-{k})" marker-end="url(#a-{k})"/>')
        d.label((L + R) / 2, y - 8, text, k, 12)
    d.text(28, 548, "Closing the tab ends the agent. Download jobs are separate detached processes on the server and "
           "keep running.", size=12.5, fill=MUTED)
    d.text(28, 566, "Without the app: edh-dl launcher sh | ps1 prints a script that opens the same UI through a plain "
           "ssh tunnel.", size=12.5, fill=MUTED)
    d.save("remote-connect")


def release_update():
    d = D(960, 400, "Releases and self-update", "From a push on main to the new version on your computer.")
    d.box(30, 90, 200, 86, "Push to main", ["version = major.minor", "+ CI run number"], "ui")
    d.arrow([(230, 133), (266, 133)])
    d.box(270, 90, 330, 86, "CI build (Linux, macOS, Windows)", ["PyInstaller single file", "with a .sha256 next to it"], "proc")
    d.arrow([(600, 133), (636, 133)])
    d.box(640, 90, 290, 86, "GitHub release", ["executables, checksums,", "notes from the commits"], "store")
    d.arrow([(785, 176), (785, 206), (115, 206), (115, 226)], "store", dash=True)
    d.box(30, 230, 200, 96, "edh-dl starts", ["never delayed:", "the check runs alongside"], "ui")
    d.arrow([(230, 278), (256, 278)])
    d.box(260, 230, 210, 96, "Newer release?", ["asks GitHub for the", "latest one, in background"], "ext")
    d.arrow([(470, 278), (496, 278)])
    d.box(500, 230, 200, 96, "Download + verify", ["SHA-256 must match", "else nothing changes"], "proc")
    d.arrow([(700, 278), (726, 278)], "ok")
    d.box(730, 230, 200, 96, "Swap the executable", ["the running one goes on;", "new version next start"], "ok")
    d.text(30, 360, "Only the packaged executable updates itself (not source or uv tool installs, not +local builds).",
           size=12.5, fill=MUTED)
    d.text(30, 378, "Opt out with --no-update or EDH_DL_NO_UPDATE=1; edh-dl update does it on demand.",
           size=12.5, fill=MUTED)
    d.save("release-update")


if __name__ == "__main__":
    for f in (overview, where_it_runs, job_states, edh_engine, edh_units, cds_engine, cds_ledger, cds_packing,
              cds_queue, storage, chunk_layouts, remote_connect, release_update):
        f()
    print("diagrams written to", OUT)
