"""#schedule — aggregate public Google Calendars into two pinned, always-current
messages: a 7-day week view (embeds) and a rendered month-grid image.

Design contract (from the requirements):
  * Stale, never wrong. Every cycle regenerates both views from source; there is
    no per-event state that can drift. The only tolerated failure is old data.
  * Both views derive from ONE fetch so they can't contradict each other.
  * Fail soft per source (one calendar failing never blanks the others — its last
    good result is reused in-memory); fail loud overall (persistent failures
    escalate to the mod console via logbus).
  * Edit in place; if a pinned message is deleted, recreate it once.
  * Display anchored to America/Chicago; timed entries also carry a Discord
    dynamic timestamp so each viewer sees their own local time. All-day events
    land on the correct Central date regardless of server timezone.

Per calendar the sources are tried in order: Google Calendar API (primary, with
singleEvents=true so recurrences are expanded server-side), then the ICS url if
configured, then the in-memory last-good cache. The ICS fallback does not expand
RRULEs — a calendar that relies on them should be fetched via the API.

Styling is config-driven (schedule_calendars.json): a calendar has a color
(one hex = solid, a list = gradient) and an optional platform badge, and
optional title-match rules that override both per event — e.g. one personal
calendar whose "Substack …" events are orange with the Substack logo while its
"Twitch …" events are purple with the Twitch logo. Badge icons are fetched once
from configured urls, cached on disk, drawn into the month grid, and uploaded
as application emojis for the week embed (colored-dot fallback if any of that
fails).
"""

import asyncio
import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
from PIL import Image, ImageDraw

from .config import (
    GOOGLE_API_KEY,
    SCHEDULE_CALENDARS_FILE,
    SCHEDULE_CHANNEL_ID,
    SCHEDULE_POLL_SECONDS,
    SCHEDULE_TZ,
)
from .database import (
    schedule_set_last_synced,
    schedule_set_message_ids,
    schedule_state_get,
)
from .logbus import log_if_persistent
from .schedule_render import BADGE_SIZE, GridEvent, hex_to_rgb, render_month_png

TZ = ZoneInfo(SCHEDULE_TZ)
UTC = ZoneInfo("UTC")
WEEK_COLOR = 0x5865F2
# left-stripe colors for non-today day cards, alternated so each day boundary
# is visible at a glance; today gets blurple
_DAY_COLORS = (0x4E5058, 0x26272B)
FOLLOW_COLOR = 0x2B2D31   # near-invisible stripe: the footer stays subtle
# Discord caps the combined character count of all embeds in a message at 6000
_MESSAGE_EMBED_CAP = 5900
_MAX_PER_DAY = 8           # week-view entries shown per day before "+N more"
_FIELD_CAP = 1024          # Discord embed field-value hard limit
_BADGE_CACHE_DIR = "badge_cache"
_EMOJI_UPLOAD_SIZE = 128   # px; app-emoji upload size (icons upscale fine)

# colored-circle emoji + reference RGB: the week-view fallback when a badge
# icon/emoji isn't available for an event
_DOTS = [
    ("\U0001f534", (237, 66, 69)),    # red
    ("\U0001f7e0", (231, 118, 40)),   # orange
    ("\U0001f7e1", (240, 180, 40)),   # yellow
    ("\U0001f7e2", (67, 181, 129)),   # green
    ("\U0001f535", (53, 123, 213)),   # blue
    ("\U0001f7e3", (155, 89, 220)),   # purple
    ("\U0001f7e4", (150, 90, 60)),    # brown
    ("⚫", (40, 40, 40)),         # black
    ("⚪", (230, 230, 230)),      # white
]


