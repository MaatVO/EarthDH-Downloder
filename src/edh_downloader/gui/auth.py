"""Access token for the web UIs (shared servers: other users can reach localhost ports)."""

import secrets

from fastapi import Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware


class TokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app_, token: str):
        super().__init__(app_)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/_nicegui/") and not path.startswith("/_nicegui/client"):
            return await call_next(request)  # static assets
        # browsers share cookies between ports of one host: the laptop app and the servers it forwards are all
        # "localhost", so the cookie is named after the port the browser sees
        name = f"edh_token_{request.url.port or 80}"
        q = request.query_params.get("token")
        if q and secrets.compare_digest(q, self.token):
            resp = RedirectResponse(url=path)
            resp.set_cookie(name, self.token, httponly=True, samesite="strict")
            return resp
        c = request.cookies.get(name, "")
        if c and secrets.compare_digest(c, self.token):
            return await call_next(request)
        return PlainTextResponse("Open the link with the access token printed by `edh-dl serve`.", status_code=401)
