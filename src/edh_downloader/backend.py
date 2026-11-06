"""How the UI reaches the machine that does the work: ``Local`` (this computer) or ``Remote`` (an agent over SSH).

Both expose ``await call(method, **kwargs)`` with the functions of ``service`` and give the same JSON back.
"""

import asyncio
import json

from . import service
from .agent import dumps


class BackendError(Exception):
    """A request failed on the machine that does the work (the message is meant for the user)."""


class Local:
    name = "This computer"

    def __init__(self):
        self.info = service.hello()
        self.closed = False

    async def call(self, method: str, on_event=None, **kw):
        fn = service.METHODS[method]
        loop = asyncio.get_running_loop()
        if on_event and "emit" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
            kw["emit"] = lambda ev: loop.call_soon_threadsafe(on_event, json.loads(dumps(ev)))
        try:
            r = await asyncio.to_thread(fn, **kw)
        except Exception as e:  # noqa: BLE001
            raise BackendError(str(e) or type(e).__name__) from e
        return json.loads(dumps(r))

    async def close(self):
        self.closed = True


class Remote:
    """Speaks the agent protocol over an asyncssh process."""

    def __init__(self, name: str, proc, info: dict):
        self.name, self.proc, self.info = name, proc, info
        self.closed = False
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: dict[int, object] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("id")
                if "ev" in msg:
                    cb = self._events.get(rid)
                    if cb:
                        cb(msg["ev"])
                elif (fut := self._pending.pop(rid, None)) and not fut.done():
                    if msg.get("ok"):
                        fut.set_result(msg["r"])
                    else:
                        fut.set_exception(BackendError(msg.get("e", "request failed")))
        finally:
            self.closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BackendError("the connection to the server was lost"))
            self._pending.clear()

    async def call(self, method: str, on_event=None, **kw):
        if self.closed:
            raise BackendError("the connection to the server is closed")
        self._seq += 1
        rid = self._seq
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        if on_event:
            self._events[rid] = on_event
        try:
            self.proc.stdin.write(dumps({"id": rid, "m": method, "a": kw}) + "\n")
            return await fut
        finally:
            self._events.pop(rid, None)

    async def close(self):
        self.closed = True
        try:
            self.proc.stdin.write_eof()
            self.proc.close()
        except Exception:  # noqa: BLE001
            pass
        self._reader.cancel()
