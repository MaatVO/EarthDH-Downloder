"""API key storage, for every data source.

Keys live in ``<config>/keys.json`` (permissions 0600). Each key belongs to one *provider* (a data source id
from ``sources``: Earth Data Hub or the Climate Data Store), and each provider has its own default key.
A key can also come from a file other tools already use: ``~/.netrc`` (entry ``machine
data.earthdatahub.destine.eu``) for Earth Data Hub, ``~/.cdsapirc`` (or ``$CDSAPI_RC``, or the
``CDSAPI_URL``/``CDSAPI_KEY`` variables) for the Climate Data Store.

Keys are never written to logs, job files or outputs: everything else refers to a key by its *name* and a short
non-reversible *fingerprint*.

``keys.json`` written before sources existed has no ``provider`` on its keys: they are Earth Data Hub keys, and its
``default`` stays the Earth Data Hub default (older versions of the app keep reading it).
"""

import hashlib
import netrc
import os
from dataclasses import dataclass
from pathlib import Path

from .paths import config_dir
from .sources import CDS, EDH
from .sources import get as get_source
from .util import read_json, write_json_atomic

DATA_HOST = "data.earthdatahub.destine.eu"
NETRC_NAME = "netrc"
CDSAPIRC_NAME = "cdsapirc"
RESERVED = {NETRC_NAME: EDH, CDSAPIRC_NAME: CDS}
CDS_HOST = "cds.climate.copernicus.eu"
DEFAULT_QUOTA = 500_000  # free DestinE access: requests per calendar month


@dataclass
class ApiKey:
    name: str
    secret: str
    quota: int | None = DEFAULT_QUOTA  # monthly request allowance; None for sources without one (CDS)
    source: str = "stored"  # where the key comes from: "stored", "netrc" or "cdsapirc"
    provider: str = EDH  # the data source it opens (see ``sources``)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.secret)


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _path():
    return config_dir() / "keys.json"


def _valid(d) -> bool:
    return (isinstance(d, dict) and isinstance(d.get("keys", {}), dict)
            and all(isinstance(v, dict) and isinstance(v.get("secret"), str) for v in d.get("keys", {}).values()))


def _load() -> dict:
    """keys.json, always with the right shape. A damaged file is kept aside (keys.json.bad) instead of crashing
    every screen, or being silently overwritten by the next change."""
    path = _path()
    d = read_json(path, None)
    if d is not None and not _valid(d):
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
    if not isinstance(d.get("defaults"), dict):
        d["defaults"] = {}
    return d


def _save(d: dict) -> None:
    write_json_atomic(_path(), d, mode=0o600)


def _provider_of(entry: dict) -> str:
    return entry.get("provider") or EDH


def _get_default(d: dict, provider: str) -> str | None:
    return d.get("default") if provider == EDH else d["defaults"].get(provider)


def _set_default(d: dict, provider: str, name: str | None) -> None:
    if provider == EDH:
        d["default"] = name  # where older versions look for it
    else:
        d["defaults"][provider] = name


# ------------------------------------------------------------------ keys other tools already use

def netrc_secret() -> str | None:
    try:
        auth = netrc.netrc(os.environ.get("NETRC")).authenticators(DATA_HOST)
    except (FileNotFoundError, netrc.NetrcParseError):
        return None
    return auth[2] if auth and auth[2] else None


def cdsapirc_secret() -> str | None:
    """The Climate Data Store token the ``cdsapi`` package would use: ``CDSAPI_KEY`` (with ``CDSAPI_URL`` when set,
    which may be left out), else the ``url:``/``key:`` lines of ``$CDSAPI_RC`` or ``~/.cdsapirc``. Only for the
    current CDS (not another store).

    The legacy form ``UID:APIKEY`` is accepted: the ``UID:`` part is dropped only when it is all digits (the old
    numeric user id); any other key is returned as it is."""
    url, key = os.environ.get("CDSAPI_URL"), os.environ.get("CDSAPI_KEY")
    if not key:
        path = Path(os.environ.get("CDSAPI_RC") or "~/.cdsapirc").expanduser()
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return None
        conf = {}
        for line in text.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                conf[k.strip()] = v.strip()
        url, key = conf.get("url"), conf.get("key")
    key = (key or "").strip()
    uid, sep, rest = key.partition(":")
    if sep and uid.isascii() and uid.isdigit() and rest.strip():
        key = rest.strip()
    if not key or (url and CDS_HOST not in url):
        return None
    return key


