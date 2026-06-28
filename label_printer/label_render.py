#!/usr/bin/env python3
"""
Renders the universal fridge label (breast milk + leftovers) as a 1-bit-friendly
PIL image for the Brother QL-820NWB (62mm continuous tape, 696px @ 300dpi).

The label has one job: record WHEN something went in and WHEN it's no longer
good -- the two facts you can't recover or remember. What it is stays implicit
(it's in your hand), so there's no dialing: a single press stamps "now" and
prints every computed deadline.

Layout, top to bottom:
  * day-of-week strip with the current day inverted (scan the fridge for oldest)
  * IN / EXPRESSED timestamp + day/night ("sleepy" milk) icon, with the dialed
    oz beside the icon when the M5Dial knob was turned
  * USE BY matrix: Food / Milk columns x Room / Fridge / Freezer rows, Fridge
    inverted as the common-case anchor
  * a Thawed write-in blank (the one value we can't know at print time)
  * a one-line safety note for the raw-meat exception to the 4-day rule

Kept free of brother_ql imports so it can be rendered/previewed off the printer.
"""

import calendar
import math
from datetime import timedelta

from PIL import Image, ImageDraw, ImageFont

LABEL_WIDTH_PX = 696          # printable width for 62mm media at 300 dpi

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "Arialbd.ttf",
]

DAY_START, DAY_END = 7, 19     # 7am-7pm = day (sun), otherwise night (moon)
WEEKDAY_LETTERS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]

BLACK = "black"
WHITE = "white"


# ----------------------------------------------------------------------
# Fonts / text helpers
# ----------------------------------------------------------------------

def load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


_measure = ImageDraw.Draw(Image.new("RGB", (10, 10)))


def text_w(text, font):
    return _measure.textlength(text, font=font)


def text_h(text, font):
    b = _measure.textbbox((0, 0), text, font=font)
    return b[3] - b[1]


def fitted_font(text, start_size, min_size, max_w):
    size = start_size
    while size > min_size:
        f = load_font(size)
        if _measure.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return load_font(min_size)


def draw_left(d, x, ycenter, text, font, fill):
    """Draw left-aligned at x, vertically centered on ycenter. Returns width."""
    b = d.textbbox((0, 0), text, font=font)
    h = b[3] - b[1]
    d.text((x - b[0], ycenter - h / 2 - b[1]), text, font=font, fill=fill)
    return b[2] - b[0]


def draw_right(d, x, ycenter, text, font, fill):
    """Draw right-aligned so the text ends at x, centered on ycenter."""
    b = d.textbbox((0, 0), text, font=font)
    w = b[2] - b[0]
    h = b[3] - b[1]
    d.text((x - w - b[0], ycenter - h / 2 - b[1]), text, font=font, fill=fill)
    return w


def fmt_time(dt):
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def add_months(dt, months):
    m = dt.month - 1 + months
    y = dt.year + m // 12
    m = m % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return dt.replace(year=y, month=m, day=min(dt.day, last_day))


# ----------------------------------------------------------------------
# Icons (simple 1-bit glyphs; `color` lets them invert on dark rows)
# ----------------------------------------------------------------------

def draw_sun(d, cx, cy, r, color=BLACK):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    ray_in, ray_out = r + 6, r + 6 + int(r * 0.55)
    w = max(3, int(r * 0.22))
    for k in range(8):
        a = math.pi / 4 * k
        d.line([(cx + math.cos(a) * ray_in, cy + math.sin(a) * ray_in),
                (cx + math.cos(a) * ray_out, cy + math.sin(a) * ray_out)],
               fill=color, width=w)


def draw_moon(d, cx, cy, r, color=BLACK, bg=WHITE):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    off = int(r * 0.55)
    d.ellipse([cx - r + off, cy - r - int(r * 0.18),
               cx + r + off, cy + r - int(r * 0.18)], fill=bg)


