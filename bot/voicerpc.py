"""OAuth token exchange for the follow-me voice overlay (desktop/voice_overlay.py).

That helper runs on the owner's PC and talks to the Discord client over its
local IPC pipe, which (unlike the RPC websocket) has no origin allowlist, so the
app owner can use it without Discord's RPC whitelisting. The only piece that
needs the client secret is the code/refresh exchange, so it lives here.

POST /voice-rpc/token   X-Key: <VOICECHAT_SECRET>   {"code": ...} | {"refresh_token": ...}
"""

import hmac
import os

import aiohttp
from aiohttp import web
from dotenv import dotenv_values

from .config import VOICECHAT_SECRET

# The one redirect URI registered on the app; RPC's AUTHORIZE doesn't send one,
# so the exchange is tried without it first.
REDIRECT_URIS = [None, "http://localhost"]


def _client_secret() -> str:
    # Read per request so the secret can land in .env after the bot is running.
    return os.getenv("DISCORD_CLIENT_SECRET") or dotenv_values(".env").get("DISCORD_CLIENT_SECRET") or ""


async def _exchange(client_id: str, form: dict) -> tuple[int, dict]:
    last = (500, {"error": "no attempt"})
    async with aiohttp.ClientSession() as s:
        for redirect in REDIRECT_URIS if "code" in form else [None]:
            data = {"client_id": client_id, "client_secret": _client_secret(), **form}
            if redirect:
                data["redirect_uri"] = redirect
            async with s.post("https://discord.com/api/v10/oauth2/token", data=data) as r:
                last = (r.status, await r.json(content_type=None))
                if r.status == 200:
                    return last
    return last


def register_routes(app: web.Application, bot) -> None:
    routes = web.RouteTableDef()

    @routes.post("/voice-rpc/token")
    async def token(request: web.Request):
        if not VOICECHAT_SECRET or not hmac.compare_digest(
                request.headers.get("X-Key", "").encode(), VOICECHAT_SECRET.encode()):
            raise web.HTTPForbidden(text="bad key")
        if not _client_secret():
            return web.json_response({"error": "DISCORD_CLIENT_SECRET not set on the server"},
                                     status=500)
        body = await request.json()
        if body.get("code"):
            form = {"grant_type": "authorization_code", "code": body["code"]}
        elif body.get("refresh_token"):
            form = {"grant_type": "refresh_token", "refresh_token": body["refresh_token"]}
        else:
            raise web.HTTPBadRequest(text="code or refresh_token required")
        status, data = await _exchange(str(bot.application_id), form)
        return web.json_response(data, status=status)

    app.add_routes(routes)
