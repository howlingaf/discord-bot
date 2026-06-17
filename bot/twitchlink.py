"""Link Twitch chatters to Discord users via a mod-approval workflow, then
silently tag (non-pinging) linked chatters in solution messages.

Failure-isolated like logbus: the integration entry points (`link_suffix`,
`maybe_prompt`) never raise — a linking error must never break recap posting.
Interactive controls use discord.py DynamicItems registered once via
`bot.add_dynamic_items(...)`, so they survive the per-deploy restart with no
stored message IDs.
"""
import difflib
import re

import discord

from .config import GUILD_ID, TWITCH_LINK_PROMPT_CHANNEL_ID
from .database import (
    twitch_link_get,
    twitch_link_create_pending,
    twitch_link_set_status,
)
from .logbus import log_error

# Twitch handles are alphanumeric + underscore, <=25 chars. Normalize to lowercase.
_HANDLE_RE = re.compile(r"^[a-z0-9_]{1,25}$")
_TEMPLATE_APPROVE = r"tl:approve:(?P<handle>[a-z0-9_]{1,25})"
_TEMPLATE_DISMISS = r"tl:dismiss:(?P<handle>[a-z0-9_]{1,25})"

_FUZZY_THRESHOLD = 0.62
_MAX_CANDIDATES = 5


def _norm(handle: str) -> str | None:
    """Lowercase + validate. Returns None for unsafe/unembeddable handles."""
    h = (handle or "").strip().lower()
    return h if _HANDLE_RE.match(h) else None


def _clean(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------- hot path
def link_suffix(twitch_user: str) -> str:
    """Returns ' (<@id>)' if this handle is LINKED, else ''. Sync, never raises."""
    try:
        h = _norm(twitch_user)
        if not h:
            return ""
        row = twitch_link_get(h)
        if row and row["status"] == "linked" and row["discord_user_id"]:
            return f" (<@{row['discord_user_id']}>)"
    except Exception as e:
        log_error(f"[TWITCHLINK] link_suffix({twitch_user!r}) failed: {e!r}")
    return ""


# ---------------------------------------------------------------- matching
def _candidates(bot, handle: str) -> list[discord.Member]:
    """Top ~5 non-bot members whose name/display/global fuzzily match `handle`."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return []
    target = _clean(handle)
    if not target:
        return []
    scored: list[tuple[float, discord.Member]] = []
    for m in guild.members:
        if m.bot:
            continue
        best = 0.0
        for field in (m.name, m.display_name, m.global_name):
            cf = _clean(field)
            if not cf:
                continue
            if cf == target or target in cf or cf in target:
                best = 1.0
                break
            r = difflib.SequenceMatcher(None, target, cf).ratio()
            if r > best:
                best = r
        if best >= _FUZZY_THRESHOLD:
            scored.append((best, m))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [m for _, m in scored[:_MAX_CANDIDATES]]


# ---------------------------------------------------------------- components
async def _require_mod(interaction: discord.Interaction) -> bool:
    ok = isinstance(interaction.user, discord.Member) and \
        interaction.user.guild_permissions.manage_messages
    if not ok:
        try:
            await interaction.response.send_message(
                "❌ You need Manage Messages to do that.", ephemeral=True)
        except Exception:
            pass
    return ok


class _ApproveSelect(discord.ui.DynamicItem[discord.ui.UserSelect], template=_TEMPLATE_APPROVE):
    def __init__(self, handle: str):
        self.handle = handle
        super().__init__(
            discord.ui.UserSelect(
                placeholder=f"Link Twitch '{handle}' to a Discord member…",
                min_values=1,
                max_values=1,
                custom_id=f"tl:approve:{handle}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["handle"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _require_mod(interaction)

    async def callback(self, interaction: discord.Interaction):
        try:
            member = self.item.values[0]
            twitch_link_set_status(self.handle, "linked", member.id)
            await interaction.response.edit_message(
                content=f"✅ Linked Twitch **{self.handle}** → {member.mention} "
                        f"(by {interaction.user.mention})",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            log_error(f"[TWITCHLINK] approve callback failed for {self.handle!r}: {e!r}")
            try:
                await interaction.response.send_message("❌ Failed to save link.", ephemeral=True)
            except Exception:
                pass


class _DismissButton(discord.ui.DynamicItem[discord.ui.Button], template=_TEMPLATE_DISMISS):
    def __init__(self, handle: str):
        self.handle = handle
        super().__init__(
            discord.ui.Button(
                label="Dismiss (no link)",
                style=discord.ButtonStyle.secondary,
                custom_id=f"tl:dismiss:{handle}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["handle"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _require_mod(interaction)

    async def callback(self, interaction: discord.Interaction):
        try:
            twitch_link_set_status(self.handle, "dismissed", None)
            await interaction.response.edit_message(
                content=f"🚫 Dismissed Twitch **{self.handle}** (by {interaction.user.mention})",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            log_error(f"[TWITCHLINK] dismiss callback failed for {self.handle!r}: {e!r}")


def _build_view(handle: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(_ApproveSelect(handle))
    v.add_item(_DismissButton(handle))
    return v


# ---------------------------------------------------------------- prompt
async def maybe_prompt(bot, twitch_user: str) -> None:
    """If `twitch_user` is brand-new, atomically mark it pending and post an
    approval prompt to the console channel. Never raises."""
    try:
        h = _norm(twitch_user)
        if not h:
            return
        if twitch_link_get(h) is not None:
            return  # already pending / linked / dismissed
        if not twitch_link_create_pending(h):
            return  # lost the race — another path posted the prompt
        channel = bot.get_channel(TWITCH_LINK_PROMPT_CHANNEL_ID) \
            or await bot.fetch_channel(TWITCH_LINK_PROMPT_CHANNEL_ID)
        cands = _candidates(bot, h)
        if cands:
            cand_txt = "\n".join(f"• {m.mention} (`{m.name}`)" for m in cands)
        else:
            cand_txt = "_no close matches found — use the menu to pick anyone_"
        content = (
            f"🔗 **New Twitch chatter:** `{h}`\n"
            f"Suggested matches:\n{cand_txt}\n"
            f"Pick the Discord member to link, or Dismiss."
        )
        await channel.send(
            content, view=_build_view(h),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as e:
        log_error(f"[TWITCHLINK] maybe_prompt({twitch_user!r}) failed: {e!r}")


def register(bot) -> None:
    """Register persistent dynamic items once (idempotent across reconnects)."""
    if getattr(bot, "_twitchlink_registered", False):
        return
    bot._twitchlink_registered = True
    bot.add_dynamic_items(_ApproveSelect, _DismissButton)
