"""Async client for the Climate Data Store retrieve API (``/api/retrieve/v1``).

A CDS download is a *job*: the request is submitted, waits in a queue, runs, and its result is a file on a public
object store. What this client relies on was checked against the live API (2026-11-12):

* ``POST processes/{id}/costing`` and ``POST processes/{id}/constraints`` are free and queue nothing; costing
  returns ``{"cost": N, "limit": L}`` (cost = number of fields; ``area`` does not change it). Costing does not
  validate names: use the constraints for that.
* ``POST processes/{id}/execution`` returns 201 with a ``jobID``. An identical request (same inputs) comes back
  already ``successful`` with the same result file: CDS caches results, so requests are always built in one
  canonical form (``canonical``).
* ``GET jobs`` lists jobs (cursor paging); ``GET jobs/{id}?request=true`` echoes the submitted request;
  ``GET jobs/{id}/results`` gives ``asset.value.{href, file:size, file:checksum}`` or 404 "results expired".
* ``DELETE jobs/{id}`` dismisses a job (afterwards: 404).
* Result links are public: the token is never sent to them.
"""

import asyncio
import hashlib
import json
import logging
import math
import random
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

API = "https://cds.climate.copernicus.eu/api"
log = logging.getLogger("edh.cds")

FINAL = {"successful", "failed", "rejected", "dismissed"}
# words CDS uses when it refuses more work from a user for a while (seen as job failures, or HTTP errors)
THROTTLE_HINTS = ("temporarily limited", "too many", "rate limit", "try again later")
ORDERED_FIELDS = {"area", "grid", "location"}  # positional values: never sorted


MAX_REDIRECTS = 5
REDIRECTS = (301, 302, 303, 307, 308)


def origin_of(url: str) -> tuple:
    """(scheme, host, port) of a URL: what decides whether a token may be sent to it."""
    p = urlsplit(url)
    scheme = p.scheme.lower()
    try:
        port = p.port
    except ValueError:
        port = None
    return scheme, (p.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


def same_origin(a: str, b: str) -> bool:
    return origin_of(a) == origin_of(b)


def _retry_after(value) -> float | None:
    """A ``Retry-After`` header in seconds (CDS does not send HTTP dates); None when absent or unreadable."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, x) if math.isfinite(x) else None


def expected_md5(checksum) -> str | None:
    """The MD5 of a ``file:checksum`` value: ``md5:<hex>`` or 32 hex digits. Another format gives None (not checked)."""
    if not isinstance(checksum, str):
        return None
    c = checksum.strip().lower()
    if c.startswith("md5:"):
        c = c[4:].strip()
    return c if re.fullmatch(r"[0-9a-f]{32}", c) else None


MIB = 1 << 20
# part sizes object-store clients use for multipart uploads (MiB), tried when the part count does not settle it
PART_MIB = (5, 8, 10, 15, 16, 25, 32, 50, 64, 100, 128, 256, 512, 1024)


def _part_sizes(size: int, parts: int | None) -> list[int]:
    """Multipart part sizes (bytes) that give ``parts`` parts for a file of ``size`` bytes (all the usual ones
    when the count is unknown), and the smallest whole MiB that does."""
    if parts is None:
        return [m * MIB for m in PART_MIB]
    out = [m * MIB for m in PART_MIB if math.ceil(size / (m * MIB)) == parts]
    if parts > 0:
        smallest = math.ceil(size / parts / MIB) * MIB
        if math.ceil(size / smallest) == parts and smallest not in out:
            out.append(smallest)
    return out


def file_digests(path: Path, part_sizes: list[int]) -> tuple[str, dict[int, str]]:
    """The MD5 of a file and, for each part size, its multipart MD5 (the MD5 of the parts' MD5s, as an object
    store computes the ETag of a multipart upload), in one read. Part sizes must be multiples of 1 MiB."""
    whole = hashlib.md5(usedforsecurity=False)
    parts = {ps: [] for ps in part_sizes}
    cur = {ps: hashlib.md5(usedforsecurity=False) for ps in part_sizes}
    pos = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(MIB), b""):
            whole.update(chunk)
            pos += len(chunk)
            for ps in part_sizes:
                cur[ps].update(chunk)
                if pos % ps == 0:
                    parts[ps].append(cur[ps].digest())
                    cur[ps] = hashlib.md5(usedforsecurity=False)
    out = {}
    for ps in part_sizes:
        if pos % ps or not parts[ps]:
            parts[ps].append(cur[ps].digest())
        out[ps] = hashlib.md5(b"".join(parts[ps]), usedforsecurity=False).hexdigest()
    return whole.hexdigest(), out


def checksum_matches(path: Path, want: str, etag: str | None = None) -> bool:
    """Whether a downloaded file is the one CDS published. Its ``file:checksum`` is the object store's ETag
    without the ``-<parts>`` suffix: the plain MD5 for a file uploaded in one part, the multipart MD5 for a large
    one (seen on a 1.5 GB ERA5 result: ``<md5 of 30 part MD5s>-30``). The ETag of the download, when it carries
    the same hash, tells the number of parts."""
    parts = None
    m = re.fullmatch(r'(?:w/)?"?([0-9a-f]{32})(?:-(\d+))?"?', (etag or "").strip().lower())
    if m and m.group(1) == want:
        if not m.group(2):
            parts = 1
        else:
            parts = int(m.group(2))
    size = Path(path).stat().st_size
    whole, multi = file_digests(path, [] if parts == 1 else _part_sizes(size, parts))
    return want == whole or want in multi.values()


def _range_start(content_range: str | None) -> int | None:
    m = re.match(r"\s*bytes\s+(\d+)-", content_range or "")
    return int(m.group(1)) if m else None


async def send(session, method: str, url: str, api: str, token: str | None, body=None) -> tuple[int, str, str | None]:
    """One request. Redirects are followed by hand (at most MAX_REDIRECTS): the token is sent only to URLs on the
    API's origin, so a redirect to another host never receives it. Returns (status, body text, Retry-After)."""
    hops = 0
    while True:
        headers = {"Accept": "application/json"}
        if token is not None and same_origin(url, api):
            headers["PRIVATE-TOKEN"] = token
        async with session.request(method, url, json=body, headers=headers, allow_redirects=False) as r:
            location = r.headers.get("Location")
            if r.status in REDIRECTS and location:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise CDSError(f"too many redirects from the Climate Data Store (HTTP {r.status})", r.status)
                url = urljoin(url, location)
                if r.status in (301, 302, 303) and method == "POST":
                    method, body = "GET", None
                continue
            return r.status, await r.text(), r.headers.get("Retry-After")


class CDSError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class AuthError(CDSError):
    """The token was rejected: retrying cannot help."""


class Throttled(CDSError):
    """CDS refuses more requests for now (HTTP 429, or a queue limit for the user or the dataset)."""

    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg, status)
        self.retry_after = retry_after


