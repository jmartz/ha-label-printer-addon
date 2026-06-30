#!/usr/bin/env python3
"""
Freeform label renderer for the custom-label designer.

Takes a JSON design spec (a list of positioned elements -- text, rectangles,
lines, QR codes, barcodes, uploaded images) plus a Brother media code and
renders a 1-bit-friendly PIL image sized exactly for that media. This is the
single source of truth: the browser designer lays elements out in the SAME
printer-pixel coordinate system and uses the SAME font files (served by the
add-on) so the on-screen canvas matches what PIL rasterises here.

Kept free of brother_ql's raster/convert imports so it can be previewed off the
printer (and unit-tested on a dev box). print_server.py does the printing.

Coordinate system: pixels at 300 dpi, origin top-left. For continuous media the
width is fixed and the length is either given (mm) or auto-fit to the content.
For die-cut/round media both dimensions are fixed by the die.
"""

import base64
import io
import math

from PIL import Image, ImageDraw, ImageFont

from brother_ql.labels import ALL_LABELS, FormFactor

PX_PER_MM = 300 / 25.4          # 11.811 px per mm at 300 dpi
MAX_TAPE_MM = 62                # QL-820NWB tops out at 62 mm wide media
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


# ----------------------------------------------------------------------
# Media table (driven by brother_ql so it can't drift)
# ----------------------------------------------------------------------

_KIND = {
    FormFactor.ENDLESS: "continuous",
    FormFactor.DIE_CUT: "die-cut",
    FormFactor.ROUND_DIE_CUT: "round",
}


def media_table():
    """Every QL-820NWB-compatible media, widest dimension <= 62 mm.

    Returns dicts: code, name, kind, width_px, length_px (0 == continuous),
    round (bool). Sorted continuous-first, then by width.
    """
    out = []
    for lab in ALL_LABELS:
        if max(lab.tape_size) > MAX_TAPE_MM:
            continue                       # 102 mm media won't fit this printer
        if lab.identifier == "62red":
            continue                       # needs the black/red ribbon; skip
        w, h = lab.dots_printable
        kind = _KIND.get(lab.form_factor, "die-cut")
        out.append({
            "code": lab.identifier,
            "name": lab.name,
            "kind": kind,
            "width_px": w,
            "length_px": 0 if kind == "continuous" else h,
            "round": kind == "round",
        })
    out.sort(key=lambda m: (m["kind"] != "continuous", m["width_px"], m["length_px"]))
    return out


def media_by_code(code):
    for m in media_table():
        if m["code"] == code:
            return m
    raise ValueError(f"Unknown media code: {code!r}")


# ----------------------------------------------------------------------
# Fonts: family -> {style: [candidate paths]}. Linux (container) paths first,
# Windows fallbacks second so the renderer also runs on the dev PC. The browser
# is served whichever path resolves, via /fonts, so on-screen == printed.
# ----------------------------------------------------------------------

_DEJAVU = "/usr/share/fonts/truetype/dejavu"
_LIB = "/usr/share/fonts/truetype/liberation"
_FREE = "/usr/share/fonts/truetype/freefont"
_WIN = "C:/Windows/Fonts"

