"""User-facing wording for failures: pure functions, so they are testable without a browser."""

import re

_AUTH = re.compile(r"\b(401|403)\b|unauthori[sz]ed|forbidden|rejected|invalid (api )?key|not authori[sz]ed", re.I)
_NET = re.compile(r"connect|resolve|name or service|timed? ?out|network|unreachable|offline|temporary failure|"
                  r"max retries|ssl", re.I)


def classify_error(err: object) -> str:
    """'auth' (the server refused the key), 'network' (it could not be reached) or 'other'."""
    text = str(err)
    if _AUTH.search(text):
        return "auth"
    if _NET.search(text):
        return "network"
    return "other"


def dataset_error(err: object, key: str | None, service: str = "Earth Data Hub") -> dict:
    """What to show when a dataset cannot be loaded: ``kind``, a one-line ``title``, the raw ``details``
    and the ``action`` that fits ('keys', 'retry' or None)."""
    kind = classify_error(err)
    if kind == "auth":
        who = f" the key '{key}'" if key else " the key"
        return {"kind": kind, "title": f"{service} rejected{who}.", "details": str(err), "action": "keys"}
    if kind == "network":
        return {"kind": kind, "title": f"Can't reach {service}.", "details": str(err), "action": "retry"}
    return {"kind": kind, "title": "Could not load the dataset.", "details": str(err), "action": "retry"}


def launch_reason(plan_ready: bool, has_key: bool, blocked: list[str] | None = None, error: str | None = None,
                  planning: bool = False) -> str:
    """The one line under a disabled Launch button; '' when launching is possible."""
    if not has_key:
        return "Add an API key in Keys."
    if planning:
        return "Updating the plan…"
    if error:
        return error
    if blocked:
        return blocked[0]
    if not plan_ready:
        return "Select a dataset and variables first."
    return ""


def display_version(version: str, dev: bool = False) -> str:
    """The version for the header: a ``+local`` build suffix is only of interest to developers."""
    if dev:
        return version
    return version.split("+", 1)[0]
