"""API key storage.

Keys live in ``<config>/keys.json`` (permissions 0600). A key can also come from ``~/.netrc``
(entry ``machine data.earthdatahub.destine.eu``). Keys are never written to logs, job files or outputs:
everything else refers to a key by its *name* and a short non-reversible *fingerprint*.
"""

import hashlib
import netrc
import os
from dataclasses import dataclass

from .paths import config_dir
from .util import read_json, write_json_atomic

DATA_HOST = "data.earthdatahub.destine.eu"
NETRC_NAME = "netrc"
DEFAULT_QUOTA = 500_000  # free DestinE access: requests per calendar month


@dataclass
class ApiKey:
    name: str
    secret: str
    quota: int = DEFAULT_QUOTA
    source: str = "stored"  # "stored" or "netrc"

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.secret)


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _path():
    return config_dir() / "keys.json"


def _load() -> dict:
    """keys.json, always with the right shape. A damaged file is kept aside (keys.json.bad) instead of crashing
    every screen, or being silently overwritten by the next change."""
    path = _path()
    d = read_json(path, None)
    if d is not None and not (isinstance(d, dict) and isinstance(d.get("keys", {}), dict)
                              and all(isinstance(v, dict) and isinstance(v.get("secret"), str)
                                      for v in d.get("keys", {}).values())):
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        d = None
    elif d is None and path.exists() and path.stat().st_size:  # not JSON at all
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
    d = d or {}
    d.setdefault("default", None)
    d.setdefault("keys", {})
    return d


def _save(d: dict) -> None:
    write_json_atomic(_path(), d, mode=0o600)


def netrc_secret() -> str | None:
    try:
        auth = netrc.netrc(os.environ.get("NETRC")).authenticators(DATA_HOST)
    except (FileNotFoundError, netrc.NetrcParseError):
        return None
    return auth[2] if auth and auth[2] else None


def list_keys() -> list[ApiKey]:
    d = _load()
    out = [ApiKey(n, v["secret"], v.get("quota", DEFAULT_QUOTA)) for n, v in d["keys"].items()]
    sec = netrc_secret()
    if sec and NETRC_NAME not in d["keys"]:
        out.append(ApiKey(NETRC_NAME, sec, d.get("netrc_quota", DEFAULT_QUOTA), source="netrc"))
    return out


def get_key(name: str | None = None) -> ApiKey:
    keys = {k.name: k for k in list_keys()}
    if not keys:
        raise KeyError(
            "no API key configured: get a free key at https://earthdatahub.destine.eu/quota-api-keys "
            "then add it in the Keys page or with `edh-dl keys add NAME`"
        )
    if name is None:
        name = _load().get("default") or next(iter(keys))
        if name not in keys:
            name = next(iter(keys))
    if name not in keys:
        raise KeyError(f"unknown API key {name!r}; known: {', '.join(keys)}")
    return keys[name]


def default_name() -> str | None:
    try:
        return get_key().name
    except KeyError:
        return None


def add_key(name: str, secret: str, quota: int = DEFAULT_QUOTA, make_default: bool = False) -> ApiKey:
    name, secret = name.strip(), secret.strip()
    if not name or not secret:
        raise ValueError("name and key must be non-empty")
    if name == NETRC_NAME:
        raise ValueError(f"'{NETRC_NAME}' is reserved for the key read from ~/.netrc")
    if int(quota) < 1:
        raise ValueError("the monthly quota must be a positive number")
    d = _load()
    d["keys"][name] = {"secret": secret, "quota": int(quota)}
    if make_default or not d.get("default"):
        d["default"] = name
    _save(d)
    return ApiKey(name, secret, quota)


def remove_key(name: str) -> None:
    d = _load()
    d["keys"].pop(name, None)
    if d.get("default") == name:
        d["default"] = next(iter(d["keys"]), None)
    _save(d)


def set_default(name: str) -> None:
    get_key(name)
    d = _load()
    d["default"] = name
    _save(d)


def set_quota(name: str, quota: int) -> None:
    if int(quota) < 1:
        raise ValueError("the monthly quota must be a positive number")
    d = _load()
    if name in d["keys"]:
        d["keys"][name]["quota"] = int(quota)
    elif name == NETRC_NAME:
        d["netrc_quota"] = int(quota)
    else:
        raise KeyError(name)
    _save(d)


async def validate(secret: str) -> tuple[bool, str]:
    """Check a key against EDH's private test dataset (costs one request)."""
    import aiohttp

    url = f"https://{DATA_HOST}/private/test-dataset-v1.zarr/zarr.json"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url, headers={"Authorization": f"Bearer {secret}"}) as r:
                if r.status == 200:
                    return True, "key accepted by Earth Data Hub"
                if r.status in (401, 403):
                    return False, f"key rejected (HTTP {r.status})"
                return False, f"unexpected HTTP {r.status}"
    except Exception as e:  # network problems
        return False, f"could not reach Earth Data Hub: {type(e).__name__}: {e}"