FONTS = {
    "Sans": {
        "regular": [f"{_DEJAVU}/DejaVuSans.ttf", f"{_WIN}/arial.ttf"],
        "bold": [f"{_DEJAVU}/DejaVuSans-Bold.ttf", f"{_WIN}/arialbd.ttf"],
        "italic": [f"{_DEJAVU}/DejaVuSans-Oblique.ttf", f"{_WIN}/ariali.ttf"],
        "bolditalic": [f"{_DEJAVU}/DejaVuSans-BoldOblique.ttf", f"{_WIN}/arialbi.ttf"],
    },
    "Serif": {
        "regular": [f"{_DEJAVU}/DejaVuSerif.ttf", f"{_WIN}/times.ttf"],
        "bold": [f"{_DEJAVU}/DejaVuSerif-Bold.ttf", f"{_WIN}/timesbd.ttf"],
        "italic": [f"{_DEJAVU}/DejaVuSerif-Italic.ttf", f"{_WIN}/timesi.ttf"],
        "bolditalic": [f"{_DEJAVU}/DejaVuSerif-BoldItalic.ttf", f"{_WIN}/timesbi.ttf"],
    },
    "Mono": {
        "regular": [f"{_DEJAVU}/DejaVuSansMono.ttf", f"{_WIN}/consola.ttf", f"{_WIN}/cour.ttf"],
        "bold": [f"{_DEJAVU}/DejaVuSansMono-Bold.ttf", f"{_WIN}/consolab.ttf", f"{_WIN}/courbd.ttf"],
        "italic": [f"{_DEJAVU}/DejaVuSansMono-Oblique.ttf", f"{_WIN}/consolai.ttf", f"{_WIN}/couri.ttf"],
        "bolditalic": [f"{_DEJAVU}/DejaVuSansMono-BoldOblique.ttf", f"{_WIN}/consolaz.ttf", f"{_WIN}/courbi.ttf"],
    },
    "Arial-like": {
        "regular": [f"{_LIB}/LiberationSans-Regular.ttf", f"{_WIN}/arial.ttf"],
        "bold": [f"{_LIB}/LiberationSans-Bold.ttf", f"{_WIN}/arialbd.ttf"],
        "italic": [f"{_LIB}/LiberationSans-Italic.ttf", f"{_WIN}/ariali.ttf"],
        "bolditalic": [f"{_LIB}/LiberationSans-BoldItalic.ttf", f"{_WIN}/arialbi.ttf"],
    },
    "Times-like": {
        "regular": [f"{_LIB}/LiberationSerif-Regular.ttf", f"{_WIN}/times.ttf"],
        "bold": [f"{_LIB}/LiberationSerif-Bold.ttf", f"{_WIN}/timesbd.ttf"],
        "italic": [f"{_LIB}/LiberationSerif-Italic.ttf", f"{_WIN}/timesi.ttf"],
        "bolditalic": [f"{_LIB}/LiberationSerif-BoldItalic.ttf", f"{_WIN}/timesbi.ttf"],
    },
    "Symbol": {   # FreeSerif has very broad Unicode/special-character coverage
        "regular": [f"{_FREE}/FreeSerif.ttf", f"{_DEJAVU}/DejaVuSans.ttf"],
        "bold": [f"{_FREE}/FreeSerifBold.ttf", f"{_DEJAVU}/DejaVuSans-Bold.ttf"],
        "italic": [f"{_FREE}/FreeSerifItalic.ttf", f"{_DEJAVU}/DejaVuSans-Oblique.ttf"],
        "bolditalic": [f"{_FREE}/FreeSerifBoldItalic.ttf", f"{_DEJAVU}/DejaVuSans-BoldOblique.ttf"],
    },
}

DEFAULT_FAMILY = "Sans"


def _style_key(bold, italic):
    return ("bold" if bold else "") + ("italic" if italic else "") or "regular"


def font_file(family, bold=False, italic=False):
    """Resolve a family+style to an existing TTF path (with sane fallbacks)."""
    import os
    fam = FONTS.get(family, FONTS[DEFAULT_FAMILY])
    key = _style_key(bold, italic)
    # Fall back through style (bolditalic->bold->regular) then family default.
    for k in (key, "bold" if "bold" in key else "regular", "regular"):
        for path in fam.get(k, []):
            if os.path.exists(path):
                return path
    for path in FONTS[DEFAULT_FAMILY]["regular"]:
        if os.path.exists(path):
            return path
    return None


_font_cache = {}


def load_font(family, size, bold=False, italic=False):
    size = max(6, int(round(size)))
    key = (family, size, bool(bold), bool(italic))
    if key not in _font_cache:
        path = font_file(family, bold, italic)
        try:
            _font_cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except OSError:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


_measure = ImageDraw.Draw(Image.new("L", (4, 4)))


# ----------------------------------------------------------------------
# Element rendering -- each returns an RGBA tile + its (w, h)
# ----------------------------------------------------------------------

def _color(el):
    return WHITE if str(el.get("color", "black")).lower() == "white" else BLACK


def _line_metrics(font):
    asc, desc = font.getmetrics()
    return asc, desc, asc + desc


def _measure_text(lines, font):
    asc, desc, lh = _line_metrics(font)
    widths = [_measure.textlength(ln, font=font) for ln in lines]
    w = int(math.ceil(max(widths) if widths else 0)) + 2
    h = lh * len(lines)
    return w, h, widths, lh, asc