class NoSuchJob(CDSError):
    """The job is unknown or was dismissed."""


class ResultsExpired(CDSError):
    """The job succeeded but its result file is gone: submit the request again (it may come from the cache)."""


class JobFailed(CDSError):
    """The job ended without a result; ``throttled`` when CDS refused it because of a queue limit."""

    @property
    def throttled(self) -> bool:
        return is_throttle(str(self))


class BadRequest(CDSError):
    """CDS refused the request itself (unknown field or value, too large, licence not accepted, ...)."""


def is_throttle(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in THROTTLE_HINTS)


def _canon_value(key: str, v):
    if isinstance(v, (list, tuple)):
        vals = [str(x) for x in v]
        return vals if key in ORDERED_FIELDS else sorted(set(vals))
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return v


def canonical(inputs: dict) -> dict:
    """The one form of a request: keys sorted, list values as sorted unique strings (``area``, ``grid`` and
    ``location`` keep their order), numbers as strings, empty values dropped. Two selections that mean the same
    data give the same request, so CDS can serve the second from its cache."""
    out = {}
    for k in sorted(inputs):
        v = inputs[k]
        if v is None or v == [] or v == "":
            continue
        if k == "grid" and isinstance(v, (list, tuple)):
            # "dx/dy": CDS regrids the same, but counts a list's length as fields (twice the cost, checked live)
            v = "/".join(str(x) for x in v)
        out[k] = _canon_value(k, v)
    return out


