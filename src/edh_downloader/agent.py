"""``edh-dl agent``: the work side of a connection. Reads JSON requests on stdin, answers on stdout, one per line.

Started over SSH by the app on your computer; it holds the keys, quota, datasets, jobs and output stores of this
machine. Requests run in threads (a long download sample does not block the others). The process ends when the
connection closes.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import service


def encode(o):
    """json ``default``: numpy scalars/arrays, paths, sets, anything else as text."""
    try:
        import numpy as np

        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def dumps(o) -> str:
    return json.dumps(o, default=encode, separators=(",", ":"), allow_nan=True)


def main():
    out = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol
    lock = threading.Lock()

    def send(obj):
        line = dumps(obj)
        with lock:
            out.write(line + "\n")
            out.flush()

    def handle(req):
        rid = req["id"]
        try:
            fn = service.METHODS[req["m"]]
            kw = dict(req.get("a") or {})
            if "emit" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                kw["emit"] = lambda ev: send({"id": rid, "ev": ev})
            send({"id": rid, "ok": True, "r": fn(**kw)})
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            send({"id": rid, "ok": False, "e": str(e) or type(e).__name__, "t": type(e).__name__})

    send({"hello": service.hello()})
    with ThreadPoolExecutor(max_workers=16) as pool:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict) or "id" not in req:
                    raise ValueError("not a request")
            except ValueError:  # a stray line must not end the session
                continue
            pool.submit(handle, req)
    os._exit(0)
