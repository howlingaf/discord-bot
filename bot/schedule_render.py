"""Render the current month as a calendar-grid PNG (no headless browser).

Pure Pillow, styled to match the target mock: near-black canvas, a title with a
top-right color legend, a 7-column grid, today's date in a blurple pill, single
events drawn as a platform badge (or color strip) + dim time + bold title,
multi-day events drawn as filled bars spanning columns (wrapping across week
rows), and a graceful "+N more" when a day overflows.

Colors are (hex, ...) tuples: one entry renders solid, several render as a
linear gradient (the LeetCode/Codeforces brand looks). Badges are pre-sized
RGBA icons passed in by name — this module stays pure and does no I/O beyond
font files.

`render_month_png` is synchronous and CPU-bound; the sync loop calls it via
asyncio.to_thread so it never blocks the event loop.
"""

import calendar as _calendar
import functools
import io
from dataclasses import dataclass
from datetime import date

from PIL import Image, ImageDraw, ImageFont

# ---- palette ----
BG = (14, 15, 19)
CELL_BG = (23, 24, 29)
CELL_BG_OTHER = (16, 17, 21)      # days outside the current month
CELL_BORDER = (36, 38, 44)
TODAY_PILL = (88, 101, 242)       # blurple
TEXT = (233, 234, 236)
TITLE = (245, 246, 248)
TEXT_DIM = (122, 126, 134)
WEEKDAY = (108, 112, 120)
LEGEND_TEXT = (170, 173, 178)

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"

BADGE_SIZE = 16   # px, square; icons passed to render_month_png must be this size


@functools.lru_cache(maxsize=None)
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(f"{_FONT_DIR}/{name}", size)
    except Exception:
        return ImageFont.load_default()


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    """Parse "#rgb"/"#rrggbb" to an RGB tuple; blurple on garbage. Shared with
    schedule.py's emoji-dot matching so the two can't drift."""
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except Exception:
        return TODAY_PILL


def _stops(color: tuple[str, ...]) -> list[tuple[int, int, int]]:
    return [hex_to_rgb(c) for c in color] or [TODAY_PILL]


