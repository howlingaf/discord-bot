import discord
from discord import app_commands

from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_ALLOWED_USER_ID,
)
from .spotify import dm_spotify_link
from .config import GUILD_ID
from .leetcode import post_leetcode_contest, post_leetcode_problem, post_leetcode_weekly_premium, get_or_create_problem_post, _classify_contest
from .database import leetcode_delete_problem, linked_users_get, linked_users_get_by_username, linked_users_set, linked_users_delete
from .client import bot


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



@bot.tree.command(name="problem", description="Look up or create a forum post for a LeetCode problem by ID.")
@app_commands.describe(question_id="The LeetCode problem number (e.g. 67)")
async def problem(interaction: discord.Interaction, question_id: int):
    if question_id < 1:
        await interaction.response.send_message("\u274c Invalid problem ID.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        thread_id, err = await get_or_create_problem_post(bot, str(question_id))
        if thread_id:
            thread_url = f"https://discord.com/channels/{GUILD_ID}/{thread_id}"
            await interaction.followup.send(thread_url, ephemeral=True)
        else:
            await interaction.followup.send(f"\u274c {err}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Invalid problem ID.", ephemeral=True)


@bot.tree.command(name="delete", description="(Admin) Delete a problem post by LeetCode ID.")
@app_commands.describe(question_id="The LeetCode problem number to delete (e.g. 67)")
@app_commands.checks.has_permissions(manage_messages=True)
async def delete_problem(interaction: discord.Interaction, question_id: int):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = leetcode_delete_problem(str(question_id))
        if not deleted:
            await interaction.followup.send(f"\u274c Problem #{question_id} not found.", ephemeral=True)
            return

        # Delete the forum post
        try:
            thread = bot.get_channel(deleted["thread_id"]) or await bot.fetch_channel(deleted["thread_id"])
            await thread.delete()
        except Exception:
            pass

        await interaction.followup.send(f"\u2705 Deleted problem #{question_id} ({deleted['title']}).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="daily", description="(Admin) Post today's LeetCode daily problem (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def daily(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_problem(bot, force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="weekly", description="(Admin) Post the current LeetCode weekly contest (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def weekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_contest(bot, "weekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="biweekly", description="(Admin) Post the current LeetCode biweekly contest (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def biweekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_contest(bot, "biweekly", force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="link", description="Link your Discord account to your LeetCode profile.")
@app_commands.describe(username="Your LeetCode username")
async def link(interaction: discord.Interaction, username: str):
    await interaction.response.defer(ephemeral=True)
    try:
        # Check if this LeetCode username is already claimed by someone else
        existing_owner = linked_users_get_by_username(username)
        if existing_owner and existing_owner != interaction.user.id:
            await interaction.followup.send("\u274c That LeetCode username is already linked to another user.", ephemeral=True)
            return

        # Verify the username exists on LeetCode
        async with bot.http_session.get(f"https://leetcode-api-pied.vercel.app/user/{username}") as resp:
            if resp.status != 200:
                await interaction.followup.send("\u274c Could not find that LeetCode username.", ephemeral=True)
                return
            data = await resp.json()
            if not data.get("username"):
                await interaction.followup.send("\u274c Could not find that LeetCode username.", ephemeral=True)
                return

        linked_users_set(interaction.user.id, username)
        await interaction.followup.send(f"\u2705 Linked to **{username}**.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="unlink", description="Unlink your LeetCode profile from your Discord account.")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    removed = linked_users_delete(interaction.user.id)
    if removed:
        await interaction.followup.send("\u2705 Your LeetCode account has been unlinked.", ephemeral=True)
    else:
        await interaction.followup.send("\u2139\ufe0f You don't have a linked LeetCode account.", ephemeral=True)


@bot.tree.command(name="premium-weekly", description="(Admin) Post this week's premium weekly problem (manual trigger).")
@app_commands.describe(force="If true, post even if it was already posted.")
@app_commands.checks.has_permissions(manage_messages=True)
async def premium_weekly(interaction: discord.Interaction, force: bool = True):
    await interaction.response.defer(ephemeral=True)
    try:
        posted, msg = await post_leetcode_weekly_premium(bot, force=force)
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)


@bot.tree.command(name="contest-recap", description="(Admin) Post a recap for any contest by slug.")
@app_commands.describe(slug="The contest slug (e.g. weekly-contest-488)")
@app_commands.checks.has_permissions(manage_messages=True)
async def contest_recap(interaction: discord.Interaction, slug: str):
    await interaction.response.defer(ephemeral=True)
    try:
        contest_type = _classify_contest(slug)
        if not contest_type:
            await interaction.followup.send("\u274c Slug must start with 'weekly-contest-' or 'biweekly-contest-'.", ephemeral=True)
            return

        title = slug.replace("-", " ").title()
        mock_contest = {"title": title, "titleSlug": slug, "startTime": 0, "duration": 5400}

        posted, msg = await post_leetcode_contest(
            bot, contest_type, force=True, contests=[mock_contest],
        )
        await interaction.followup.send(("\u2705 " if posted else "\u2139\ufe0f ") + msg, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"\u274c Failed: {repr(e)}", ephemeral=True)
