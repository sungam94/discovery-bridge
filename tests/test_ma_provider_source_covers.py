"""The TIDAL and SoundCloud characters drawn on those services' own playlist artwork."""
import importlib.util
import io
from pathlib import Path

from PIL import Image

_spec = importlib.util.spec_from_file_location(
    "spotify_bridge_frames_src", Path(__file__).parents[1] / "ma_provider" / "spotify_bridge" / "frames.py")
frames = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frames)

HUES = {"psytrance": 310, "jazz": 162, "dub": 197, "black metal": 0, "hip hop": 178, "rap rock": 62,
        "dance-pop": 123, "dance-punk": 107, "doom metal": 28, "darkpsy": 280, "trance": 220}
SIX = [["black metal", 4], ["hip hop", 4], ["rap rock", 3], ["dance-pop", 2], ["dance-punk", 2], ["doom metal", 2]]
MOOD = {"words": ["space", "energetic"], "instrumental": 0.2}
W = frames.BASE


def art(w=640, h=640, colour=None) -> bytes:
    """Colourful synthetic artwork (or one flat colour) like a service's playlist image."""
    img = Image.new("RGB", (w, h), colour or (0, 0, 0))
    if colour is None:
        img.putdata([(x * 255 // w, y * 255 // h, 200) for y in range(h) for x in range(w)])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def drawn(source, data, genres, mood=None, style=None, seed="tidal--x://playlist/1", tiles=None):
    out = frames.draw_source(data, source, genres, style, HUES, mood, seed, tiles)
    return Image.open(io.BytesIO(out)).convert("RGB")


def near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def count(img, box, test):
    x0, y0, x1, y1 = (round(v) for v in box)
    return sum(1 for x in range(x0, x1) for y in range(y0, y1) if test(img.getpixel((x, y))))


def ink(line):
    text, size, x, y = line
    left, top, right, bottom = frames._font(size).getbbox(text)
    return x + left, y + top, x + right, y + bottom


def plates_clear(plates, gap):
    ordered = sorted(plates, key=lambda b: b[1])
    return all(b[1] - a[3] >= gap for a, b in zip(ordered, ordered[1:]))


def tidal_layout(shares, st, footer):
    return frames.layout((W, W), shares, st, footer, align="center", band=frames.TIDAL_BAND, footer_plates=False)


def frame_px(size=W):
    return max(1, round(frames.FRAME * size / W))


def assert_framed(img, colour, tol=30):
    w, h = img.size
    f = frame_px(w)
    for x, y in ((f // 2, h // 2), (w - 1 - f // 2, h // 2), (w // 2, f // 2), (w // 2, h - 1 - f // 2),
                 (w // 5, f // 2), (w // 5, h - 1 - f // 2), (f // 2, h // 5), (w - 1 - f // 2, h // 5)):
        assert near(img.getpixel((x, y)), colour, tol), (x, y, img.getpixel((x, y)))
    assert not near(img.getpixel((f + 6, h // 3)), colour, 30)   # thin: just inside is not the frame


def style(**kw):
    return {**frames.DEFAULT_STYLE, **kw}


# TIDAL

def test_tidal_frame_is_white_on_all_four_sides():
    assert frames.FRAME_COLOURS["tidal"] == (255, 255, 255)
    assert_framed(drawn("tidal", art(), SIX, MOOD), (255, 255, 255))


def test_tidal_cover_is_square_at_full_size_from_any_artwork():
    assert drawn("tidal", art(1080, 720), SIX).size == (W, W)


def test_tidal_base_is_greyscale_and_darkened():
    out = drawn("tidal", art(colour=(220, 40, 40)), [])
    r, g, b = out.getpixel((750, 600))
    assert max(r, g, b) - min(r, g, b) <= 4                  # no colour left
    grey = round(0.299 * 220 + 0.587 * 40 + 0.114 * 40)      # Pillow's "L" conversion
    assert abs(r - round(grey * 0.45 + 8 * 0.55)) <= 4


def test_tidal_bottom_third_is_a_solid_black_band():
    out = drawn("tidal", art(colour=(250, 250, 250)), [])
    band = round(W * frames.TIDAL_BAND)
    for x, y in ((750, band + 4), (100, 1300), (1400, W - 30)):
        assert max(out.getpixel((x, y))) <= 6, (x, y)
    assert min(out.getpixel((750, band - 6))) > 60          # the artwork reaches the band


def test_tidal_shows_six_album_covers_three_by_two_in_grey_above_the_band():
    colours = [(250, 20, 20), (20, 250, 20), (20, 20, 250), (250, 250, 20), (250, 20, 250), (20, 250, 250)]
    out = drawn("tidal", art(), [], tiles=[art(300, 300, c) for c in colours])
    side = W // 3
    greys = []
    for k, c in enumerate(colours):
        r, g, b = out.getpixel((side * (k % 3) + side // 2, side * (k // 3) + side // 2))
        assert max(r, g, b) - min(r, g, b) <= 4                        # grey
        grey = round(0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])
        assert abs(r - round(grey * (1 - frames.TIDAL_DARKEN) + 8 * frames.TIDAL_DARKEN)) <= 5
        greys.append(r)
    assert len(set(greys)) >= 5                                        # each tile is its own cover, in order


def test_tidal_with_fewer_covers_repeats_them_and_without_any_uses_its_own_artwork():
    two = drawn("tidal", art(), [], tiles=[art(300, 300, (250, 250, 250)), art(300, 300, (10, 10, 10))])
    side = W // 3
    light = [two.getpixel((side * (k % 3) + side // 2, side * (k // 3) + side // 2))[0] for k in range(6)]
    assert light[0] == light[2] == light[4] and light[1] == light[3] == light[5] and light[0] > light[1] + 40
    own = drawn("tidal", art(colour=(220, 40, 40)), [], tiles=[])
    assert own.getpixel((750, 600)) == drawn("tidal", art(colour=(220, 40, 40)), []).getpixel((750, 600))


def test_tidal_genres_are_centred_in_their_colours():
    lay = frames.layout((W, W), frames._shares(SIX[:3]), style(), "", align="center")
    for line in lay["lines"]:
        x0, _, x1, _ = ink(line)
        assert abs((x0 + x1) / 2 - W / 2) <= 2
    out = drawn("tidal", art(colour=(30, 30, 30)), SIX[:3])
    red = frames.genre_colour("black metal", HUES)
    box = ink(lay["lines"][0])
    left = count(out, (box[0], box[1], W / 2, box[3]), lambda p: near(p, red))
    right = count(out, (W / 2, box[1], box[2], box[3]), lambda p: near(p, red))
    assert left > 300 and right > 300 and 0.6 < left / right < 1.6


def test_tidal_footer_is_centred_in_the_band_under_a_short_white_bar_without_plates():
    lay = tidal_layout(frames._shares(SIX), style(), frames.footer_text(MOOD))
    band = round(W * frames.TIDAL_BAND)
    x0, y0, x1, y1 = lay["bar"]
    assert x1 - x0 + 1 == frames.BAR_W and y1 - y0 + 1 == frames.BAR_H and abs((x0 + x1) / 2 - W / 2) <= 1
    assert band < y0
    (top, s1, fx1, fy1, sp1, f1), (low, s2, fx2, fy2, sp2, f2) = lay["footer"]
    assert (top, low) == ("SPACE · ENERGETIC", "VOCALS") and s2 < s1 and (f1, f2) == ("type", "mono")
    for text, size, x, _, spacing, face in lay["footer"]:
        assert abs(x + frames.spaced_width(text, frames._font(size, face), spacing) / 2 - W / 2) <= 2
    assert len(lay["plates"]) == len(lay["lines"])                    # the band carries the footer
    assert max(p[3] for p in lay["plates"]) < band
    out = drawn("tidal", art(colour=(30, 30, 30)), SIX, MOOD)
    middle = round((y0 + y1) / 2)
    assert near(out.getpixel((750, middle)), (255, 255, 255), 20)
    assert max(out.getpixel((x0 - 40, middle))) <= 6                  # short: the band around it stays black
    white = lambda p: min(p) > 235                                     # noqa: E731
    grey = lambda p: 150 < min(p) < 215 and max(p) - min(p) < 12       # noqa: E731
    assert count(out, (20, fy1, W - 20, fy1 + s1), white) > 300
    assert count(out, (20, fy2, W - 20, fy2 + s2), grey) > 150 and count(out, (20, fy2, W - 20, fy2 + s2), white) < 50


def test_tidal_six_genres_stay_above_the_band():
    for st in (style(bands=6), style(bands=6, font=400, font_min=300)):
        lay = tidal_layout(frames._shares(SIX), st, frames.footer_text(MOOD))
        boxes = [ink(line) for line in lay["lines"]]
        assert len(boxes) == 6 and plates_clear(lay["plates"], frames.PLATE_GAP)
        assert max(p[3] for p in lay["plates"]) < round(W * frames.TIDAL_BAND)
        assert all(b[0] >= frames.MARGIN and b[2] <= W - frames.MARGIN for b in boxes)


def test_tidal_without_moods_has_no_footer_or_rule():
    out = drawn("tidal", art(colour=(30, 30, 30)), SIX)
    assert count(out, (60, 1300, W - 60, W - 60), lambda p: min(p) > 200) == 0


# SoundCloud

SC = "soundcloud--Ab1://playlist/soundcloud:system-playlists:your-mix:1"
TWO = [["darkpsy", 3], ["trance", 1]]


def stripes(size=600) -> bytes:
    img = Image.new("RGB", (size, size))
    img.putdata([(255, 255, 255) if (x // 3) % 2 else (0, 0, 0) for y in range(size) for x in range(size)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_soundcloud_frame_is_orange_on_all_four_sides():
    assert frames.FRAME_COLOURS["soundcloud"] == (0xFF, 0x55, 0x00)
    assert_framed(drawn("soundcloud", art(), TWO, seed=SC), (0xFF, 0x55, 0x00))


def test_soundcloud_artwork_is_blurred_and_darkened():
    out = drawn("soundcloud", stripes(), [], seed=SC)
    row = [out.getpixel((x, 500))[0] for x in range(600, 700)]
    assert max(row) - min(row) <= 6                     # fine stripes are gone
    assert max(row) < 255 * 0.6                         # and darker than their average


def test_soundcloud_orange_glow_rises_from_the_bottom():
    out = drawn("soundcloud", art(colour=(90, 90, 90)), [], seed=SC)
    top_y = frames.waveform((W, W), [], SC)[0][2]
    high, low = out.getpixel((750, 300)), out.getpixel((750, round(top_y) - 30))
    assert abs(high[0] - high[2]) <= 4                  # no glow high up
    assert low[0] - low[2] > 40 and low[0] > high[0]    # orange near the bottom


def test_waveform_stretches_follow_the_genre_shares_left_to_right():
    bars = frames.waveform((W, W), frames._shares(TWO), SC)
    genres = [g for *_, g in bars]
    n = len(genres)
    assert n > 60
    assert abs(genres.count("darkpsy") - n * 3 / 4) <= 1 and genres.count("trance") == n - genres.count("darkpsy")
    assert genres == sorted(genres, key=lambda g: g != "darkpsy")   # one stretch each, most tracks first
    three = frames.waveform((W, W), frames._shares([["dub", 1], ["jazz", 1], ["trance", 2]]), SC)
    assert [g for *_, g in three][0] == "trance" and [g for *_, g in three][-1] in ("dub", "jazz")


def test_waveform_heights_are_stable_per_playlist_and_vary_along_it():
    a = frames.waveform((W, W), frames._shares(TWO), SC)
    assert a == frames.waveform((W, W), frames._shares(TWO), SC)
    b = frames.waveform((W, W), frames._shares(TWO), SC + "2")
    assert [bar[2] for bar in a] != [bar[2] for bar in b]
    heights = [bar[3] - bar[2] for bar in a]
    assert max(heights) > 2 * min(heights) > 0


def test_waveform_stays_inside_the_margins_near_the_bottom():
    m = frames.MARGIN
    for x0, x1, top, base, bottom, _ in frames.waveform((W, W), frames._shares(TWO), SC):
        assert m <= x0 < x1 <= W - m and W * 0.7 < top < base < bottom <= W - m


def test_waveform_bars_are_drawn_in_their_genres_colours():
    out = drawn("soundcloud", art(colour=(40, 40, 40)), TWO, seed=SC)
    bars = frames.waveform((W, W), frames._shares(TWO), SC)
    for genre in ("darkpsy", "trance"):
        x0, x1, top, base, _, _ = max((b for b in bars if b[5] == genre), key=lambda b: b[3] - b[2])
        assert near(out.getpixel((round((x0 + x1) / 2), round((top + base) / 2))),
                    frames.genre_colour(genre, HUES), 30)


def test_soundcloud_genres_are_left_aligned_with_an_orange_bar_above_the_waveform():
    lay = frames.layout((W, W), frames._shares(TWO), style(), "", bar="always", reserve=frames.WAVE_RESERVE)
    assert all(x == frames.MARGIN for _, _, x, _ in lay["lines"]) and lay["footer"] is None
    x0, y0, x1, y1 = lay["bar"]
    wave_top = min(b[2] for b in frames.waveform((W, W), frames._shares(TWO), SC))
    assert x0 == frames.MARGIN and x1 - x0 + 1 == frames.BAR_W and y1 < wave_top
    assert max(p[3] for p in lay["plates"]) < y0
    out = drawn("soundcloud", art(colour=(40, 40, 40)), TWO, seed=SC)
    assert near(out.getpixel((x0 + 150, round((y0 + y1) / 2))), (0xFF, 0x55, 0x00), 30)


def test_soundcloud_six_genres_bar_and_waveform_never_overlap():
    for st in (style(bands=6), style(bands=6, font=400, font_min=300)):
        shares = frames._shares(SIX)
        lay = frames.layout((W, W), shares, st, "", bar="always", reserve=frames.WAVE_RESERVE)
        assert len(lay["lines"]) == 6 and plates_clear(lay["plates"], frames.PLATE_GAP)
        assert lay["plates"][-1][3] < lay["bar"][1]
        assert lay["bar"][3] < min(b[2] for b in frames.waveform((W, W), shares, SC))


def test_soundcloud_shows_no_mood_words_even_with_a_moods_entry():
    with_mood = drawn("soundcloud", art(colour=(40, 40, 40)), TWO, MOOD, seed=SC)
    without = drawn("soundcloud", art(colour=(40, 40, 40)), TWO, seed=SC)
    assert with_mood.tobytes() == without.tobytes()


def test_a_tidal_cover_needs_no_own_artwork_when_it_has_tiles():
    out = frames.draw_source(None, "tidal", SIX, None, HUES, MOOD, "x", [art(300, 300, (250, 250, 250))])
    assert Image.open(io.BytesIO(out)).size == (W, W)


def test_a_large_tidal_footer_shrinks_to_stay_inside_the_band():
    for st in (style(footer=400, footer2=400), style(footer=250, footer2=150), style()):
        lay = frames.layout((W, W), frames._shares(SIX), st, "CALM · VOCALS", align="center", band=frames.TIDAL_BAND,
                            footer_plates=False)
        band = round(W * frames.TIDAL_BAND)
        assert lay["bar"][1] > band
        for text, size, x, y, spacing, face in lay["footer"]:
            left, top, right, low = frames._font(size, face).getbbox(text)
            assert band < y + top and y + low <= W - frames.MARGIN, (st, text)


def test_the_tidal_footer_is_centred_in_the_visible_band():
    lay = frames.layout((W, W), [], style(), frames.footer_text(MOOD), align="center", band=frames.TIDAL_BAND,
                        footer_plates=False)
    band = round(W * frames.TIDAL_BAND)
    text, size, x, y, spacing, face = lay["footer"][-1]
    low = y + frames._font(size, face).getbbox(text)[3]
    assert abs((lay["bar"][1] + low) / 2 - (band + W) / 2) <= 3
