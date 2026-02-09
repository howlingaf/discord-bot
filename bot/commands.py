import urllib.parse

import discord
from discord import app_commands

from .config import (
    PUBLIC_BASE_URL,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
)
from .database import has_mapping, create_state
from .twitch import try_set_nick
from .spotify import dm_spotify_link
from .leetcode import post_leetcode_daily, post_leetcode_contest
from .client import bot


@bot.tree.command(name="settwitch", description="Set your server nickname to your Twitch display name.")
@app_commands.describe(display_name="Your Twitch display name (e.g., hairyrug_)")
async def settwitch(interaction: discord.Interaction, display_name: str):
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this command inside the server.", ephemeral=True)
        return

    ok, why = await try_set_nick(member, display_name)
    if ok:
        await interaction.response.send_message(f"\u2705 Set your nickname to **{display_name}**", ephemeral=True)
        return

    await interaction.response.send_message(
        "\u274c I can't change your nickname.\n"
        f"Reason: {why}",
        ephemeral=True
    )


@bot.tree.command(name="verify", description="Get the Twitch verify link (fallback if you didn't receive a DM).")
async def verify(interaction: discord.Interaction):
    if has_mapping(interaction.user.id):
        await interaction.response.send_message("\u2705 You're already verified.", ephemeral=True)
        return

    state = create_state(interaction.user.id)
    url = f"{PUBLIC_BASE_URL}/verify/start?state={urllib.parse.quote(state)}"
    await interaction.response.send_message(f"Click to verify your Twitch name:\n{url}", ephemeral=True)


@bot.tree.command(name="spotifylink", description="(Owner) DM yourself the Spotify link so the bot can auto pause/resume.")
async def spotifylink(interaction: discord.Interaction):
    if SPOTIFY_ALLOWED_USER_ID and interaction.user.id != SPOTIFY_ALLOWED_USER_ID:
        await interaction.response.send_message("\u274c Not allowed.", ephemeral=True)
        return

    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI):
        await interaction.response.send_message("\u274c Spotify env not configured.", ephemeral=True)
        return

    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("Run this inside the server.", ephemeral=True)
        return

    try:
        await dm_spotify_link(member)
        await interaction.response.send_message("\u2705 Check your DMs for the Spotify link.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("\u274c I can't DM you.", ephemeral=True)


@bot.tree.command(name="daily", description="Post the current LeetCode daily (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted today.")
async def daily(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_daily(bot, force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="weekly", description="Post the current LeetCode weekly contest (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
async def weekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_contest(bot, "weekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="biweekly", description="Post the current LeetCode biweekly contest (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
async def biweekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_contest(bot, "biweekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)
