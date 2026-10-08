"""Type stack on playlist covers: the album covers darkened, the playlist's genres written top-down at the left in
their colours (larger for more tracks), and a footer with the playlist's mood and how much singing it has. Each
line sits on its own dark plate so it reads on any artwork.
TIDAL and SoundCloud playlists get their own characters (draw_source); every cover gets a thin frame and a short
accent bar in its source's brand colour. Pillow only, no Music Assistant imports, so it can be tested outside MA.
The faces (Archivo Black, DM Mono; SIL Open Font License) ship in fonts/."""
from __future__ import annotations

import colorsys
import io
import math
import os
import random
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = 1500  # style sizes are pixels on a 1500 px cover and scale with the image
# bands: genres shown; font / font_min: text size of the top genre and the smallest; darken: how far the covers are
# blended toward DARK; footer: footer text size; grid: covers per side (MA's album grid has SOURCE_GRID per side)
DEFAULT_STYLE = {"bands": 4, "font": 250, "font_min": 110, "darken": 0.62, "footer": 120, "footer2": 56, "grid": 3}
SOURCE_GRID = 6
MARGIN = 60           # space around the text
FOOTER_GAP = 28       # between the genres and the accent bar, and between the bar and the footer
BAR_W, BAR_H = 300, 8  # the short accent bar in the source's brand colour
PLATE_PAD = (22, 12)  # a plate reaches this far around the letters of its line (x, y)
PLATE_GAP = 20        # between two plates
PLATE_SHADE, PLATE_COLOUR = 0.62, (6, 6, 8)    # how far a plate darkens the artwork under it
TIDAL_DARK, TIDAL_DARKEN = (8, 8, 8), 0.55    # grey artwork, darkened
TIDAL_BAND, TIDAL_BLACK = 2 / 3, (0, 0, 0)     # solid black band from here (share of the height) down
TIDAL_TILES = (3, 2)                           # album covers across and down, above the band
SC_BLUR, SC_DARKEN = 18, 0.5
SC_ORANGE, SC_GLOW_FROM, SC_GLOW = (0xFF, 0x55, 0x00), 0.45, 0.6   # glow rises from the bottom to 45 % height
WAVE_H, WAVE_BAR, WAVE_GAP, WAVE_BASE, WAVE_SPLIT = 220, 8, 6, 0.72, 3   # waveform height, bars, baseline share
WAVE_RESERVE = WAVE_H + 50          # kept free for the waveform below the text
LETTER_SPACING = {"type": 0.0, "mono": 0.02}  # footer letter spacing per face, as a share of its size
VOCAL_TEXT = (190, 190, 190)  # the footer's second line (vocal label) is lighter than the mood words
FONT_FLOOR = 24
DARK = (12, 12, 14)
FOOTER_TEXT = (255, 255, 255)
LIGHT_GREY = (200, 200, 206)  # a genre the bridge could not place on the colour wheel
LIGHTNESS, SATURATION = 0.66, 0.85
INSTRUMENTAL, VOCALS = 0.75, 0.35
FRAME = 10            # thin frame on every cover, in the colour of the playlist's source
FRAME_COLOURS = {"spotify": (0x1D, 0xB9, 0x54), "tidal": (0xFF, 0xFF, 0xFF), "soundcloud": (0xFF, 0x55, 0x00)}
FONT_DIR = Path(__file__).parent / "fonts"
FACES = {"type": FONT_DIR / "ArchivoBlack-Regular.ttf", "mono": FONT_DIR / "DMMono-Medium.ttf"}
FALLBACK_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc")


def genre_colour(genre: str, hues: dict | None) -> tuple[int, int, int]:
    """The colour of the genre's hue on the bridge's colour wheel (neighbouring hues sound alike)."""
    hue = (hues or {}).get(genre)
    if hue is None:
        return LIGHT_GREY
    r, g, b = colorsys.hls_to_rgb((float(hue) % 360) / 360, LIGHTNESS, SATURATION)
    return round(r * 255), round(g * 255), round(b * 255)


def vocal_label(instrumental: float) -> str:
    if instrumental >= INSTRUMENTAL:
        return "INSTRUMENTAL"
    if instrumental <= VOCALS:
        return "VOCALS"
    return "SOME VOCALS"