def draw_thermometer(d, cx, cy, s, color):
    w = max(3, s // 7)
    br = int(s * 0.24)
    top = int(cy - s / 2)
    bulb_cy = int(cy + s / 2 - br)
    d.rounded_rectangle([cx - w, top, cx + w, bulb_cy], radius=w, outline=color, width=3)
    d.ellipse([cx - br, bulb_cy - br, cx + br, bulb_cy + br], outline=color, width=3)
    d.ellipse([cx - br + 4, bulb_cy - br + 4, cx + br - 4, bulb_cy + br - 4], fill=color)
    d.rectangle([cx - w + 4, bulb_cy - int(s * 0.16), cx + w - 4, bulb_cy], fill=color)


def draw_fridge(d, cx, cy, s, color):
    w = int(s * 0.66)
    h = s
    x0, y0 = cx - w // 2, int(cy - h / 2)
    x1, y1 = cx + w // 2, int(cy + h / 2)
    d.rounded_rectangle([x0, y0, x1, y1], radius=5, outline=color, width=3)
    ysplit = y0 + int(h * 0.40)
    d.line([x0, ysplit, x1, ysplit], fill=color, width=3)
    hx = x0 + int(w * 0.24)
    d.line([hx, y0 + int(h * 0.13), hx, y0 + int(h * 0.30)], fill=color, width=3)
    d.line([hx, ysplit + int(h * 0.10), hx, ysplit + int(h * 0.30)], fill=color, width=3)


def draw_snowflake(d, cx, cy, s, color):
    r = s // 2
    w = max(2, s // 11)
    fk = int(r * 0.36)
    for k in range(3):
        a = math.pi / 3 * k
        dx, dy = math.cos(a) * r, math.sin(a) * r
        d.line([cx - dx, cy - dy, cx + dx, cy + dy], fill=color, width=w)
    for k in range(6):
        a = math.pi / 3 * k
        tx, ty = cx + math.cos(a) * r, cy + math.sin(a) * r
        for da in (-0.6, 0.6):
            d.line([tx, ty,
                    tx - math.cos(a + da) * fk,
                    ty - math.sin(a + da) * fk], fill=color, width=w)


def draw_droplet(d, cx, cy, s, color):
    r = int(s * 0.36)
    bot = int(cy + s / 2)
    top = int(cy - s / 2)
    d.ellipse([cx - r, bot - 2 * r, cx + r, bot], fill=color)
    d.polygon([(cx, top), (cx - r, bot - r), (cx + r, bot - r)], fill=color)


def draw_meat(d, cx, cy, s, color):
    mr = int(s * 0.36)
    mx, my = cx + int(s * 0.14), cy - int(s * 0.08)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=color)
    bx = cx - int(s * 0.46)
    by = cy + int(s * 0.20)
    d.line([mx, my, bx + 5, by], fill=color, width=max(3, s // 6))
    knob = max(3, int(s * 0.14))
    d.ellipse([bx, by - knob, bx + 2 * knob, by + knob], fill=color)
    d.ellipse([bx - knob, by - 2 * knob, bx + knob, by], fill=color)


def _dashed_line(d, x0, x1, y, color, dash=10, gap=7, width=2):
    x = x0
    while x < x1:
        d.line([x, y, min(x + dash, x1), y], fill=color, width=width)
        x += dash + gap


# ----------------------------------------------------------------------
# Build the label image
# ----------------------------------------------------------------------

def build_label_image(now, oz=None):
    W = LABEL_WIDTH_PX
    BORDER = 4
    left = 24
    right = W - 24

    # --- deadlines ---
    room = now + timedelta(hours=4)        # room temp: 4 hours (fresh milk)
    fridge = now + timedelta(days=4)       # fridge: 4 days
    fz_food = add_months(now, 3)           # freezer, leftovers: ~3 months
    fz_milk = add_months(now, 6)           # freezer, milk: 6 months (best)

    def md(dt):                            # "Sep 27", + compact year on rollover
        s = f"{dt:%b} {dt.day}"
        return s + (f" '{dt.year % 100:02d}" if dt.year != now.year else "")

    in_label = "IN / EXPRESSED"
    date_str = f"{now:%a, %b} {now.day}, {now.year}"
    time_str = fmt_time(now)
    oz_str = f"{oz:.1f} oz" if oz is not None else None
    room_str = fmt_time(room)
    fridge_str = f"{fridge:%a, %b} {fridge.day} · {fmt_time(fridge)}"
    food_fz, milk_fz = md(fz_food), md(fz_milk)
    safety_str = "Raw meat, fish, poultry: 1–2 days"
    note_str = "24h · no refreeze"

    # --- fonts ---
    f_day = load_font(30)
    f_inlabel = load_font(21)
    f_time = load_font(30)
    f_oz = load_font(34)
    f_hdr = load_font(21)
    f_row = load_font(30)
    f_thaw = load_font(30)
    f_note = load_font(20)
    f_safety = load_font(21)

    ICON_R = 22
    ICON_HALF = ICON_R + 6 + int(ICON_R * 0.55)   # widest case (sun rays)

    oz_w = text_w(oz_str, f_oz) if oz_str else 0
    right_group_w = ICON_HALF * 2 + (14 + oz_w if oz_str else 0)
    date_max_w = (right - right_group_w - 16) - left
    f_date = fitted_font(date_str, 42, 26, max(140, date_max_w))

    # --- measured heights ---
    day_h = text_h("Sa", f_day)
    inlabel_h = text_h(in_label, f_inlabel)
    date_h = text_h(date_str, f_date)
    time_h = text_h(time_str, f_time)
    hdr_h = text_h("Food", f_hdr)
    row_h = text_h("Fridge", f_row)
    thaw_h = text_h("Thawed:", f_thaw)
    safety_h = text_h(safety_str, f_safety)

    strip_h = day_h + 20
    stack_h = inlabel_h + 6 + date_h + 4 + time_h
    band_h = 12 + max(stack_h, ICON_R * 2) + 12
    table_gap = 12
    header_row_h = hdr_h + 14
    drow_h = row_h + 16
    fridge_row_h = row_h + 22
    after_table = 14
    thaw_block_h = thaw_h + 16
    safety_block_h = safety_h + 12
    bottom_pad = 12

    total_h = int(BORDER + strip_h + 2 + band_h + 2 + table_gap
                  + header_row_h + 1 + drow_h + fridge_row_h + drow_h
                  + after_table + thaw_block_h + safety_block_h
                  + bottom_pad + BORDER)

    img = Image.new("RGB", (W, total_h), WHITE)
    d = ImageDraw.Draw(img)

    # --- day strip ---
    strip_top = BORDER
    strip_bot = strip_top + strip_h
    active = (now.weekday() + 1) % 7          # Mon=0..Sun=6 -> Sun-first index
    cell_w = (W - 2 * BORDER) / 7
    for i, lab in enumerate(WEEKDAY_LETTERS):
        x0 = BORDER + i * cell_w
        x1 = BORDER + (i + 1) * cell_w
        cy = (strip_top + strip_bot) / 2
        if i == active:
            d.rectangle([x0, strip_top, x1, strip_bot], fill=BLACK)
            b = d.textbbox((0, 0), lab, font=f_day)
            d.text(((x0 + x1) / 2 - (b[2] - b[0]) / 2 - b[0],
                    cy - (b[3] - b[1]) / 2 - b[1]), lab, font=f_day, fill=WHITE)
        else:
            b = d.textbbox((0, 0), lab, font=f_day)
            d.text(((x0 + x1) / 2 - (b[2] - b[0]) / 2 - b[0],
                    cy - (b[3] - b[1]) / 2 - b[1]), lab, font=f_day, fill=BLACK)
    d.line([BORDER, strip_bot, W - BORDER, strip_bot], fill=BLACK, width=2)

    # --- header band: timestamp left, icon (+oz) right ---
    band_top = strip_bot + 2
    band_bot = band_top + band_h
    cy0 = band_top + 12 + inlabel_h / 2
    cy1 = band_top + 12 + inlabel_h + 6 + date_h / 2
    cy2 = band_top + 12 + inlabel_h + 6 + date_h + 4 + time_h / 2
    draw_left(d, left, cy0, in_label, f_inlabel, BLACK)
    draw_left(d, left, cy1, date_str, f_date, BLACK)
    draw_left(d, left, cy2, time_str, f_time, BLACK)

    icon_cy = (band_top + band_bot) / 2
    is_day = DAY_START <= now.hour < DAY_END
    if oz_str:
        ox = right
        oz_left = draw_right(d, ox, icon_cy, oz_str, f_oz, BLACK)
        icon_cx = (right - oz_left - 16) - ICON_HALF
    else:
        icon_cx = (W - BORDER - 16) - ICON_HALF
    (draw_sun if is_day else draw_moon)(d, icon_cx, icon_cy, ICON_R, BLACK)
    d.line([BORDER, band_bot, W - BORDER, band_bot], fill=BLACK, width=2)

    # --- USE BY matrix ---
    milk_r = right
    food_r = right - 178
    icon_x = left + 16
    label_x = left + 38

    ty = band_bot + 2 + table_gap

    # header row
    hcy = ty + header_row_h / 2
    draw_left(d, left, hcy, "USE BY", f_hdr, BLACK)
    mw = draw_right(d, milk_r, hcy, "Milk", f_hdr, BLACK)
    draw_droplet(d, milk_r - mw - 14, hcy, 20, BLACK)
    fw = draw_right(d, food_r, hcy, "Food", f_hdr, BLACK)
    draw_meat(d, food_r - fw - 16, hcy, 20, BLACK)
    ty += header_row_h
    d.line([left, ty, right, ty], fill=BLACK, width=1)

    # Room row
    rcy = ty + drow_h / 2
    draw_thermometer(d, icon_x, rcy, 30, BLACK)
    draw_left(d, label_x, rcy, "Room", f_row, BLACK)
    draw_right(d, food_r, rcy, "–", f_row, BLACK)
    draw_right(d, milk_r, rcy, room_str, f_row, BLACK)
    ty += drow_h

    # Fridge row (inverted hero)
    d.rectangle([BORDER + 2, ty, W - BORDER - 2, ty + fridge_row_h], fill=BLACK)
    fcy = ty + fridge_row_h / 2
    draw_fridge(d, icon_x, fcy, 30, WHITE)
    draw_left(d, label_x, fcy, "Fridge", f_row, WHITE)
    draw_right(d, milk_r, fcy, fridge_str, f_row, WHITE)
    ty += fridge_row_h

    # Freezer row
    zcy = ty + drow_h / 2
    draw_snowflake(d, icon_x, zcy, 30, BLACK)
    draw_left(d, label_x, zcy, "Freezer", f_row, BLACK)
    draw_right(d, food_r, zcy, food_fz, f_row, BLACK)
    draw_right(d, milk_r, zcy, milk_fz, f_row, BLACK)
    ty += drow_h

    # --- Thawed write-in ---
    ty += after_table
    _dashed_line(d, left, right, ty, BLACK)
    tcy = ty + thaw_block_h / 2
    lbl_w = draw_left(d, left, tcy, "Thawed:", f_thaw, BLACK)
    note_w = draw_right(d, right, tcy, note_str, f_note, BLACK)
    line_x0 = left + lbl_w + 16
    line_x1 = right - note_w - 16
    if line_x1 > line_x0:
        d.line([line_x0, tcy + thaw_h / 2, line_x1, tcy + thaw_h / 2],
               fill=BLACK, width=2)
    ty += thaw_block_h

    # --- safety line ---
    scy = ty + safety_block_h / 2
    draw_left(d, left, scy, safety_str, f_safety, BLACK)

    # --- outer frame ---
    d.rectangle([2, 2, W - 3, total_h - 3], outline=BLACK, width=2)

    header_text = f"{date_str} {time_str}"
    return img, header_text
