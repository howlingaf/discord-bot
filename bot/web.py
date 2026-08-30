import asyncio
import hmac
import re

import discord
from aiohttp import web

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
    RECAP_SECRET,
    CONSOLE_SECRET,
    ALERT_CHANNEL_ID,
    STREAM_ALERT_CHANNEL_ID,
    STREAM_ALERT_TEST_CHANNEL_ID,
    STREAM_ALERT_TEXT,
    TWITCH_CHANNEL_URL,
)
from .database import consume_state, spotify_upsert_tokens, spotify_set_runtime
from .recap import process_recap
from .spotify import spotify_authorize_url, spotify_exchange_code


def _require_bearer(request: web.Request, secret: str):
    """Constant-time Bearer check. Compare as bytes: hmac.compare_digest raises
    TypeError on non-ASCII str, which would turn a malformed header into a 500."""
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if not secret or not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
        raise web.HTTPUnauthorized(text="Invalid or missing auth token")


async def _twitch_json(request: web.Request, *required: str) -> dict:
    """Auth + body for the twitch bot's CONSOLE_SECRET endpoints: a bearer
    check, a JSON body, and the fields it must carry."""
    _require_bearer(request, CONSOLE_SECRET)
    try:
        payload = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        raise web.HTTPBadRequest(text=f"missing: {', '.join(missing)}")
    return payload


def _require_recap_auth(request: web.Request):
    """Shared by the recap/post-solution endpoints."""
    _require_bearer(request, RECAP_SECRET)


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
        _require_recap_auth(request)
        return web.json_response({"status": "ok"})

    @routes.post("/recap")
    async def recap(request: web.Request):
        _require_recap_auth(request)

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

    # ---- Post Solution ----
    @routes.post("/post-solution")
    async def post_solution(request: web.Request):
        _require_recap_auth(request)

        try:
            payload = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Invalid JSON")

        slug = payload.get("slug") or ""
        solutions = payload.get("solutions") or []
        if not slug or not solutions:
            raise web.HTTPBadRequest(text="Missing slug or solutions")

        from .database import leetcode_get_problem_by_slug

        existing = leetcode_get_problem_by_slug(slug)
        if not existing:
            raise web.HTTPNotFound(text=f"No forum post found for '{slug}'")

        thread_id = existing["thread_id"]

        async def _post():
            try:
                from .twitchlink import solution_name, maybe_prompt
                thread = bot_instance.get_channel(thread_id) or await bot_instance.fetch_channel(thread_id)
                lines = []
                for s in solutions:
                    user = s.get("user") or "anonymous"
                    url = s.get("url") or ""
                    await maybe_prompt(bot_instance, user)  # new handle -> mod approval prompt
                    line = f"{solution_name(user)} submitted a solution!"
                    # Only embed real web links — skip javascript:/data: etc.
                    if url.startswith(("http://", "https://")):
                        line += f"\n<{url}>"
                    lines.append(line)
                # user is caller-supplied; never let it ping @everyone/roles.
                await thread.send(
                    "\n\n".join(lines),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                print(f"[POST-SOLUTION] Posted {len(solutions)} solution(s) to {slug}")
            except Exception as e:
                print(f"[POST-SOLUTION] Failed: {e!r}")

        asyncio.create_task(_post())
        return web.json_response({"status": "accepted"})

    # ---- Twitch bot log relay ----
    @routes.post("/alert")
    async def alert(request: web.Request):
        """Ping the owner in #twitch-bot-console — a real @mention, because it
        exists to be noticed (the twitch bot uses it for a failed ad break)."""
        payload = await _twitch_json(request, "message")
        text = str(payload["message"]).strip()
        if not (ALERT_CHANNEL_ID and SPOTIFY_ALLOWED_USER_ID):
            raise web.HTTPBadRequest(text="Alert channel or owner id not configured")
        try:
            channel = (bot_instance.get_channel(ALERT_CHANNEL_ID)
                       or await bot_instance.fetch_channel(ALERT_CHANNEL_ID))
            await channel.send(
                f"<@{SPOTIFY_ALLOWED_USER_ID}> {text}"[:2000],
                allowed_mentions=discord.AllowedMentions(users=True))
        except Exception as e:
            return web.json_response({"ok": False, "error": repr(e)}, status=200)
        return web.json_response({"ok": True})

    # ---- Stream alerts (go-live post, then edited into a VOD card) ----
    def _round_duration(raw: str) -> str:
        """Twitch's "14h12m40s" -> "14h13m", rounded UP to the minute (so 59s
        reads "1m", never "0m")."""
        if not raw:
            return "?"
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw)
        if not m:
            return raw
        h, mi, se = (int(x or 0) for x in m.groups())
        total = h * 60 + mi + (1 if se else 0)
        return f"{total // 60}h{total % 60}m" if total >= 60 else f"{total}m"

    def _alert_embed(title: str, game: str, vod: dict | None) -> discord.Embed:
        e = discord.Embed(title=(title or "Live")[:256],
                          url=vod["url"] if vod else TWITCH_CHANNEL_URL,
                          color=0x9146FF)
        if game:
            e.add_field(name="Category", value=game[:1024], inline=True)
        if vod:
            # The title already links to the VOD; a second link would be noise.
            e.add_field(name="Duration", value=_round_duration(vod["duration"]), inline=True)
        return e

    async def _alert_channel(test: bool):
        cid = STREAM_ALERT_TEST_CHANNEL_ID if test else STREAM_ALERT_CHANNEL_ID
        return bot_instance.get_channel(cid) or await bot_instance.fetch_channel(cid)

    @routes.post("/stream-alert")
    async def stream_alert(request: web.Request):
        """Go-live. Returns the message id so the caller can edit it later.
        A real @everyone — unless test=true, which posts to #testing unpinged."""
        payload = await _twitch_json(request)
        test = bool(payload.get("test"))
        try:
            channel = await _alert_channel(test)
            msg = await channel.send(
                " ".join(filter(None, ("@everyone", STREAM_ALERT_TEXT))),
                embed=_alert_embed(payload.get("title") or "", payload.get("game") or "", None),
                allowed_mentions=discord.AllowedMentions(everyone=not test))
        except Exception as e:
            return web.json_response({"ok": False, "error": repr(e)})
        return web.json_response({"ok": True, "message_id": str(msg.id)})

    @routes.post("/stream-alert/vod")
    async def stream_alert_vod(request: web.Request):
        """Edit a go-live post into its VOD card. Never re-pings: an edit can't."""
        payload = await _twitch_json(request, "message_id")
        message_id = int(payload["message_id"])
        vod = {"url": payload.get("vod_url") or TWITCH_CHANNEL_URL,
               "duration": payload.get("duration") or ""}
        try:
            channel = await _alert_channel(bool(payload.get("test")))
            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=_alert_embed(payload.get("title") or "", payload.get("game") or "", vod))
        except Exception as e:
            return web.json_response({"ok": False, "error": repr(e)})
        return web.json_response({"ok": True})

    @routes.post("/twitch-log")
    async def twitch_log(request: web.Request):
        payload = await _twitch_json(request)
        lines = payload.get("lines")
        if lines is None:
            msg = payload.get("message")
            lines = [msg] if msg else []
        if not isinstance(lines, list):
            raise web.HTTPBadRequest(text="`lines` must be a list of strings")

        from .twitchlog import push
        push([str(x) for x in lines])
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes(routes)

    from .voicechat import register_routes as vc_register
    vc_register(app, bot_instance)

    return app
