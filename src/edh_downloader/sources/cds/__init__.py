"""Copernicus Climate Data Store: account access.

The CDS API authenticates with a personal access token in the ``PRIVATE-TOKEN`` header. Reading the account
profile checks a token without queueing anything and without any cost.
"""

import os

API = "https://cds.climate.copernicus.eu/api"


def api() -> str:
    """The CDS API base URL (``EDH_DL_CDS_API`` points the app at another one, e.g. the test server)."""
    return (os.environ.get("EDH_DL_CDS_API") or API).rstrip("/")


async def validate(secret: str, base: str | None = None) -> tuple[bool, str]:
    """Check a token against the CDS account endpoint (free: nothing is queued)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True) as s:
            async with s.get(f"{base or api()}/profiles/v1/account", headers={"PRIVATE-TOKEN": secret}) as r:
                if r.status == 200:
                    return True, "token accepted by the Climate Data Store"
                if r.status in (401, 403):
                    return False, f"token rejected (HTTP {r.status})"
                return False, f"unexpected HTTP {r.status}"
    except Exception as e:  # network problems
        return False, f"could not reach the Climate Data Store: {type(e).__name__}: {e}"