def _dot_emoji(color: tuple[str, ...]) -> str:
    # middle gradient stop reads as the color's identity (CF: blue, LC: yellow)
    rgb = hex_to_rgb(color[len(color) // 2])
    return min(_DOTS, key=lambda dot: sum((a - b) ** 2 for a, b in zip(dot[1], rgb)))[0]


# ------------------------------------------------------------------ #
#  Config: calendars + badge urls (source of truth is a JSON file)   #
# ------------------------------------------------------------------ #

@dataclass
class Rule:
    match: str                       # lowercase substring of the event title
    color: tuple[str, ...] | None    # None = inherit the calendar color
    badge: str | None                # None = inherit the calendar badge


@dataclass
class Calendar:
    name: str
    cal_id: str
    color: tuple[str, ...]
    ics: str = ""
    badge: str = ""
    rules: list[Rule] = field(default_factory=list)


def _parse_color(v) -> tuple[str, ...]:
    if isinstance(v, list):
        return tuple(str(c).strip() for c in v if str(c).strip()) or ("#5865f2",)
    return ((v or "#5865f2").strip(),)


def load_config() -> tuple[list[Calendar], dict[str, str]]:
    """Read the configured calendar list and badge-icon urls.

    A missing file returns ([], {}) (the feature just isn't set up yet); a
    malformed file raises so the scheduler's failure counter surfaces it,
    instead of the bot silently idling on a typo.
    """
    try:
        with open(SCHEDULE_CALENDARS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], {}

    badges = {str(k): str(v) for k, v in (data.get("badges") or {}).items()}
    cals = []
    for c in data.get("calendars", []):
        cid = (c.get("id") or "").strip()
        name = (c.get("name") or cid or "?").strip()
        if not cid and not c.get("ics"):
            continue
        rules = [
            Rule(
                match=str(r.get("match") or "").lower(),
                color=_parse_color(r["color"]) if "color" in r else None,
                badge=str(r["badge"]) if "badge" in r else None,
            )
            for r in (c.get("rules") or [])
            if r.get("match")
        ]
        cals.append(Calendar(
            name=name,
            cal_id=cid,
            color=_parse_color(c.get("color")),
            ics=(c.get("ics") or "").strip(),
            badge=str(c.get("badge") or ""),
            rules=rules,
        ))
    return cals, badges


def _style(cal: Calendar, title: str) -> tuple[tuple[str, ...], str]:
    """Resolve (color, badge) for an event: first matching rule wins, else the
    calendar defaults."""
    t = title.lower()
    for r in cal.rules:
        if r.match in t:
            return (r.color or cal.color, cal.badge if r.badge is None else r.badge)
    return (cal.color, cal.badge)


# ------------------------------------------------------------------ #
#  Normalized event model                                            #
# ------------------------------------------------------------------ #

@dataclass
class Event:
    cal_name: str
    color: tuple[str, ...]
    title: str
    all_day: bool
    badge: str = ""
    # timed events: tz-aware start. all-day: start is None and the inclusive
    # Central date span lives in start_date/end_date.
    start: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None

    def sort_key(self):
        # all-day first within a day, then by start time
        if self.all_day:
            return (0, 0)
        return (1, int(self.start.timestamp()))


@dataclass
class FetchResult:
    events: list[Event] = field(default_factory=list)
    status: str = "failed"   # "fresh" | "stale" (reused last-good) | "failed"


# per-calendar last-good events, so a transient fetch failure reuses data instead
# of blanking that source. In-memory only — cleared on restart, never persisted,
# so it can't drift across deploys.
_last_good: dict[str, list[Event]] = {}
# per-calendar consecutive fetch failures, feeding logbus's escalation threshold.
_fail_counts: dict[str, int] = {}
# calendars whose ICS RRULE limitation was already noted (once per process).
_rrule_noted: set[str] = set()


# ------------------------------------------------------------------ #
#  Fetch: Google Calendar API, then ICS, then last-good cache        #
# ------------------------------------------------------------------ #

def _parse_dt(node: dict) -> tuple[datetime | None, date | None]:
    """Parse a Google start/end node -> (aware datetime | None, date | None).

    Exactly one is set for a valid node; a date means an all-day boundary
    (Google uses YYYY-MM-DD with an exclusive end).
    """
    if "date" in node:
        return None, date.fromisoformat(node["date"])
    raw = node.get("dateTime")
    if not raw:
        return None, None
    dt = datetime.fromisoformat(raw)         # 3.11+ handles Z and offsets
    if dt.tzinfo is None:
        tzname = node.get("timeZone")
        dt = dt.replace(tzinfo=ZoneInfo(tzname) if tzname else TZ)
    return dt, None


def _make_event(cal: Calendar, title: str, **kwargs) -> Event:
    color, badge = _style(cal, title)
    return Event(cal.name, color, title, badge=badge, **kwargs)


def _all_day_event(cal: Calendar, title: str, start: date, end_exclusive: date | None) -> Event:
    """Google and ICS both use an EXCLUSIVE end date for all-day events; store an
    inclusive last day so the rest of the module never re-derives it."""
    last = end_exclusive - timedelta(days=1) if end_exclusive else start
    if last < start:
        last = start
    return _make_event(cal, title, all_day=True, start_date=start, end_date=last)


async def _fetch_api(session, cal: Calendar, lo: datetime, hi: datetime) -> list[Event]:
    base = f"https://www.googleapis.com/calendar/v3/calendars/{quote(cal.cal_id, safe='')}/events"
    params = {
        "key": GOOGLE_API_KEY,
        "timeMin": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeMax": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": "2500",
    }
    events: list[Event] = []
    page_token = None
    for _ in range(10):  # hard cap on pagination
        if page_token:
            params["pageToken"] = page_token
        async with session.get(base, params=params) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                msg = (body or {}).get("error", {}).get("message", body)
                raise RuntimeError(f"API {resp.status}: {msg}")
        for item in body.get("items", []):
            if item.get("status") == "cancelled":
                continue
            ev = _item_to_event(cal, item)
            if ev:
                events.append(ev)
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return events


def _item_to_event(cal: Calendar, item: dict) -> Event | None:
    title = (item.get("summary") or "(untitled)").strip()
    s_dt, s_date = _parse_dt(item.get("start") or {})
    if s_date:
        _, e_date = _parse_dt(item.get("end") or {})
        return _all_day_event(cal, title, s_date, e_date)
    if not s_dt:
        return None
    return _make_event(cal, title, all_day=False, start=s_dt)


# ---- minimal ICS fallback (no RRULE expansion; Google feeds mostly ship
# discrete VEVENTs, and the API is the primary path anyway) ----

def _ics_unfold(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and out:      # RFC 5545 line folding
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _ics_parse_dt(value: str, params: dict) -> tuple[datetime | None, date | None]:
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        return None, datetime.strptime(value, "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), None
    try:
        naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None, None
    try:
        tz = ZoneInfo(params["TZID"]) if params.get("TZID") else TZ
    except Exception:
        tz = TZ
    return naive.replace(tzinfo=tz), None


def _parse_ics(cal: Calendar, text: str, window_lo: date, window_hi: date) -> tuple[list[Event], int]:
    """Parse VEVENTs into Events. Returns (events, skipped_rrule_count); the
    caller decides how to surface the skip count."""
    events: list[Event] = []
    cur: dict | None = None
    skipped_rrule = 0
    for line in _ics_unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                ev = _ics_to_event(cal, cur, window_lo, window_hi)
                if ev is None and cur.get("_rrule"):
                    skipped_rrule += 1
                elif ev:
                    events.append(ev)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        name, value = line.split(":", 1)
        parts = name.split(";")
        key = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v
        if key == "SUMMARY":
            cur["summary"] = value
        elif key == "DTSTART":
            cur["start"] = _ics_parse_dt(value, params)
        elif key == "DTEND":
            cur["end"] = _ics_parse_dt(value, params)
        elif key == "RRULE":
            cur["_rrule"] = True
    return events, skipped_rrule


def _ics_to_event(cal: Calendar, cur: dict, window_lo: date, window_hi: date) -> Event | None:
    if cur.get("_rrule") or "start" not in cur:
        return None
    title = (cur.get("summary") or "(untitled)").strip()
    s_dt, s_date = cur["start"]
    end = cur.get("end")
    if s_date:
        ev = _all_day_event(cal, title, s_date, end[1] if end else None)
        if ev.end_date < window_lo or ev.start_date > window_hi:
            return None
        return ev
    if not s_dt:
        return None
    e_dt = (end[0] if end else None) or s_dt
    if s_dt.astimezone(TZ).date() > window_hi or e_dt.astimezone(TZ).date() < window_lo:
        return None
    return _make_event(cal, title, all_day=False, start=s_dt)


async def _fetch_ics(session, cal: Calendar, lo: datetime, hi: datetime) -> list[Event]:
    async with session.get(cal.ics) as resp:
        if resp.status != 200:
            raise RuntimeError(f"ICS HTTP {resp.status}")
        text = await resp.text()
    events, skipped = _parse_ics(cal, text, lo.astimezone(TZ).date(), hi.astimezone(TZ).date())
    if skipped and cal.name not in _rrule_noted:
        _rrule_noted.add(cal.name)
        print(f"[SCHEDULE] {cal.name}: skipped {skipped} recurring ICS event(s) (no RRULE expansion in fallback)")
    return events


async def _fetch_calendar(session, cal: Calendar, lo: datetime, hi: datetime) -> FetchResult:
    """Fetch one calendar, fail-soft: API first, then the ICS url, then the
    last-good in-memory result (marked stale) rather than dropping the source."""
    errors: list[str] = []
    events: list[Event] | None = None

    if GOOGLE_API_KEY and cal.cal_id:
        try:
            events = await _fetch_api(session, cal, lo, hi)
        except Exception as e:
            errors.append(f"api: {e!r}")
    if events is None and cal.ics:
        try:
            events = await _fetch_ics(session, cal, lo, hi)
        except Exception as e:
            errors.append(f"ics: {e!r}")

    if events is not None:
        _fail_counts.pop(cal.name, None)
        _last_good[cal.name] = events
        return FetchResult(events, "fresh")

    if not errors:
        errors.append("no usable source (need GOOGLE_API_KEY+id, or an ics url)")
    n = _fail_counts.get(cal.name, 0) + 1
    _fail_counts[cal.name] = n
    log_if_persistent(n, f"[SCHEDULE] fetch failed for {cal.name} (attempt {n}): {'; '.join(errors)}")

    prior = _last_good.get(cal.name)
    if prior is not None:
        return FetchResult(list(prior), "stale")
    return FetchResult()


# ------------------------------------------------------------------ #
#  Badges: platform icons for the grid + application emojis          #
# ------------------------------------------------------------------ #

_badge_icons: dict[str, Image.Image] = {}   # name -> BADGE_SIZE RGBA, for the grid
_badge_png: dict[str, bytes] = {}           # name -> emoji-sized PNG, for upload
_badge_failed: set[str] = set()             # don't refetch/relog every cycle
# emoji-name override per badge; discord-avatar badges embed the avatar hash so
# an avatar change mints a fresh emoji instead of serving the stale image
_badge_emoji_names: dict[str, str] = {}

_emoji_strs: dict[str, str] = {}            # name -> "<:sched_x:id>"
_emoji_failed: set[str] = set()
_app_emojis: dict[str, discord.Emoji] | None = None   # fetched once per process


def _circle_crop(im: Image.Image) -> Image.Image:
    """Round-mask an avatar the way Discord displays them."""
    im = im.convert("RGBA")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, im.size[0] - 1, im.size[1] - 1], fill=255)
    im.putalpha(mask)
    return im


def _store_badge(name: str, im: Image.Image) -> None:
    _badge_icons[name] = im.resize((BADGE_SIZE, BADGE_SIZE), Image.LANCZOS)
    buf = io.BytesIO()
    im.resize((_EMOJI_UPLOAD_SIZE, _EMOJI_UPLOAD_SIZE), Image.LANCZOS).save(buf, "PNG")
    _badge_png[name] = buf.getvalue()


async def _ensure_badge_icons(bot, badge_urls: dict[str, str], needed: set[str]) -> None:
    """Load every needed badge icon. Two source kinds:

    * "discord-avatar:<user_id>" — the user's current Discord avatar,
      circle-cropped. Fetched once per process (never disk-cached) so it always
      tracks the live avatar; the emoji name embeds the avatar hash.
    * anything else — a url to a square image: memory -> disk cache -> fetch.

    Fail-soft per badge; a failed badge just falls back to colored dots/strips.
    """
    for name in needed:
        if not name or name in _badge_icons or name in _badge_failed:
            continue
        url = badge_urls.get(name)
        if not url:
            _badge_failed.add(name)
            print(f"[SCHEDULE] badge '{name}' has no url in the badges config — using color fallback")
            continue

        if url.startswith("discord-avatar:"):
            try:
                user = await bot.fetch_user(int(url.split(":", 1)[1]))
                avatar = user.display_avatar.with_size(_EMOJI_UPLOAD_SIZE)
                data = await avatar.read()
                _store_badge(name, _circle_crop(Image.open(io.BytesIO(data))))
                _badge_emoji_names[name] = f"sched_{name}_{avatar.key[:8]}"
            except Exception as e:
                _badge_failed.add(name)
                print(f"[SCHEDULE] avatar badge '{name}' failed: {e!r} — using color fallback")
            continue

        path = os.path.join(_BADGE_CACHE_DIR, f"{re.sub(r'[^a-zA-Z0-9_-]', '_', name)}.png")
        data = None
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read()
        else:
            try:
                async with bot.http_session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = await resp.read()
                # content-sniff: some icon services return HTML with status 200
                Image.open(io.BytesIO(data)).verify()
                os.makedirs(_BADGE_CACHE_DIR, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(data)
            except Exception as e:
                _badge_failed.add(name)
                print(f"[SCHEDULE] badge fetch failed for '{name}': {e!r} — using color fallback")
                continue
        try:
            _store_badge(name, Image.open(io.BytesIO(data)).convert("RGBA"))
        except Exception as e:
            _badge_failed.add(name)
            print(f"[SCHEDULE] badge decode failed for '{name}': {e!r} — using color fallback")


async def _badge_emoji(bot, name: str) -> str:
    """Application-emoji string for a badge, creating the emoji on first use.
    Returns "" when unavailable (caller falls back to a colored dot)."""
    if not name or name in _emoji_failed:
        return ""
    if name in _emoji_strs:
        return _emoji_strs[name]
    if name not in _badge_png:
        return ""   # icon not loaded (yet); retry next cycle without marking failed
    global _app_emojis
    ename = _badge_emoji_names.get(name) or ("sched_" + re.sub(r"[^a-zA-Z0-9_]", "_", name))[:32]
    try:
        if _app_emojis is None:
            _app_emojis = {e.name: e for e in await bot.fetch_application_emojis()}
        emoji = _app_emojis.get(ename)
        if emoji is None:
            emoji = await bot.create_application_emoji(name=ename, image=_badge_png[name])
            _app_emojis[ename] = emoji
            # a hash-suffixed (avatar) emoji replaces its older versions
            if name in _badge_emoji_names:
                prefix = f"sched_{name}_"
                for stale in [n for n in _app_emojis if n.startswith(prefix) and n != ename]:
                    try:
                        await _app_emojis.pop(stale).delete()
                    except Exception:
                        pass
        _emoji_strs[name] = str(emoji)
        return _emoji_strs[name]
    except Exception as e:
        _emoji_failed.add(name)
        print(f"[SCHEDULE] app emoji for '{name}' failed: {e!r} — using color fallback")
        return ""


# ------------------------------------------------------------------ #
#  Week view (two embeds: the week itself + follow links)            #
# ------------------------------------------------------------------ #

def _google_add_link(cal: Calendar) -> str | None:
    if not cal.cal_id:
        return None
    cid = base64.b64encode(cal.cal_id.encode()).decode().rstrip("=")
    return f"https://calendar.google.com/calendar/render?cid={cid}"


def _ical_link(cal: Calendar) -> str | None:
    if cal.ics:
        return cal.ics
    if cal.cal_id:
        return f"https://calendar.google.com/calendar/ical/{quote(cal.cal_id, safe='')}/public/basic.ics"
    return None


def _event_line(ev: Event, badge_emoji: dict[str, str]) -> str:
    mark = badge_emoji.get(ev.badge) or _dot_emoji(ev.color)
    if ev.all_day:
        return f"{mark} **{ev.title}** · {ev.cal_name} · all day"
    # one time only: the dynamic timestamp renders in each viewer's local zone
    # (time-only style — the date already lives in the day header)
    return f"{mark} **{ev.title}** · <t:{int(ev.start.timestamp())}:t>"


def build_week_embeds(events: list[Event], calendars: list[Calendar], now: datetime,
                      last_synced: int, any_stale: bool,
                      badge_emoji: dict[str, str]) -> list[discord.Embed]:
    today = now.astimezone(TZ).date()

    # bucket events onto Central dates for the next 7 days
    buckets: dict[date, list[Event]] = {today + timedelta(days=i): [] for i in range(7)}
    for ev in events:
        if ev.all_day:
            day = ev.start_date
            while day <= ev.end_date:
                if day in buckets:
                    buckets[day].append(ev)
                day += timedelta(days=1)
        else:
            day = ev.start.astimezone(TZ).date()
            if day in buckets:
                buckets[day].append(ev)

    # One embed per day: each day is its own card with its own accent color
    # (alternating shades, today in blurple) so day boundaries are unmissable.
    day_embeds: list[discord.Embed] = []
    for i in range(7):
        day = today + timedelta(days=i)
        day_events = sorted(buckets[day], key=lambda e: e.sort_key())
        if not day_events:
            continue  # omit empty days (matches the mock)
        header = day.strftime("%a, %b %-d")
        if i == 0:
            header += "  ← today"

        lines = [_event_line(ev, badge_emoji) for ev in day_events[:_MAX_PER_DAY]]
        if len(day_events) > _MAX_PER_DAY:
            lines.append(f"*+{len(day_events) - _MAX_PER_DAY} more*")
        value = "\n".join(lines)
        if len(value) > _FIELD_CAP:
            value = value[:_FIELD_CAP - 20].rsplit("\n", 1)[0] + "\n*…*"
        color = WEEK_COLOR if i == 0 else _DAY_COLORS[len(day_embeds) % 2]
        day_embeds.append(discord.Embed(title=header, description=value, color=color))

    if day_embeds:
        day_embeds[0].set_author(name="This Week")
    else:
        day_embeds.append(discord.Embed(
            title="This Week", description="*Nothing scheduled in the next 7 days.*",
            color=WEEK_COLOR))

    # add-to-calendar block + staleness indicator, kept subtle: no embed title,
    # everything in Discord subtext (-#) so it reads as a small gray footer
    follow = discord.Embed(color=FOLLOW_COLOR)
    link_lines = ["-# **Add to calendar**"]
    for cal in calendars:
        parts = []
        g = _google_add_link(cal)
        ics = _ical_link(cal)
        if g:
            parts.append(f"[Google]({g})")
        if ics:
            parts.append(f"[iCal]({ics})")
        mark = badge_emoji.get(cal.badge) or _dot_emoji(cal.color)
        line = f"-# {mark} {cal.name}"
        link_lines.append(f"{line} · {' · '.join(parts)}" if parts else line)

    tail = f"-# Last synced <t:{last_synced}:R> · times shown in Central + your local time"
    if any_stale:
        tail += "\n-# ⚠️ some sources are showing cached data"
    follow.description = ("\n".join(link_lines) + "\n" + tail)[:4096]

    embeds = day_embeds + [follow]
    # Discord rejects the whole edit if the embeds together exceed 6000 chars;
    # trim event lines from the busiest days until we're safely under.
    def total_len() -> int:
        return sum(len(e.title or "") + len(e.description or "") for e in embeds)
    while total_len() > _MESSAGE_EMBED_CAP:
        longest = max(day_embeds, key=lambda e: len(e.description or ""))
        head, _, _ = (longest.description or "").rpartition("\n")
        if not head:
            break
        longest.description = head + "\n*…*"
    return embeds


# ------------------------------------------------------------------ #
#  Month image                                                       #
# ------------------------------------------------------------------ #

def _month_time_label(dt: datetime) -> str:
    d = dt.astimezone(TZ)
    if d.minute == 0:
        return d.strftime("%-I%p").lower()          # "9am"
    return d.strftime("%-I:%M%p").lower()           # "1:35pm"


def _to_grid_events(events: list[Event]) -> list[GridEvent]:
    out: list[GridEvent] = []
    for ev in events:
        if ev.all_day:
            out.append(GridEvent(ev.title, ev.color, ev.start_date, ev.end_date,
                                 badge=ev.badge))
        else:
            d = ev.start.astimezone(TZ)
            out.append(GridEvent(ev.title, ev.color, d.date(), d.date(),
                                 timed_label=_month_time_label(ev.start),
                                 start_minutes=d.hour * 60 + d.minute,
                                 badge=ev.badge))
    return out


# ------------------------------------------------------------------ #
#  Message management (edit in place, recreate once if deleted)      #
# ------------------------------------------------------------------ #

async def _pin(msg: discord.Message):
    try:
        await msg.pin()
    except Exception as e:
        print(f"[SCHEDULE] could not pin {msg.id}: {e}")


async def _sync_message(channel, mid: int | None, *,
                        embeds: list | None = None, image: bytes | None = None) -> int | None:
    """Edit a pinned view in place, recreating it once if it was deleted.

    Returns the new message id when one was created (the caller persists it),
    else None. No DB access here — persistence stays in sync_once. Discord
    errors other than a deleted message propagate to the scheduler loop, whose
    failure counter owns alerting cadence.
    """
    def month_file() -> discord.File:
        return discord.File(io.BytesIO(image), filename="month.png")

    if mid:
        try:
            kwargs = {"embeds": embeds} if image is None else {"attachments": [month_file()]}
            # partial message: edit without a fetch round-trip; a deleted
            # message still raises NotFound from the edit itself
            await channel.get_partial_message(mid).edit(**kwargs)
            return None
        except discord.NotFound:
            pass  # deleted — recreate below (once)
    kwargs = {"embeds": embeds} if image is None else {"file": month_file()}
    msg = await channel.send(**kwargs)
    await _pin(msg)
    return msg.id


@dataclass
class _MonthCache:
    """Skip the render + upload when the month view's inputs are unchanged.

    In-memory only: a restart renders and edits once, which also recreates the
    message if it was deleted while the bot was down. Deletions during uptime
    set `force` via on_message_delete. Only updated after a successful sync, so
    a failed edit retries next cycle instead of wedging on stale state.
    """
    key: tuple | None = None
    png: bytes | None = None
    force: bool = False


_month_cache = _MonthCache()


def on_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    """Re-send the month view next cycle if its message was deleted (the week
    view needs nothing here — its every-cycle edit hits NotFound and recreates)."""
    if payload.channel_id != SCHEDULE_CHANNEL_ID:
        return
    if payload.message_id == schedule_state_get()["month_message_id"]:
        _month_cache.force = True


async def _sync_month(channel, mid: int | None, ct: datetime,
                      grid_events: list[GridEvent],
                      legend: list[tuple[str, tuple[str, ...], str]]) -> int | None:
    """Render and upload the month grid only when its inputs changed (or the
    message needs recreating); the steady-state cycle costs a tuple compare."""
    key = (
        ct.date(),   # the today-pill moves at midnight even if events don't
        tuple((g.title, g.color, g.start_date, g.end_date, g.timed_label, g.badge)
              for g in grid_events),
        tuple(legend),
        frozenset(_badge_icons),   # a late-loading icon must trigger a re-render
    )
    changed = key != _month_cache.key
    if not (changed or _month_cache.force):
        return None

    png = _month_cache.png
    if changed or png is None:
        png = await asyncio.to_thread(render_month_png, ct.year, ct.month, ct.date(),
                                      grid_events, legend, _badge_icons)
    new_id = await _sync_message(channel, mid, image=png)
    _month_cache.key, _month_cache.png, _month_cache.force = key, png, False
    return new_id


# ------------------------------------------------------------------ #
#  Sync cycle + scheduler loop                                       #
# ------------------------------------------------------------------ #

def _window(now: datetime) -> tuple[datetime, datetime]:
    """Fetch window covering the month grid plus the next-7-days week view."""
    ct = now.astimezone(TZ)
    first_of_month = ct.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # grid can show a few trailing days of next month; week view can reach today+7.
    lo = first_of_month - timedelta(days=7)
    hi = max(first_of_month + timedelta(days=45), ct + timedelta(days=15))
    return lo.astimezone(UTC), hi.astimezone(UTC)


async def sync_once(bot) -> str:
    """One regenerate-and-push cycle. Returns "ok" or "unconfigured"; raises on
    failure so the scheduler loop's counter owns alerting cadence."""
    calendars, badge_urls = load_config()
    if not calendars:
        return "unconfigured"

    channel = bot.get_channel(SCHEDULE_CHANNEL_ID) or await bot.fetch_channel(SCHEDULE_CHANNEL_ID)
    now = datetime.now(tz=UTC)
    lo, hi = _window(now)

    results = await asyncio.gather(*[
        _fetch_calendar(bot.http_session, cal, lo, hi) for cal in calendars
    ])

    any_fresh = any(r.status == "fresh" for r in results)
    any_stale = any(r.status == "stale" for r in results)
    events = [e for r in results for e in r.events]

    # Nobody produced anything and there's no cache to fall back on: leave the
    # messages as-is (never blank them) and let the loop's counter escalate.
    if not events and not any_fresh:
        raise RuntimeError("every source failed and no cached data (messages left stale)")

    # badge icons + app emojis (each fail-soft to colored dots/strips)
    needed = {ev.badge for ev in events} | {cal.badge for cal in calendars}
    needed.discard("")
    await _ensure_badge_icons(bot, badge_urls, needed)
    badge_emoji = {name: e for name in needed if (e := await _badge_emoji(bot, name))}

    state = schedule_state_get()
    last_synced = int(time.time()) if any_fresh else state["last_synced_at"]

    week_embeds = build_week_embeds(events, calendars, now, last_synced, any_stale, badge_emoji)
    new_week = await _sync_message(channel, state["week_message_id"], embeds=week_embeds)

    ct = now.astimezone(TZ)
    new_month = await _sync_month(channel, state["month_message_id"], ct,
                                  _to_grid_events(events),
                                  [(c.name, c.color, c.badge) for c in calendars])

    if new_week or new_month:
        schedule_set_message_ids(week_message_id=new_week, month_message_id=new_month)
    if any_fresh:
        schedule_set_last_synced(last_synced)
    return "ok"


async def schedule_scheduler(bot) -> None:
    await bot.wait_until_ready()
    await asyncio.sleep(5)
    print(f"✅ Schedule scheduler started (every {SCHEDULE_POLL_SECONDS}s)")
    fails = 0
    warned_unconfigured = False
    while not bot.is_closed():
        try:
            status = await sync_once(bot)
            fails = 0
            if status == "unconfigured":
                if not warned_unconfigured:
                    warned_unconfigured = True
                    print("[SCHEDULE] no calendars configured (schedule_calendars.json) — idling until set up")
            else:
                warned_unconfigured = False
        except Exception as e:
            fails += 1
            log_if_persistent(fails, f"[SCHEDULE] sync cycle failed (attempt {fails}): {e!r}")
        await asyncio.sleep(SCHEDULE_POLL_SECONDS)


def start(bot) -> None:
    """Launch the sync loop once (idempotent across reconnects)."""
    if not SCHEDULE_CHANNEL_ID:
        print("[SCHEDULE] SCHEDULE_CHANNEL_ID not set — scheduler disabled")
        return
    if getattr(bot, "_schedule_started", False):
        return
    bot._schedule_started = True
    bot.loop.create_task(schedule_scheduler(bot))
