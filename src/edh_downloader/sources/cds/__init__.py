"""Copernicus Climate Data Store: account access.

The CDS API authenticates with a personal access token in the ``PRIVATE-TOKEN`` header. Reading the account
profile checks a token without queueing anything and without any cost.
"""

API = "https://cds.climate.copernicus.eu/api"


async def validate(secret: str, api: str = API) -> tuple[bool, str]:
    """Check a token against the CDS account endpoint (free: nothing is queued)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(f"{api}/profiles/v1/account", headers={"PRIVATE-TOKEN": secret}) as r:
                if r.status == 200:
                    return True, "token accepted by the Climate Data Store"
                if r.status in (401, 403):
                    return False, f"token rejected (HTTP {r.status})"
                return False, f"unexpected HTTP {r.status}"
    except Exception as e:  # network problems
        return False, f"could not reach the Climate Data Store: {type(e).__name__}: {e}"
