import urllib.parse

import discord
from aiohttp import ClientSession, web

from .config import (
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
    TWITCH_REDIRECT_URI,
    PUBLIC_BASE_URL,
)
from .database import create_state


def twitch_authorize_url(state: str) -> str:
    qs = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope": "",
        "state": state,
    })
    return f"https://id.twitch.tv/oauth2/authorize?{qs}"


async def twitch_exchange_code(session: ClientSession, code: str) -> dict:
    token_url = "https://id.twitch.tv/oauth2/token"
    data = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TWITCH_REDIRECT_URI,
    }
    async with session.post(token_url, data=data) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Twitch token exchange failed: {js}")
        return js


async def twitch_get_user(session: ClientSession, access_token: str) -> dict:
    url = "https://api.twitch.tv/helix/users"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Client-Id": TWITCH_CLIENT_ID,
    }
    async with session.get(url, headers=headers) as resp:
        js = await resp.json()
        if resp.status != 200:
            raise web.HTTPBadRequest(text=f"Twitch get user failed: {js}")
        data = js.get("data", [])
        if not data:
            raise web.HTTPBadRequest(text="No user data returned from Twitch.")
        return data[0]


async def try_set_nick(member: discord.Member, display_name: str) -> tuple[bool, str]:
    try:
        await member.edit(nick=display_name, reason="Twitch verified: set nickname to Twitch display name")
        return True, "ok"
    except discord.Forbidden:
        return False, "Forbidden (owner/admin or role hierarchy/permission)"
    except discord.HTTPException as e:
        return False, f"HTTPException: {e}"


async def dm_verify_link(member: discord.Member):
    state = create_state(member.id)
    url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
    await member.send(
        "Almost done — click once to confirm your Twitch display name for on-stream voice:\n"
        f"{url}\n\n"
        "After this, I'll set your server nickname permanently."
    )
