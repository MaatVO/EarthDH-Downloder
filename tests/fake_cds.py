"""A fake Climate Data Store for tests: catalogue (real forms and constraints from tests/data/cds), account,
costing, constraints, a job queue with delays, a result cache, throttling, failures, expiry and result downloads
with HTTP Range. Behaviour follows what the live API did on 2026-11-12 (see sources/cds/client.py)."""

import asyncio
import hashlib
import itertools
import json
import threading
import time
import uuid
from pathlib import Path

from aiohttp import web

DATA = Path(__file__).parent / "data" / "cds"
TOKEN = "cds-test-token"
THROTTLE_TEXT = "Number of API queued requests for this dataset is temporarily limited"


def _canon(inputs: dict) -> str:
    def v(k, x):
        if isinstance(x, list):
            x = [str(i) for i in x]
            return x if k in ("area", "grid", "location") else sorted(set(x))
        return x
    return json.dumps({k: v(k, inputs[k]) for k in sorted(inputs)}, sort_keys=True)


class FakeCDS:
    def __init__(self):
        self.url = None
        self.api = None
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.Lock()):
            self.jobs: dict[str, dict] = {}
            self.order: list[str] = []  # job ids, oldest first
            self.cache: dict[str, str] = {}  # canonical request -> result file name
            self.files: dict[str, bytes] = {}
            self.calls: list[tuple[str, str]] = []
            self.submissions = 0
            self.queue_delay = 0.05  # seconds accepted before running
            self.run_delay = 0.05  # seconds running before done
            self.max_active = 100  # queued + running jobs per user
            self.throttle_mode = "http"  # over max_active: "http" (429 on submit) or "job" (job fails)
            self.fail_requests: set[str] = set()  # canonical requests whose jobs fail
            self.expire_results = False
            self.truncate_next_download = False
            self.fail_next_api_calls = 0  # 503s before the next API calls succeed
            self.ranges: list[str] = []  # Range headers seen on downloads
            self.licences = [{"id": "cc-by", "revision": 1}, {"id": "licence-to-use-copernicus-products",
                                                                "revision": 12}]
            self.limits = {"reanalysis-era5-pressure-levels": 60000.0, "reanalysis-cerra-single-levels": 90000.0}
            self.file_size = 2000
            self.cost_factor = 1.0  # > 1: the server counts more fields than the client expects

    # ------------------------------------------------------------ helpers
    def _status(self, j: dict) -> str:
        if j["status"] in ("failed", "dismissed", "successful"):
            return j["status"]
        now = time.monotonic()
        if now >= j["t"] + self.queue_delay + self.run_delay:
            if j["fail"]:
                j["status"] = "failed"
            else:
                j["status"] = "successful"
                self._finish(j)
            j["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        elif now >= j["t"] + self.queue_delay:
            j["status"] = "running"
            j.setdefault("started", time.strftime("%Y-%m-%dT%H:%M:%S"))
        return j["status"]

    def _finish(self, j: dict):
        key = j["canon"]
        if key not in self.cache:
            name = hashlib.sha1(key.encode()).hexdigest() + ".grib"
            body = (hashlib.sha256(key.encode()).digest() * (self.file_size // 32 + 1))[: self.file_size]
            self.files[name] = body
            self.cache[key] = name
        j["file"] = self.cache[key]

    def _view(self, j: dict, request: bool = False) -> dict:
        st = self._status(j)
        d = {"processID": j["dataset"], "type": "process", "jobID": j["id"], "status": st, "created": j["created"],
             "started": j.get("started"), "finished": j.get("finished"), "updated": j["created"],
             "links": [], "metadata": {"origin": "api", "qos": {"status": {}}}}
        if st == "failed":
            d["metadata"]["results"] = {"type": "job results failed", "title": "job results failed", "status": 400,
                                        "traceback": j["fail"]}
        if request:
            d["metadata"]["request"] = {"ids": j["inputs"]}
        return d

    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if self._status(j) in ("accepted", "running"))

    # ------------------------------------------------------------ handlers
    def handlers(self):
        def auth(req):
            if req.headers.get("PRIVATE-TOKEN") != TOKEN:
                raise web.HTTPUnauthorized(text=json.dumps({"title": "not authorised"}))

        def flaky():
            if self.fail_next_api_calls > 0:
                self.fail_next_api_calls -= 1
                raise web.HTTPServiceUnavailable(text=json.dumps({"title": "busy"}))

        def fixture(name):
            p = DATA / name
            if not p.exists():
                raise web.HTTPNotFound(text=json.dumps({"title": "not found"}))
            return web.Response(body=p.read_bytes(), content_type="application/json")

        async def collections(req):
            self.calls.append(("GET", req.path))
            return fixture("collections.json")

        async def form(req):
            self.calls.append(("GET", req.path))
            return fixture(f"form_{req.match_info['id']}.json")

        async def constraints_json(req):
            self.calls.append(("GET", req.path))
            return fixture(f"constraints_{req.match_info['id']}.json")

        async def account(req):
            self.calls.append(("GET", req.path))
            auth(req)
            return web.json_response({"id": 1})

        async def licences(req):
            self.calls.append(("GET", req.path))
            auth(req)
            return web.json_response({"licences": self.licences})

        async def costing(req):
            self.calls.append(("POST", req.path))
            auth(req)
            flaky()
            inputs = (await req.json())["inputs"]
            cost = 1
            # like CDS: every list counts, except area/location (a grid list does!); dates count only when real
            dates = ("year", "month", "day")
            for k, v in inputs.items():
                if isinstance(v, list) and k not in ("area", "location") + dates:
                    cost *= len(v)
            if all(isinstance(inputs.get(k), list) for k in dates):
                import calendar
                cost *= sum(1 for y in inputs["year"] for m in inputs["month"] for d in inputs["day"]
                            if int(d) <= calendar.monthrange(int(y), int(m))[1])
            else:
                for k in dates:
                    if isinstance(inputs.get(k), list):
                        cost *= len(inputs[k])
            return web.json_response({"id": "size", "cost": float(cost) * self.cost_factor,
                                      "limit": self.limits.get(req.match_info["id"], 121000.0)})

        async def constraints(req):
            self.calls.append(("POST", req.path))
            ds = req.match_info["id"]
            table = json.loads((DATA / f"constraints_{ds}.json").read_text())
            sel = (await req.json())["inputs"]
            out = {}
            for f in set(itertools.chain.from_iterable(table)):
                vals = set()
                for e in table:
                    if all(set(map(str, e[g])) & set(map(str, v if isinstance(v, list) else [v]))
                           for g, v in sel.items() if g != f and g in e):
                        vals |= set(e.get(f, []))
                out[f] = sorted(vals)
            return web.json_response(out)

        async def execution(req):
            self.calls.append(("POST", req.path))
            auth(req)
            flaky()
            inputs = (await req.json())["inputs"]
            canon = _canon(inputs)
            with self.lock:
                if canon in self.cache and not self.expire_results:  # served from the cache
                    status = "successful"
                elif self.active() >= self.max_active:
                    if self.throttle_mode == "http":
                        raise web.HTTPTooManyRequests(text=json.dumps({"title": "too many requests",
                                                                       "detail": THROTTLE_TEXT}))
                    status = "throttled"
                else:
                    status = "accepted"
                self.submissions += 1
                jid = str(uuid.uuid4())
                j = {"id": jid, "dataset": req.match_info["id"], "inputs": inputs, "canon": canon,
                     "t": time.monotonic(), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "accepted",
                     "fail": THROTTLE_TEXT if status == "throttled" else
                     ("MARS returned no data" if canon in self.fail_requests else None)}
                if status == "successful":
                    j["status"] = "successful"
                    self._finish(j)
                if status == "throttled":
                    j["status"] = "failed"
                self.jobs[jid] = j
                self.order.append(jid)
                return web.json_response(self._view(j), status=201)

        def get_job(req):
            j = self.jobs.get(req.match_info["jid"])
            if not j or j["status"] == "dismissed":
                raise web.HTTPNotFound(text=json.dumps({"type": "no-such-job", "title": "job not found",
                                                        "detail": f"job {req.match_info['jid']} dismissed"}))
            return j

        async def jobs(req):
            self.calls.append(("GET", req.path))
            auth(req)
            flaky()
            limit = int(req.query.get("limit", 10))
            ids = [i for i in reversed(self.order) if self.jobs[i]["status"] != "dismissed"]
            start = int(req.query.get("cursor", 0))
            page = ids[start:start + limit]
            links = []
            if start + limit < len(ids):
                links.append({"rel": "next", "href": f"{self.api}/retrieve/v1/jobs?limit={limit}&cursor={start + limit}"})
            with self.lock:
                return web.json_response({"jobs": [self._view(self.jobs[i]) for i in page], "links": links,
                                          "metadata": {"totalCount": len(ids)}})

        async def job(req):
            self.calls.append(("GET", req.path))
            auth(req)
            flaky()
            with self.lock:
                j = get_job(req)
                return web.json_response(self._view(j, request=req.query.get("request") == "true"))

        async def results(req):
            self.calls.append(("GET", req.path))
            auth(req)
            with self.lock:
                j = get_job(req)
                st = self._status(j)
                if st == "failed":
                    raise web.HTTPBadRequest(text=json.dumps({"type": "job results failed",
                                                              "title": "job results failed", "traceback": j["fail"]}))
                if st != "successful":
                    raise web.HTTPNotFound(text=json.dumps({"title": "results not ready"}))
                if self.expire_results:
                    raise web.HTTPNotFound(text=json.dumps({"type": "results expired", "title": "results expired",
                                                            "detail": f"results of job {j['id']} expired"}))
                name = j["file"]
                return web.json_response({"asset": {"value": {
                    "type": "application/x-grib", "href": f"{self.url}/files/{name}",
                    "file:checksum": hashlib.md5(self.files[name]).hexdigest(), "file:size": len(self.files[name]),
                    "file:local_path": f"s3://fake/{name}"}}})

        async def delete(req):
            self.calls.append(("DELETE", req.path))
            auth(req)
            with self.lock:
                j = get_job(req)
                j["status"] = "dismissed"
                return web.json_response({"jobID": j["id"], "status": "dismissed"})

        async def files(req):
            if "PRIVATE-TOKEN" in req.headers:
                raise web.HTTPBadRequest(text="the token must not be sent to the result store")
            body = self.files.get(req.match_info["name"])
            if body is None or self.expire_results:
                raise web.HTTPNotFound()
            rng = req.headers.get("Range")
            self.ranges.append(rng)
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            part = body[start:]
            resp = web.StreamResponse(status=206 if rng else 200)
            resp.content_length = len(part)
            await resp.prepare(req)
            if self.truncate_next_download:
                self.truncate_next_download = False
                await resp.write(part[: len(part) // 2])
                req.transport.close()  # connection lost mid-file
                return resp
            await resp.write(part)
            await resp.write_eof()
            return resp

        return [
            web.get("/api/catalogue/v1/collections", collections),
            web.get("/api/catalogue/v1/collections/{id}/form.json", form),
            web.get("/api/catalogue/v1/collections/{id}/constraints.json", constraints_json),
            web.get("/api/profiles/v1/account", account),
            web.get("/api/profiles/v1/account/licences", licences),
            web.post("/api/retrieve/v1/processes/{id}/costing", costing),
            web.post("/api/retrieve/v1/processes/{id}/constraints", constraints),
            web.post("/api/retrieve/v1/processes/{id}/execution", execution),
            web.get("/api/retrieve/v1/jobs", jobs),
            web.get("/api/retrieve/v1/jobs/{jid}", job),
            web.get("/api/retrieve/v1/jobs/{jid}/results", results),
            web.delete("/api/retrieve/v1/jobs/{jid}", delete),
            web.get("/files/{name}", files),
        ]

    def start(self):
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            app = web.Application()
            app.add_routes(self.handlers())
            runner = web.AppRunner(app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, "127.0.0.1", 0)
            loop.run_until_complete(site.start())
            port = site._server.sockets[0].getsockname()[1]
            self.url = f"http://127.0.0.1:{port}"
            self.api = f"{self.url}/api"
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        return self