def list_keys(provider: str | None = None) -> list[ApiKey]:
    """Every known key, or only those of one data source."""
    d = _load()
    out = []
    for n, v in d["keys"].items():
        p = _provider_of(v)
        try:
            has_quota = get_source(p).has_quota
        except (ValueError, TypeError):  # a provider this version does not know: listed, not usable
            p, has_quota = "unknown", False
        out.append(ApiKey(n, v["secret"], v.get("quota", DEFAULT_QUOTA) if has_quota else None, provider=p))
    sec = netrc_secret()
    if sec and NETRC_NAME not in d["keys"]:
        out.append(ApiKey(NETRC_NAME, sec, d.get("netrc_quota", DEFAULT_QUOTA), source="netrc", provider=EDH))
    sec = cdsapirc_secret()
    if sec and CDSAPIRC_NAME not in d["keys"]:
        out.append(ApiKey(CDSAPIRC_NAME, sec, None, source="cdsapirc", provider=CDS))
    return [k for k in out if provider is None or k.provider == provider]


def find_key(name: str) -> ApiKey:
    """A key by name, whatever its source."""
    for k in list_keys():
        if k.name == name:
            return k
    raise KeyError(f"unknown API key {name!r}")


def get_key(name: str | None = None, provider: str = EDH) -> ApiKey:
    """The named key, or the provider's default; refuses a key of another data source."""
    src = get_source(provider)
    keys = {k.name: k for k in list_keys(provider)}
    if name is not None and name not in keys:
        try:
            other = find_key(name)
        except KeyError:
            other = None
        if other is not None:
            raise KeyError(f"API key {name!r} is a {get_source(other.provider).title} key, "
                           f"not a {src.title} key")
    if not keys:
        raise KeyError(
            f"no {src.title} API key configured: get one at {src.key_url} "
            "then add it in the Keys page or with `edh-dl keys add NAME`"
            + (f" --source {provider}" if provider != EDH else "")
        )
    if name is None:
        name = _get_default(_load(), provider) or next(iter(keys))
        if name not in keys:
            name = next(iter(keys))
    if name not in keys:
        raise KeyError(f"unknown API key {name!r}; known: {', '.join(keys)}")
    return keys[name]


def default_name(provider: str = EDH) -> str | None:
    try:
        return get_key(provider=provider).name
    except KeyError:
        return None


def add_key(name: str, secret: str, quota: int | None = DEFAULT_QUOTA, make_default: bool = False,
            provider: str = EDH) -> ApiKey:
    name, secret = name.strip(), secret.strip()
    src = get_source(provider)
    if not name or not secret:
        raise ValueError("name and key must be non-empty")
    if name in RESERVED:
        raise ValueError(f"'{name}' is reserved for the key read from ~/.{name}")
    d = _load()
    if name in d["keys"] and _provider_of(d["keys"][name]) != provider:
        raise ValueError(f"a {get_source(_provider_of(d['keys'][name])).title} key is already called {name!r}; "
                         "choose another name")
    entry = {"secret": secret, "provider": provider}
    if src.has_quota:
        quota = DEFAULT_QUOTA if quota is None else quota
        if int(quota) < 1:
            raise ValueError("the monthly quota must be a positive number")
        entry["quota"] = int(quota)
    d["keys"][name] = entry
    if make_default or not _get_default(d, provider):
        _set_default(d, provider, name)
    _save(d)
    return ApiKey(name, secret, entry.get("quota"), provider=provider)


def remove_key(name: str) -> None:
    d = _load()
    entry = d["keys"].pop(name, None)
    if entry is not None:
        provider = _provider_of(entry)
        if _get_default(d, provider) == name:
            _set_default(d, provider, next((n for n, v in d["keys"].items() if _provider_of(v) == provider), None))
    _save(d)


def set_default(name: str) -> None:
    k = find_key(name)
    d = _load()
    _set_default(d, k.provider, name)
    _save(d)


def set_quota(name: str, quota: int) -> None:
    if int(quota) < 1:
        raise ValueError("the monthly quota must be a positive number")
    k = find_key(name)
    if not get_source(k.provider).has_quota:
        raise ValueError(f"{get_source(k.provider).title} keys have no monthly request quota")
    d = _load()
    if name in d["keys"]:
        d["keys"][name]["quota"] = int(quota)
    elif name == NETRC_NAME:
        d["netrc_quota"] = int(quota)
    else:
        raise KeyError(name)
    _save(d)


async def validate(secret: str, provider: str = EDH) -> tuple[bool, str]:
    """Check a key with its data source. Earth Data Hub: one request on its private test dataset (counts against
    the quota). Climate Data Store: the account profile (free, nothing queued)."""
    if provider == CDS:
        from .sources import cds

        return await cds.validate(secret)
    get_source(provider)
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
