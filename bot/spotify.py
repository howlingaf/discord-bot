import time
import urllib.parse

import discord
from aiohttp import ClientSession, web

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPES,
    SPOTIFY_PAUSE_THRESHOLD,
    PUBLIC_BASE_URL,
)
from .database import (
    create_state,
    spotify_get_tokens,
    spotify_upsert_tokens,
    spotify_get_runtime,
    spotify_set_runtime,
)


def spotify_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "response_type": "code",
        "scope": SPOTIFY_SCOPES,
        "state": state,
        "show_dialog": "true",
    })
    return f"https://accounts.spotify.com/authorize?{qs}"


async def spotify_exchange_code(session: ClientSession, code: str) -> dict:
    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    async with session.post(token_url, data=data) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Spotify token exchange failed: {js}")
        return js


async def spotify_refresh(session: ClientSession, refresh_token: str) -> dict:
    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    async with session.post(token_url, data=data) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Spotify refresh failed: {js}")
        return js


async def spotify_get_access_token(session: ClientSession) -> str | None:
    access_token, refresh_token, expires_at = spotify_get_tokens()
    now = int(time.time())

    if not refresh_token:
        return None

    if access_token and expires_at and expires_at > now:
        return access_token

    js = await spotify_refresh(session, refresh_token)
    new_access = js["access_token"]
    new_refresh = js.get("refresh_token")  # may be absent
    expires_in = js.get("expires_in", 3600)
    spotify_upsert_tokens(new_access, new_refresh, expires_in)
    return new_access


async def spotify_get_playback(session: ClientSession, access_token: str) -> dict | None:
    url = "https://api.spotify.com/v1/me/player"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status == 204:
            return None
        js = await resp.json()
        if resp.status != 200:
            return None
        return js


async def spotify_player_put(session: ClientSession, access_token: str, endpoint: str) -> bool:
    url = f"https://api.spotify.com/v1/me/player/{endpoint}"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with session.put(url, headers=headers) as resp:
        text = await resp.text()
        if 200 <= resp.status < 300 and '"error"' not in text:
            return True
        print(f"[SPOTIFY] {endpoint} failed status={resp.status} body={text}")
        return False


async def spotify_pause(session: ClientSession, access_token: str) -> bool:
    return await spotify_player_put(session, access_token, "pause")


async def spotify_play(session: ClientSession, access_token: str) -> bool:
    return await spotify_player_put(session, access_token, "play")


def count_humans_in_channel(channel: discord.VoiceChannel) -> int:
    return sum(1 for m in channel.members if not m.bot)


async def handle_spotify_auto_pause(http_session: ClientSession, member_count: int):
    paused_by_bot, _, _ = spotify_get_runtime()
    now = int(time.time())

    spotify_set_runtime(last_member_count=member_count)

    access = await spotify_get_access_token(http_session)
    if not access:
        return

    threshold = SPOTIFY_PAUSE_THRESHOLD if SPOTIFY_PAUSE_THRESHOLD > 0 else 2

    # PAUSE when >= threshold
    if member_count >= threshold:
        playback = await spotify_get_playback(http_session, access)
        is_playing = bool(playback and playback.get("is_playing"))
        if is_playing:
            ok = await spotify_pause(http_session, access)
            if ok:
                spotify_set_runtime(paused_by_bot=True, last_action_at=now)
        return

    # RESUME when <= 1 (only if we paused it)
    if member_count <= 1 and paused_by_bot:
        ok = await spotify_play(http_session, access)
        if ok:
            spotify_set_runtime(paused_by_bot=False, last_action_at=now)
        return


async def dm_spotify_link(user: discord.Member):
    state = create_state(user.id)
    url = f"{PUBLIC_BASE_URL}/spotify/start?state={urllib.parse.quote(state)}"
    await user.send(
        "Link Spotify (one-time) so I can auto pause/resume during voice:\n"
        f"{url}"
    )
