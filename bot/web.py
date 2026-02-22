import asyncio

from aiohttp import web

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
    RECAP_SECRET,
)
from .database import consume_state, spotify_upsert_tokens, spotify_set_runtime
from .recap import process_recap
from .spotify import spotify_authorize_url, spotify_exchange_code


def make_web_app(bot_instance) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/health")
    async def health(_: web.Request):
        return web.Response(text="ok", content_type="text/plain")

    # ---- Spotify OAuth ----
    @routes.get("/spotify/start")
    async def spotify_start(request: web.Request):
        state = request.query.get("state")
        if not state:
            raise web.HTTPBadRequest(text="Missing state")
        if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI):
            raise web.HTTPBadRequest(text="Spotify env not configured.")
        return web.HTTPFound(spotify_authorize_url(state))

    @routes.get("/spotify/callback")
    async def spotify_callback(request: web.Request):
        if request.query.get("error"):
            desc = request.query.get("error_description") or "Cancelled."
            return web.Response(text=f"Spotify auth cancelled: {desc}", content_type="text/plain")

        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            raise web.HTTPBadRequest(text="Missing code/state")

        discord_user_id = consume_state(state)
        if not discord_user_id:
            return web.Response(text="This Spotify link is invalid or expired. Please try again.", content_type="text/plain")

        if SPOTIFY_ALLOWED_USER_ID and discord_user_id != SPOTIFY_ALLOWED_USER_ID:
            return web.Response(text="Not allowed to link Spotify for this bot.", content_type="text/plain")

        session = bot_instance.http_session
        if session is None:
            raise web.HTTPServiceUnavailable(text="Bot not ready")

        token_js = await spotify_exchange_code(session, code)
        access_token = token_js["access_token"]
        refresh_token = token_js.get("refresh_token")
        expires_in = token_js.get("expires_in", 3600)

        if not refresh_token:
            return web.Response(
                text="Spotify did not return a refresh_token. Remove bot access in Spotify and try again.\n"
                     "Spotify: Settings \u2192 Apps \u2192 Remove access, then re-link.",
                content_type="text/plain",
            )

        spotify_upsert_tokens(access_token, refresh_token, expires_in)
        spotify_set_runtime(paused_by_bot=False, last_action_at=0, last_member_count=-1)

        return web.Response(
            text="\u2705 Spotify linked! Auto pause/resume can now work.\nYou can close this window.",
            content_type="text/plain",
        )

    # ---- Recap ----
    @routes.get("/recap/verify")
    async def recap_verify(request: web.Request):
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {RECAP_SECRET}"
        if not RECAP_SECRET or auth != expected:
            raise web.HTTPUnauthorized(text="Invalid or missing auth token")
        return web.json_response({"status": "ok"})

    @routes.post("/recap")
    async def recap(request: web.Request):
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {RECAP_SECRET}"
        if not RECAP_SECRET or auth != expected:
            raise web.HTTPUnauthorized(text="Invalid or missing auth token")

        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Invalid JSON")

        async def _run_recap():
            try:
                await process_recap(bot_instance, payload)
            except Exception as e:
                print(f"[RECAP] process_recap failed: {e!r}")
                import traceback
                traceback.print_exc()

        asyncio.create_task(_run_recap())
        return web.json_response({"status": "accepted"})

    app = web.Application()
    app.add_routes(routes)
    return app