def request_hash(dataset: str, inputs: dict) -> str:
    raw = json.dumps([dataset, canonical(inputs)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _message(body) -> str:
    if isinstance(body, dict):
        parts = [body.get("title"), body.get("detail")]
        if isinstance(body.get("traceback"), str):
            parts.append(body["traceback"][-400:])
        return " — ".join(str(p) for p in parts if p) or json.dumps(body)[:400]
    return str(body)[:400]


class CDSClient:
    """One authenticated session. Network errors, 5xx and 429 are retried with exponential backoff and jitter
    (``Retry-After`` honoured); other errors raise at once."""

    def __init__(self, token: str, api: str | None = None, timeout: float = 120, retries: int = 5,
                 backoff: float = 2.0, max_backoff: float = 120.0):
        from . import api as default_api

        self.token = token
        self.api = (api or default_api()).rstrip("/")
        sp = urlsplit(self.api)
        self.origin = f"{sp.scheme}://{sp.netloc}"
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.session: aiohttp.ClientSession | None = None
        self.calls = 0  # HTTP calls made to the API (not downloads)

    async def __aenter__(self):
        # trust_env: HTTPS_PROXY / NO_PROXY from the environment, for the API and the result store alike
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=True,
                                             headers={"User-Agent": "edh-downloader"})
        return self

    async def __aexit__(self, *exc):
        if self.session:
            await self.session.close()

    def _wait(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(self.max_backoff, max(1.0, retry_after))  # Retry-After: 0 still waits a second
        return min(self.max_backoff, self.backoff * 2 ** attempt) * (0.5 + random.random() / 2)

    def _absolute(self, href: str) -> str:
        """The URL for a path or a link. ``http(s)://`` is used as it is, ``/api/...`` is relative to the origin
        (as links from CDS are), other paths are relative to the API base (``/retrieve/v1/...``)."""
        if href.startswith(("http://", "https://")):
            return href
        if href.startswith("/api/"):
            return urljoin(self.origin, href)
        if href.startswith("/"):
            return self.api + href
        return urljoin(self.api + "/", href)

    async def _call(self, method: str, path: str, body=None, auth: bool = True, submitting: bool = False):
        url = self._absolute(path)
        token = self.token if auth else None
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                status, text, retry_after_hdr = await send(self.session, method, url, self.api, token, body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt >= self.retries:
                    raise CDSError(f"could not reach the Climate Data Store: {type(e).__name__}: {e}") from e
                await asyncio.sleep(self._wait(attempt))
                continue
            try:
                data, is_json = (json.loads(text) if text else {}), True
            except ValueError:
                data, is_json = text, False
            if status < 400:
                if not is_json:
                    raise CDSError("unexpected response from the CDS (not JSON)", status)
                return status, data
            msg = _message(data)
            if status in (401, 403):
                raise AuthError(f"the Climate Data Store refused the token (HTTP {status}): {msg}", status)
            ra = _retry_after(retry_after_hdr)
            if status == 429 or (status == 503 and is_throttle(msg)) or (submitting and is_throttle(msg)):
                # a refused submission goes straight back: the request queue decides when to try again
                if submitting or attempt >= self.retries:
                    raise Throttled(f"the Climate Data Store asks to slow down (HTTP {status}): {msg}", status, ra)
                await asyncio.sleep(self._wait(attempt, ra))
                continue
            if status >= 500:
                if attempt >= self.retries:
                    raise CDSError(f"Climate Data Store error (HTTP {status}): {msg}", status)
                await asyncio.sleep(self._wait(attempt, ra))
                continue
            if status == 404:
                if "expired" in msg.lower():
                    raise ResultsExpired(msg, status)
                raise NoSuchJob(msg, status)
            raise BadRequest(f"the Climate Data Store refused the request (HTTP {status}): {msg}", status)
        raise CDSError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------ account
    async def account(self) -> dict:
        return (await self._call("GET", "/profiles/v1/account"))[1]

    async def licences(self) -> set[tuple[str, int]]:
        """The licences this account has accepted, as (id, revision)."""
        d = (await self._call("GET", "/profiles/v1/account/licences"))[1]
        return {(x["id"], int(x.get("revision", 0))) for x in d.get("licences", [])}

    # ------------------------------------------------------------ free checks
    async def costing(self, dataset: str, inputs: dict) -> dict:
        d = (await self._call("POST", f"/retrieve/v1/processes/{dataset}/costing",
                              {"inputs": canonical(inputs)}))[1]
        return {"cost": float(d.get("cost", 0)), "limit": float(d["limit"]) if d.get("limit") is not None else None}

    async def constraints(self, dataset: str, inputs: dict) -> dict:
        return (await self._call("POST", f"/retrieve/v1/processes/{dataset}/constraints",
                                 {"inputs": canonical(inputs)}))[1]

    # ------------------------------------------------------------ jobs
    async def submit(self, dataset: str, inputs: dict) -> dict:
        """Queue a request; returns the job (``jobID``, ``status``: often ``accepted``, ``successful`` when CDS
        serves it from its cache)."""
        status, d = await self._call("POST", f"/retrieve/v1/processes/{dataset}/execution",
                                     {"inputs": canonical(inputs)}, submitting=True)
        if not isinstance(d, dict) or not d.get("jobID"):
            raise CDSError(f"unexpected answer to a submission: {str(d)[:300]}", status)
        return d

    async def job(self, job_id: str, request: bool = False) -> dict:
        q = "?request=true" if request else ""
        return (await self._call("GET", f"/retrieve/v1/jobs/{job_id}{q}"))[1]

    async def jobs(self, limit: int = 100, max_pages: int = 20) -> list[dict]:
        """The account's jobs, newest first (at most ``limit * max_pages``)."""
        out, url = [], f"/retrieve/v1/jobs?limit={limit}"
        for _ in range(max_pages):
            d = (await self._call("GET", url))[1]
            page = d.get("jobs", [])
            out += page
            nxt = next((l["href"] for l in d.get("links", []) if l.get("rel") == "next"), None)
            if not page or not nxt or len(page) < limit:
                break
            url = self._absolute(nxt)
        return out

    async def statuses(self, job_ids: list[str], limit: int = 100) -> dict[str, dict]:
        """Status of many jobs with as few calls as possible: the job list first (one call covers ``limit``
        recent jobs), then one call for each job not found there. Unknown or dismissed jobs are left out."""
        want = set(job_ids)
        found: dict[str, dict] = {}
        if not want:
            return found
        out, url = [], f"/retrieve/v1/jobs?limit={limit}"
        for _ in range(max(1, -(-len(want) // limit)) + 1):
            d = (await self._call("GET", url))[1]
            page = d.get("jobs", [])
            for j in page:
                if j.get("jobID") in want:
                    found[j["jobID"]] = j
            nxt = next((l["href"] for l in d.get("links", []) if l.get("rel") == "next"), None)
            if len(found) == len(want) or not page or not nxt or len(page) < limit:
                break
            url = self._absolute(nxt)
        for jid in want - set(found):
            try:
                found[jid] = await self.job(jid)
            except NoSuchJob:
                pass
        return found

    async def results(self, job_id: str) -> dict:
        """The result file of a successful job: ``{href, file:size, file:checksum, type}``."""
        try:
            d = (await self._call("GET", f"/retrieve/v1/jobs/{job_id}/results"))[1]
        except BadRequest as e:  # a failed job answers its results with its error
            raise JobFailed(str(e), e.status) from None
        v = (d.get("asset") or {}).get("value") if isinstance(d, dict) else None
        if not v or not v.get("href"):
            raise JobFailed(f"job {job_id} has no result: {_message(d)}")
        return v

    async def failure(self, job_id: str) -> str:
        """Why a job failed, as CDS tells it."""
        try:
            d = await self.job(job_id)
        except CDSError as e:
            return str(e)
        res = (d.get("metadata") or {}).get("results")
        return _message(res) if res else f"job {job_id} {d.get('status')}"

    async def delete(self, job_id: str) -> None:
        try:
            await self._call("DELETE", f"/retrieve/v1/jobs/{job_id}")
        except NoSuchJob:
            pass

    # ------------------------------------------------------------ download
    async def download(self, href: str, dest: Path, size: int | None = None, progress=None,
                       block: int = 1 << 20, checksum: str | None = None) -> int:
        """Fetch a result file to ``dest``.

        A partial ``dest.part`` from an interrupted run is continued with an HTTP Range request, but only when the
        server answers with a range that starts where the file stops; otherwise the file is fetched from the start.
        Connection errors, 5xx and 429 are retried with backoff (``Retry-After`` honoured). The size is checked
        against the one CDS announced; ``checksum`` (CDS's ``file:checksum``: ``md5:<hex>`` or 32 hex digits; any
        other format is not checked) is verified too. A file that fails a check is deleted and CDSError is raised.
        The token is not sent."""
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        failures = 0
        etag = None
        while True:
            have = part.stat().st_size if part.exists() else 0
            if size is not None and have > size:
                part.unlink()
                have = 0
            if size is not None and have == size:
                break
            headers = {"Range": f"bytes={have}-"} if have else {}
            ra, err = None, None
            try:
                async with self.session.get(href, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=None, sock_read=300)) as r:
                    if r.status in (403, 404):
                        raise ResultsExpired(f"result file is gone (HTTP {r.status})", r.status)
                    if r.status == 429 or r.status >= 500:
                        ra, err = _retry_after(r.headers.get("Retry-After")), f"download failed (HTTP {r.status})"
                    elif r.status not in (200, 206):
                        raise CDSError(f"download failed (HTTP {r.status})", r.status)
                    elif r.status == 206 and have and _range_start(r.headers.get("Content-Range")) != have:
                        # not the range that continues this file: start again (the next pass asks for all of it)
                        part.unlink(missing_ok=True)
                        continue
                    else:
                        etag = r.headers.get("ETag") or etag
                        mode = "ab" if r.status == 206 else "wb"
                        with open(part, mode) as f:
                            async for chunk in r.content.iter_chunked(block):
                                f.write(chunk)
                                if progress:
                                    progress(len(chunk))
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                err = f"download failed: {type(e).__name__}: {e}"
            failures += 1
            if failures > self.retries:
                raise CDSError(err or "download failed")
            await asyncio.sleep(self._wait(failures - 1, ra))
        got = part.stat().st_size
        if size is not None and got != size:
            part.unlink(missing_ok=True)
            raise CDSError(f"download incomplete: {got} of {size} bytes")
        want = expected_md5(checksum)
        if want is not None and not await asyncio.to_thread(checksum_matches, part, want, etag):
            part.unlink(missing_ok=True)
            raise CDSError("checksum mismatch: the downloaded file is not the one CDS published")
        part.replace(dest)
        return got
