"""Outbound client for the Twitch bot's /console control API.

The reverse of the inbound /recap integration: Discord slash commands typed in
the twitch-bot-console channel are forwarded here, which POSTs them to the Twitch
bot and returns (ok, human-readable text). Never raises (connection refused,
timeout and non-200 are turned into a clear message); never logs the secret.
"""
import asyncio

import aiohttp

from .config import CONSOLE_SECRET, TWITCH_BOT_URL

_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def call_console(session: aiohttp.ClientSession, command: str, args: str = "") -> tuple[bool, str]:
    """POST {command, args} to the Twitch bot's /console. Returns (ok, text)."""
    if not CONSOLE_SECRET:
        return False, "CONSOLE_SECRET isn't set on the Discord bot — can't authenticate to the Twitch bot."

    url = f"{TWITCH_BOT_URL}/console"
    headers = {
        "Authorization": f"Bearer {CONSOLE_SECRET}",
        "Content-Type": "application/json",
    }
    payload = {"command": command, "args": args or ""}

    try:
        async with session.post(url, json=payload, headers=headers, timeout=_TIMEOUT) as resp:
            # Contract: the body is always JSON {"ok": bool, "output": str}.
            try:
                js = await resp.json(content_type=None)
            except Exception:
                js = None
            if isinstance(js, dict) and "output" in js:
                return bool(js.get("ok")), str(js.get("output") or "(no output)")

            # No / malformed JSON body — fall back to a status-based message.
            if resp.status == 401:
                return False, "Twitch bot rejected auth (CONSOLE_SECRET mismatch)."
            if resp.status == 404:
                return False, f"Twitch bot doesn't recognize command '{command}'."
            if resp.status == 400:
                return False, "Twitch bot rejected the request (bad body)."
            return False, f"Twitch bot returned HTTP {resp.status} with no usable body."
    except aiohttp.ClientConnectorError:
        return False, "Couldn't reach the Twitch bot (connection refused — is it running?)."
    except asyncio.TimeoutError:
        return False, "Twitch bot timed out (no response within 10s)."
    except Exception as e:
        return False, f"Request to the Twitch bot failed: {e!r}"