def _lerp(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient_seq(stops: list[tuple], n: int) -> list[tuple[int, int, int]]:
    """n colors linearly interpolated through the stops."""
    if n <= 1 or len(stops) == 1:
        return [stops[0]] * max(n, 1)
    segs = len(stops) - 1
    out = []
    for i in range(n):
        t = i / (n - 1) * segs
        k = min(int(t), segs - 1)
        out.append(_lerp(stops[k], stops[k + 1], t - k))
    return out


def _fill_shape(img: Image.Image, box: tuple, color: tuple[str, ...],
                radius: int, vertical: bool = False, ellipse: bool = False) -> None:
    """Fill a rounded-rect (or ellipse) with a solid color or linear gradient."""
    stops = _stops(color)
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    if len(stops) == 1 and not ellipse:
        ImageDraw.Draw(img).rounded_rectangle(box, radius=radius, fill=stops[0])
        return
    if len(stops) == 1:
        ImageDraw.Draw(img).ellipse(box, fill=stops[0])
        return
    grad = Image.new("RGB", (w, h))
    gd = ImageDraw.Draw(grad)
    n = h if vertical else w
    for i, c in enumerate(_gradient_seq(stops, n)):
        if vertical:
            gd.line([(0, i), (w - 1, i)], fill=c)
        else:
            gd.line([(i, 0), (i, h - 1)], fill=c)
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    if ellipse:
        md.ellipse([0, 0, w - 1, h - 1], fill=255)
    else:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.paste(grad, (x0, y0), mask)


def _readable_text(color: tuple[str, ...]) -> tuple[int, int, int]:
    stops = _stops(color)
    avg = tuple(sum(s[i] for s in stops) // len(stops) for i in range(3))
    lum = 0.299 * avg[0] + 0.587 * avg[1] + 0.114 * avg[2]
    return (18, 18, 20) if lum > 150 else (245, 245, 247)


@dataclass
class GridEvent:
    """A calendar event projected onto grid days (inclusive date span)."""
    title: str
    color: tuple[str, ...]  # 1 hex = solid, 2+ = gradient
    start_date: date
    end_date: date          # inclusive
    timed_label: str = ""   # e.g. "9am"; "" for all-day
    start_minutes: int = 0  # minutes since Central midnight, for in-day ordering
    badge: str = ""         # platform icon name; "" = color strip fallback

    @property
    def multi_day(self) -> bool:
        return self.end_date > self.start_date


def _truncate(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font) > max_w:
        text = text[:-1]
    return (text + ell) if text else ell


def render_month_png(
    year: int,
    month: int,
    today: date,
    events: list[GridEvent],
    legend: list[tuple[str, tuple[str, ...], str]],  # (name, color, badge) in display order
    badges: dict[str, Image.Image] | None = None,    # name -> BADGE_SIZE RGBA icon
) -> bytes:
    badges = badges or {}
    W = 1680
    margin = 28
    title_h = 64
    weekday_h = 34

    cal = _calendar.Calendar(firstweekday=6)  # Sunday-first
    weeks = cal.monthdatescalendar(year, month)
    rows = len(weeks)

    grid_w = W - 2 * margin
    cell_w = grid_w / 7
    cell_h = 150
    grid_h = int(cell_h * rows)
    H = margin + title_h + weekday_h + grid_h + margin

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_title = _font(True, 34)
    f_weekday = _font(True, 15)
    f_daynum = _font(True, 17)
    f_time = _font(False, 13)
    f_event = _font(True, 15)
    f_bar = _font(True, 14)
    f_more = _font(True, 13)
    f_legend = _font(False, 16)

    # ---- title ----
    d.text((margin, margin + 8), f"{_calendar.month_name[month]} {year}", font=f_title, fill=TITLE)

    # ---- legend (top-right): badge icon when available, else a color dot ----
    lx = W - margin
    for name, color, badge in reversed(legend):
        tw = d.textlength(name, font=f_legend)
        lx -= tw
        d.text((lx, margin + 18), name, font=f_legend, fill=LEGEND_TEXT)
        lx -= 12
        icon = badges.get(badge)
        if icon:
            img.paste(icon, (int(lx - BADGE_SIZE), margin + 20), icon)
        else:
            _fill_shape(img, (lx - 12, margin + 22, lx, margin + 34), color, radius=0, ellipse=True)
        lx -= 28

    grid_top = margin + title_h + weekday_h

    # ---- weekday header ----
    for i, nm in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]):
        d.text((margin + i * cell_w + 10, margin + title_h + 6), nm, font=f_weekday, fill=WEEKDAY)

    def cell_xy(col: int, row: int) -> tuple[float, float]:
        return margin + col * cell_w, grid_top + row * cell_h

    pad = 10
    daynum_h = 30
    bar_h = 22
    bar_gap = 4
    entry_h = 36          # single-day event: time line + title line

    # ---- cells + date numbers + today pill ----
    for row, week in enumerate(weeks):
        for col, day in enumerate(week):
            x0, y0 = cell_xy(col, row)
            x1, y1 = x0 + cell_w, y0 + cell_h
            in_month = day.month == month
            fill = CELL_BG if in_month else CELL_BG_OTHER
            d.rectangle([x0, y0, x1, y1], fill=fill, outline=CELL_BORDER, width=1)
            if not in_month:
                continue  # blank cell for adjacent-month days (matches the mock)
            num = str(day.day)
            if day == today:
                tw = d.textlength(num, font=f_daynum)
                d.rounded_rectangle([x0 + pad - 5, y0 + 6, x0 + pad + tw + 5, y0 + 28],
                                    radius=6, fill=TODAY_PILL)
                d.text((x0 + pad, y0 + 8), num, font=f_daynum, fill=(255, 255, 255))
            else:
                d.text((x0 + pad, y0 + 8), num, font=f_daynum, fill=TEXT_DIM)

    # ---- per-week: multi-day bars first (consistent lanes), then single events ----
    for row, week in enumerate(weeks):
        wk_start, wk_end = week[0], week[6]

        multi = []
        for ev in events:
            if not ev.multi_day:
                continue
            if ev.end_date < wk_start or ev.start_date > wk_end:
                continue
            scol = 0 if ev.start_date < wk_start else (ev.start_date - wk_start).days
            ecol = 6 if ev.end_date > wk_end else (ev.end_date - wk_start).days
            multi.append((scol, ecol, ev))
        multi.sort(key=lambda t: (t[0], -(t[1] - t[0])))

        # greedy lane assignment; each bar draws as soon as its lane is known
        lane_end: list[int] = []
        for scol, ecol, ev in multi:
            lane = None
            for i in range(len(lane_end)):
                if scol > lane_end[i]:
                    lane = i
                    break
            if lane is None:
                lane = len(lane_end)
                lane_end.append(ecol)
            else:
                lane_end[lane] = ecol

            x0, y0 = cell_xy(scol, row)
            x_end, _ = cell_xy(ecol, row)
            bx0 = x0 + 4
            bx1 = x_end + cell_w - 4
            by0 = y0 + daynum_h + lane * (bar_h + bar_gap)
            by1 = by0 + bar_h
            _fill_shape(img, (bx0, by0, bx1, by1), ev.color, radius=5)
            tx = bx0 + 8
            icon = badges.get(ev.badge)
            if icon:
                img.paste(icon, (int(bx0 + 5), int(by0 + (bar_h - BADGE_SIZE) // 2)), icon)
                tx = bx0 + 5 + BADGE_SIZE + 6
            label = _truncate(d, ev.title, f_bar, (bx1 - tx) - 8)
            d.text((tx, by0 + 3), label, font=f_bar, fill=_readable_text(ev.color))

        # single-day events, stacked per cell below the week's bar area
        singles_top = daynum_h + len(lane_end) * (bar_h + bar_gap)
        for col, day in enumerate(week):
            if day.month != month:
                continue
            day_singles = [ev for ev in events
                           if not ev.multi_day and ev.start_date == day]
            # all-day singles first, then chronological (label strings sort wrong:
            # "8pm" < "9am" alphabetically)
            day_singles.sort(key=lambda e: (e.timed_label != "", e.start_minutes))
            x0, y0 = cell_xy(col, row)
            avail = cell_h - singles_top - 8
            capacity = max(0, int(avail // entry_h))
            shown = day_singles[:capacity]
            for j, ev in enumerate(shown):
                ey = y0 + singles_top + j * entry_h
                icon = badges.get(ev.badge)
                if icon:
                    img.paste(icon, (int(x0 + pad), int(ey + 6)), icon)
                    tx = x0 + pad + BADGE_SIZE + 7
                else:
                    _fill_shape(img, (x0 + pad, ey, x0 + pad + 3, ey + entry_h - 8),
                                ev.color, radius=2, vertical=True)
                    tx = x0 + pad + 10
                maxw = x0 + cell_w - pad - tx
                if ev.timed_label:
                    d.text((tx, ey - 1), ev.timed_label, font=f_time, fill=TEXT_DIM)
                title = _truncate(d, ev.title, f_event, maxw)
                d.text((tx, ey + (15 if ev.timed_label else 7)), title, font=f_event, fill=TEXT)
            hidden = len(day_singles) - len(shown)
            if hidden > 0:
                d.text((x0 + pad, y0 + cell_h - 20), f"+{hidden} more", font=f_more, fill=TEXT_DIM)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