def render_text(el):
    text = str(el.get("text", ""))
    lines = text.split("\n") or [""]
    font = load_font(el.get("family", DEFAULT_FAMILY), el.get("size", 40),
                     el.get("bold", False), el.get("italic", False))
    align = el.get("align", "left")
    w, h, widths, lh, asc = _measure_text(lines, font)
    tile = Image.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    fill = _color(el)
    for i, ln in enumerate(lines):
        lw = widths[i]
        x = 0 if align == "left" else (w - lw) if align == "right" else (w - lw) / 2
        y = i * lh
        d.text((x, y), ln, font=font, fill=fill)
        if el.get("underline"):
            uy = y + asc + max(1, int(font.size * 0.08))
            d.line([(x, uy), (x + lw, uy)], fill=fill, width=max(1, int(font.size * 0.06)))
    return tile, w, h


def render_rect(el):
    w = max(1, int(el.get("w", 100)))
    h = max(1, int(el.get("h", 60)))
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    col = _color(el) + (255,)
    if el.get("fill"):
        d.rectangle([0, 0, w - 1, h - 1], fill=col)
    else:
        lw = max(1, int(el.get("line", 3)))
        d.rectangle([lw // 2, lw // 2, w - 1 - lw // 2, h - 1 - lw // 2], outline=col, width=lw)
    return tile, w, h


def render_line(el):
    x1, y1 = el.get("x", 0), el.get("y", 0)
    x2, y2 = el.get("x2", x1 + 100), el.get("y2", y1)
    lw = max(1, int(el.get("line", 3)))
    w = int(abs(x2 - x1)) + lw * 2
    h = int(abs(y2 - y1)) + lw * 2
    tile = Image.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    ox, oy = lw, lw
    d.line([ox, oy, ox + (x2 - x1), oy + (y2 - y1)], fill=_color(el) + (255,), width=lw)
    return tile, w, h, min(x1, x2) - lw, min(y1, y2) - lw   # absolute origin


def render_qr(el):
    import qrcode
    size = max(40, int(el.get("size", 160)))
    ecc = {"L": qrcode.constants.ERROR_CORRECT_L, "M": qrcode.constants.ERROR_CORRECT_M,
           "Q": qrcode.constants.ERROR_CORRECT_Q, "H": qrcode.constants.ERROR_CORRECT_H
           }.get(str(el.get("ecc", "M")).upper(), qrcode.constants.ERROR_CORRECT_M)
    qr = qrcode.QRCode(error_correction=ecc, border=2, box_size=4)
    qr.add_data(str(el.get("data", "")))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    img = img.resize((size, size), Image.NEAREST)
    if str(el.get("color", "black")).lower() == "white":
        img = _invert_rgba(img)
    return img, size, size


def render_barcode(el):
    import barcode
    from barcode.writer import ImageWriter
    sym = str(el.get("symbology", "code128")).lower()
    data = str(el.get("data", "")) or "0"
    w = max(80, int(el.get("w", 360)))
    h = max(40, int(el.get("h", 120)))
    try:
        cls = barcode.get_barcode_class(sym)
        opts = {"write_text": bool(el.get("show_text", True)),
                "module_height": max(5.0, h / PX_PER_MM * 0.8),
                "font_size": 8, "text_distance": 2, "quiet_zone": 2}
        img = cls(data, writer=ImageWriter()).render(opts).convert("RGBA")
    except Exception as e:
        # Bad data for the chosen symbology (e.g. EAN needs digits): show why.
        img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
        ImageDraw.Draw(img).text((4, 4), f"barcode err:\n{e}", fill=BLACK,
                                 font=load_font(DEFAULT_FAMILY, 16))
        return img, w, h
    img = img.resize((w, h), Image.LANCZOS)
    if str(el.get("color", "black")).lower() == "white":
        img = _invert_rgba(img)
    return img, w, h


def render_image(el):
    w = max(1, int(el.get("w", 200)))
    h = max(1, int(el.get("h", 200)))
    raw = el.get("data", "")
    if "," in raw:
        raw = raw.split(",", 1)[1]
    src = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGBA")
    src = src.resize((w, h), Image.LANCZOS)
    # Flatten transparency onto white, then to 1-bit (dither optional).
    flat = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    flat.alpha_composite(src)
    gray = flat.convert("L")
    if el.get("dither", True):
        bw = gray.convert("1")                       # Floyd-Steinberg
    else:
        thr = int(el.get("threshold", 128))
        bw = gray.point(lambda p: 255 if p >= thr else 0).convert("1")
    return bw.convert("RGBA"), w, h


def _invert_rgba(img):
    from PIL import ImageOps
    r = img.convert("RGB")
    inv = ImageOps.invert(r).convert("RGBA")
    inv.putalpha(img.split()[-1])
    return inv


_RENDERERS = {
    "text": render_text, "rect": render_rect, "qr": render_qr,
    "barcode": render_barcode, "image": render_image,
}


# ----------------------------------------------------------------------
# Compose the whole label
# ----------------------------------------------------------------------

def _paste(base, tile, x, y, rotation):
    if rotation:
        rot = tile.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        # keep the element's center fixed under rotation
        cx, cy = x + tile.width / 2, y + tile.height / 2
        base.alpha_composite(rot, (int(cx - rot.width / 2), int(cy - rot.height / 2)))
    else:
        base.alpha_composite(tile, (int(x), int(y)))


def render_spec(spec):
    """Render a design spec to an RGB PIL image sized for its media."""
    media = media_by_code(spec.get("media", "62"))
    W = media["width_px"]
    margin = int(spec.get("margin", 16))
    elements = spec.get("elements", [])

    # Decide canvas height.
    if media["length_px"]:
        H = media["length_px"]                       # die-cut / round: fixed
    elif spec.get("length_mm"):
        H = int(round(float(spec["length_mm"]) * PX_PER_MM))
    else:
        H = _auto_length(elements, margin)           # continuous: fit content

    base = Image.new("RGBA", (W, max(H, 1)), (255, 255, 255, 255))
    for el in elements:
        try:
            res = _render_one(el)
        except Exception as e:
            print(f"element {el.get('type')} failed: {e}", flush=True)
            continue
        if res is None:
            continue
        tile, x, y = res
        _paste(base, tile, x, y, float(el.get("rotation", 0)))

    return base.convert("RGB")


def _render_one(el):
    t = el.get("type")
    if t == "line":
        tile, w, h, ox, oy = render_line(el)
        return tile, ox, oy
    fn = _RENDERERS.get(t)
    if not fn:
        return None
    tile, w, h = fn(el)
    return tile, el.get("x", 0), el.get("y", 0)


def _element_extent(el):
    """Bottom y of an element (for auto-length). Approximate is fine."""
    y = el.get("y", 0)
    t = el.get("type")
    if t == "text":
        font = load_font(el.get("family", DEFAULT_FAMILY), el.get("size", 40),
                         el.get("bold"), el.get("italic"))
        _, h, _, _, _ = _measure_text(str(el.get("text", "")).split("\n"), font)
        return y + h
    if t == "line":
        return max(el.get("y", 0), el.get("y2", el.get("y", 0))) + int(el.get("line", 3))
    if t == "qr":
        return y + int(el.get("size", 160))
    return y + int(el.get("h", 80))


def _auto_length(elements, margin):
    if not elements:
        return int(40 * PX_PER_MM)                   # blank ~40 mm default
    bottom = max(_element_extent(el) for el in elements)
    return int(bottom + margin)


# ----------------------------------------------------------------------
# Local self-test: render a sample design to a PNG (no printer needed)
# ----------------------------------------------------------------------

if __name__ == "__main__":
    sample = {
        "media": "62",
        "elements": [
            {"type": "text", "x": 24, "y": 20, "text": "Café déjà vu — №1",
             "family": "Serif", "size": 52, "bold": True, "align": "left"},
            {"type": "text", "x": 24, "y": 90, "text": "Ω π ✓ ★ ½ ° µ → ™",
             "family": "Symbol", "size": 40},
            {"type": "line", "x": 24, "y": 150, "x2": 672, "y2": 150, "line": 3},
            {"type": "text", "x": 24, "y": 168, "text": "Rotated!", "family": "Sans",
             "size": 44, "italic": True, "rotation": -8},
            {"type": "qr", "x": 470, "y": 175, "size": 190, "data": "https://example.com"},
            {"type": "barcode", "x": 24, "y": 250, "w": 360, "h": 120,
             "data": "HELLO-123", "symbology": "code128"},
            {"type": "rect", "x": 24, "y": 390, "w": 648, "h": 70, "line": 4},
            {"type": "text", "x": 40, "y": 405, "text": "Boxed note",
             "family": "Mono", "size": 38},
        ],
    }
    img = render_spec(sample)
    out = "custom_sample.png"
    img.save(out)
    print(f"wrote {out} ({img.width}x{img.height})")