def footer_text(mood) -> str:
    """'SPACE · ENERGETIC · INSTRUMENTAL' from the playlist's entry in the layout's "moods"; '' without one."""
    if not isinstance(mood, dict):
        return ""
    words = mood.get("words")
    parts = [str(w).strip().upper() for w in words if str(w).strip()] if isinstance(words, list) else []
    try:
        parts.append(vocal_label(float(mood["instrumental"])))
    except (KeyError, TypeError, ValueError):
        pass
    return " · ".join(parts)


@lru_cache(maxsize=256)
def _font(size: int, face: str = "type"):
    for path in (FACES[face], *FALLBACK_FONTS):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def spaced_width(text: str, font, spacing: int) -> float:
    return sum(font.getlength(c) for c in text) + spacing * max(0, len(text) - 1)


def _regrid(img: Image.Image, grid: int) -> Image.Image:
    """Fewer, larger covers: the top-left grid x grid tiles of MA's collage, enlarged to the full cover."""
    w, h = img.size
    return img.crop((0, 0, w * grid // SOURCE_GRID, h * grid // SOURCE_GRID)).resize((w, h), Image.LANCZOS)


def _shares(genres: list) -> list[tuple[str, int]]:
    """[[name, count], ...] from the bridge; plain names (older layout files) count 1 each."""
    return [(g, 1) if isinstance(g, str) else (str(g[0]), max(1, int(g[1]))) for g in genres]


def _ink(text: str, size: int, face: str, spacing: int = 0) -> tuple[int, int, int, int]:
    """The box the letters cover, relative to where they are drawn (Pillow's default anchor)."""
    left, top, right, bottom = _font(size, face).getbbox(text)
    if spacing:
        right = left + round(spaced_width(text, _font(size, face), spacing))
    return left, top, right, bottom


def layout(size: tuple[int, int], shares: list[tuple[str, int]], st: dict, footer: str, *,
           align: str = "left", bar: str = "footer", reserve: int = 0, band: float | None = None,
           footer_plates: bool = True) -> dict:
    """Where everything goes. lines: (text, size, x, y) with y the top of the line (Pillow's default anchor);
    footer: list of (text, size, x, y, letter spacing, face), mood words first, or None; bar: its box or None;
    plates: one dark box per genre line, then one per footer line (unless footer_plates is False).
    With "footer2" > 0 the vocal label gets its own smaller mono line under the mood words.
    align: "left" or "center"; bar: "footer" (only above a footer), "always" or "none";
    reserve: pixels (on a BASE cover) kept free at the bottom, above the margin, e.g. for a waveform;
    band: share of the height where a band starts: the genres stay above it, the footer is centred in it."""
    w, h = size
    k = w / BASE
    m = round(MARGIN * k)
    px, py = round(PLATE_PAD[0] * k), round(PLATE_PAD[1] * k)
    gap_p = max(1, round(PLATE_GAP * k))
    gap = max(2, round(FOOTER_GAP * k))
    floor = max(6, round(FONT_FLOOR * k))
    width = w - 2 * m
    bottom = h - m - round(reserve * k)
    band_top = round(h * band) if band else None
    centre = align == "center"
    foot, box, foot_plates = None, None, []

    def place_x(ink_w: int, left: int) -> int:
        return round((w - ink_w) / 2 - left) if centre else m - left

    bar_room = gap + max(2, round(BAR_H * k)) if bar != "none" else 0

    def stack_footer(scale: float) -> tuple[list, list]:
        second = float(st.get("footer2") or 0)
        parts = footer.rsplit(" · ", 1) if second > 0 else [footer]
        wanted = [float(st["footer"]), second][:len(parts)]
        faces = ["type", "mono"][:len(parts)] if len(parts) == 2 else ["type"]
        lines_, plates_, y_low = [], [], bottom
        for text, want, face in reversed(list(zip(parts, wanted, faces))):
            fsize = max(floor, round(want * k * scale))
            spacing = round(fsize * LETTER_SPACING[face])
            while fsize > floor and _ink(text, fsize, face, spacing)[2] - _ink(text, fsize, face, spacing)[0] > width:
                fsize -= 1
                spacing = round(fsize * LETTER_SPACING[face])
            left, top, right, low = _ink(text, fsize, face, spacing)
            y = y_low - py - low
            x = place_x(right - left, left)
            lines_.insert(0, (text, fsize, x, y, spacing, face))
            plates_.insert(0, (x + left - px, y + top - py, x + right + px, y + low + py))
            y_low = y + top - py - gap_p
        return lines_, plates_

    if footer:
        foot, foot_plates = stack_footer(1.0)
        if band_top is not None:   # the footer (and its bar) fits in the band, centred in its visible part
            scale = 1.0
            while scale > 0.2 and foot_plates[0][1] - bar_room <= band_top + gap:
                scale *= 0.92
                foot, foot_plates = stack_footer(scale)
            block_top, block_low = foot_plates[0][1] - bar_room, foot_plates[-1][3] - py
            shift = round((band_top + h) / 2 - (block_top + block_low) / 2)
            shift = max(band_top + gap - block_top, min(shift, 0))
            foot = [(t, s_, x, y + shift, sp, f) for t, s_, x, y, sp, f in foot]
            foot_plates = [(a, b + shift, c, d + shift) for a, b, c, d in foot_plates]
        top_edge = foot_plates[0][1]
    else:
        top_edge = bottom + gap
    if bar == "always" or (bar == "footer" and foot):
        thick, half = max(2, round(BAR_H * k)), min(width, round(BAR_W * k))
        x0 = (w - half) // 2 if centre else m
        box = (x0, top_edge - gap - thick, x0 + half - 1, top_edge - gap - 1)
        limit = box[1] - gap
    else:
        limit = top_edge - gap if foot else bottom
    if band_top is not None:
        limit = min(limit, band_top - gap)

    shares = sorted(shares, key=lambda s_: -s_[1])
    texts = [g.upper() for g, _ in shares]
    big = float(st["font"])
    small = min(float(st["font_min"]), big)
    top_n = shares[0][1] if shares else 1
    sizes = []
    for text, (_, n) in zip(texts, shares):
        s_ = max(floor, round((small + (big - small) * n / top_n) * k))
        while s_ > floor and _ink(text, s_, "type")[2] - _ink(text, s_, "type")[0] > width:
            s_ -= 1
        sizes.append(min(s_, sizes[-1]) if sizes else s_)  # never larger than a genre with more tracks

    def height(texts_sizes) -> int:
        hs = [_ink(t, z, "type")[3] - _ink(t, z, "type")[1] + 2 * py for t, z in texts_sizes]
        return sum(hs) + gap_p * max(0, len(hs) - 1)

    room = limit - (m - py)
    while sizes and height(zip(texts, sizes)) > room:  # too tall: all sizes shrink together
        if all(s_ <= floor for s_ in sizes):
            sizes.pop()
            continue
        f = room / height(zip(texts, sizes))
        sizes = [max(floor, min(s_ - 1, int(s_ * f))) for s_ in sizes]
    lines, plates, y_top = [], [], m - py
    for text, s_ in zip(texts, sizes):
        left, top, right, low = _ink(text, s_, "type")
        x, y = place_x(right - left, left), y_top + py - top
        lines.append((text, s_, x, y))
        plates.append((x + left - px, y + top - py, x + right + px, y + low + py))
        y_top = plates[-1][3] + gap_p
    return {"lines": lines, "footer": foot, "bar": box, "plates": plates + (foot_plates if footer_plates else [])}


def draw_genres(data: bytes, genres: list, style: dict | None = None, regrid: bool = False,
                hues: dict | None = None, mood: dict | None = None) -> bytes:
    """JPEG bytes in, JPEG bytes out. `regrid` is only valid for covers made with MA's album-grid template."""
    st = {**DEFAULT_STYLE, **(style or {})}
    shares = sorted(_shares(genres), key=lambda s: -s[1])[:max(0, int(st["bands"]))]
    footer = footer_text(mood)
    regrid = regrid and 0 < int(st["grid"]) < SOURCE_GRID
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if regrid:
        img = _regrid(img, int(st["grid"]))
    if shares or footer:
        darken = min(1.0, max(0.0, float(st["darken"])))
        if darken:
            img = Image.blend(img, Image.new("RGB", img.size, DARK), darken)
        _type_stack(img, shares, footer, st, hues)
    _frame(img, FRAME_COLOURS["spotify"])
    return _jpeg(img)


def _jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def _frame(img: Image.Image, colour: tuple[int, int, int]) -> None:
    w, h = img.size
    f = max(1, round(FRAME * w / BASE))
    ImageDraw.Draw(img).rectangle((0, 0, w - 1, h - 1), outline=colour, width=f)


def _type_stack(img: Image.Image, shares: list[tuple[str, int]], footer: str, st: dict, hues: dict | None) -> None:
    _draw_text(img, shares, layout(img.size, shares, st, footer), hues, FRAME_COLOURS["spotify"])


def _plate(img: Image.Image, box) -> None:
    w, h = img.size
    f = max(1, round(FRAME * w / BASE))
    x0, y0, x1, y1 = max(f, round(box[0])), max(f, round(box[1])), min(w - f, round(box[2])), min(h - f, round(box[3]))
    if x1 > x0 and y1 > y0:
        region = img.crop((x0, y0, x1, y1))
        img.paste(Image.blend(region, Image.new("RGB", region.size, PLATE_COLOUR), PLATE_SHADE), (x0, y0))


def _draw_text(img: Image.Image, shares: list[tuple[str, int]], lay: dict, hues: dict | None, bar_colour) -> None:
    for box in lay["plates"]:   # all plates first, so none darkens letters already drawn
        _plate(img, box)
    draw = ImageDraw.Draw(img)
    for (genre, _), (text, size, x, y) in zip(shares, lay["lines"]):  # shares come sorted, most tracks first
        draw.text((x, y), text, font=_font(size, "type"), fill=genre_colour(genre, hues))
    if lay["bar"] and bar_colour:
        draw.rectangle(lay["bar"], fill=bar_colour)
    if lay["footer"]:
        for i, (text, size, x, y, spacing, face) in enumerate(lay["footer"]):
            font, fill = _font(size, face), FOOTER_TEXT if i == 0 else VOCAL_TEXT
            if not spacing:   # as measured: the whole string, kerned
                draw.text((x, y), text, font=font, fill=fill)
                continue
            for c in text:
                draw.text((x, y), c, font=font, fill=fill)
                x += font.getlength(c) + spacing


def draw_source(data: bytes | None, source: str, genres: list, style: dict | None = None, hues: dict | None = None,
                mood: dict | None = None, seed: str = "", tiles: list[bytes] | None = None) -> bytes:
    """A TIDAL or SoundCloud playlist's own artwork (any image bytes) in, a BASE-sized JPEG cover out.
    `seed` (the playlist uri) keeps the SoundCloud waveform the same on every redraw. `tiles`: album covers of a
    TIDAL playlist's tracks, shown in place of its own artwork (`data` may then be None)."""
    st = {**DEFAULT_STYLE, **(style or {})}
    shares = sorted(_shares(genres), key=lambda s: -s[1])[:max(0, int(st["bands"]))]
    footer = footer_text(mood)
    img = _square(Image.open(io.BytesIO(data)).convert("RGB"), BASE) if data else Image.new("RGB", (BASE, BASE), DARK)
    if source == "tidal":
        img = _tidal(img, shares, footer, st, hues, tiles)
    elif source == "soundcloud":
        img = _soundcloud(img, shares, st, hues, seed)
    else:
        raise ValueError(f"unknown source {source!r}")
    _frame(img, FRAME_COLOURS[source])
    return _jpeg(img)


def _square(img: Image.Image, side: int) -> Image.Image:
    w, h = img.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    return img.crop((left, top, left + s, top + s)).resize((side, side), Image.LANCZOS)


def _grid(tiles: list[bytes], size: tuple[int, int]) -> Image.Image:
    """TIDAL_TILES covers, filled left to right and top down, repeating them when there are fewer."""
    w, h = size
    across, down = TIDAL_TILES
    side = w // across
    out = Image.new("RGB", size, TIDAL_DARK)
    pics = [_square(Image.open(io.BytesIO(t)).convert("RGB"), side) for t in tiles]
    for k in range(across * down):
        out.paste(pics[k % len(pics)], ((k % across) * side, (k // across) * side))
    return out


def _tidal(img: Image.Image, shares, footer: str, st: dict, hues: dict | None, tiles: list[bytes] | None) -> Image.Image:
    if tiles:
        img = _grid(tiles, img.size)
    img = Image.blend(img.convert("L").convert("RGB"), Image.new("RGB", img.size, TIDAL_DARK), TIDAL_DARKEN)
    w, h = img.size
    ImageDraw.Draw(img).rectangle((0, round(h * TIDAL_BAND), w, h), fill=TIDAL_BLACK)   # also hides TIDAL's title
    lay = layout(img.size, shares, st, footer, align="center", band=TIDAL_BAND, footer_plates=False)
    _draw_text(img, shares, lay, hues, FRAME_COLOURS["tidal"])
    return img


def waveform(size: tuple[int, int], shares: list[tuple[str, int]], seed: str) -> list[tuple]:
    """SoundCloud-like bars across the bottom: (x0, x1, top, base, bottom, genre) each, with the bar rising from
    `base` to `top` and its reflection reaching down to `bottom`. Left to right the bars are coloured by genre in
    stretches proportional to the genres' tracks, most tracks first. Heights depend only on `seed`."""
    w, h = size
    k = w / BASE
    m = round(MARGIN * k)
    bar, gap = max(1, round(WAVE_BAR * k)), max(1, round(WAVE_GAP * k))
    n = max(1, (w - 2 * m + gap) // (bar + gap))
    x = m + (w - 2 * m - (n * (bar + gap) - gap)) // 2
    y1 = h - m
    y0 = y1 - round(WAVE_H * k)
    base = y0 + round((y1 - y0) * WAVE_BASE)
    rng = random.Random(seed)
    noise = [rng.random() for _ in range(n + 2)]
    jitter = [rng.random() for _ in range(n)]
    phase, waves = rng.uniform(0, 6.3), rng.uniform(2.0, 4.0)
    shares = sorted(shares, key=lambda s: -s[1])
    total = sum(c for _, c in shares)
    ends, run = [], 0
    for _, c in shares:
        run += c
        ends.append(run / total)
    out = []
    for i in range(n):
        smooth = sum(noise[i:i + 3]) / 3
        envelope = 0.5 + 0.5 * math.sin(phase + waves * math.pi * i / n)
        level = 0.12 + 0.88 * (0.4 * envelope + 0.4 * smooth + 0.2 * jitter[i]) ** 1.5
        up = max(1, round((base - y0) * level))
        down = max(1, round((y1 - base) * level))
        where = (i + 0.5) / n
        genre = next((g for (g, _), end in zip(shares, ends) if where < end), shares[-1][0] if shares else None)
        out.append((x, x + bar, base - up, base, base + down, genre))
        x += bar + gap
    return out


def _fade(img: Image.Image, colour, start: float, strength: float) -> Image.Image:
    """Blend toward `colour` from `start` (share of the height) down to the bottom, smoothly up to `strength`."""
    w, h = img.size
    y0 = round(h * start)
    ramp = [0] * y0 + [round(255 * strength * (t * t * (3 - 2 * t)))
                       for t in (min(1.0, (y - y0) / max(1, h - 1 - y0)) for y in range(y0, h))]
    mask = Image.new("L", (1, h))
    mask.putdata(ramp)
    return Image.composite(Image.new("RGB", (w, h), colour), img, mask.resize((w, h)))


def _soundcloud(img: Image.Image, shares, st: dict, hues: dict | None, seed: str) -> Image.Image:
    """No mood words: the waveform is SoundCloud's footer."""
    k = img.width / BASE
    img = img.filter(ImageFilter.GaussianBlur(SC_BLUR * k))
    img = Image.blend(img, Image.new("RGB", img.size, DARK), SC_DARKEN)
    img = _fade(img, SC_ORANGE, SC_GLOW_FROM, SC_GLOW)
    lay = layout(img.size, shares, st, "", bar="always", reserve=WAVE_RESERVE)
    _draw_text(img, shares, lay, hues, FRAME_COLOURS["soundcloud"])
    draw = ImageDraw.Draw(img)
    for x0, x1, top, base, bottom, genre in waveform(img.size, shares, seed):
        colour = genre_colour(genre, hues) if genre else LIGHT_GREY
        draw.rectangle((x0, top, x1 - 1, base - 1), fill=colour)
        echo = tuple(round(c * 0.45 + d * 0.55) for c, d in zip(colour, DARK))   # dimmer reflection
        draw.rectangle((x0, base + max(1, round(WAVE_SPLIT * k)), x1 - 1, bottom - 1), fill=echo)
    return img


def restyle_file(path: str, genres: list, style: dict | None = None, regrid: bool = False,
                 hues: dict | None = None, mood: dict | None = None) -> None:
    """Redraw the cover file in place, atomically, so MA never serves a half-written image."""
    target = Path(path)
    styled = draw_genres(target.read_bytes(), genres, style, regrid, hues, mood)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(styled)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
