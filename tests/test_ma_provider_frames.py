import importlib.util
import io
from pathlib import Path

from PIL import Image

_spec = importlib.util.spec_from_file_location(
    "spotify_bridge_frames", Path(__file__).parents[1] / "ma_provider" / "spotify_bridge" / "frames.py")
frames = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frames)

WHITE = (250, 250, 250)
HUES = {"psytrance": 310, "jazz": 162, "dub": 197, "black metal": 0, "hip hop": 178, "rap rock": 62,
        "dance-pop": 123, "dance-punk": 107, "doom metal": 28}
SIX = [["black metal", 4], ["hip hop", 4], ["rap rock", 3], ["dance-pop", 2], ["dance-punk", 2], ["doom metal", 2]]
MOOD = {"words": ["space", "energetic"], "instrumental": 0.9}


def cover(size=600, colour=WHITE) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def tiles(n=6, size=600) -> bytes:
    """A collage like MA's album grid: n x n tiles, each in its own colour."""
    img, t = Image.new("RGB", (size, size)), size // n
    for x in range(n):
        for y in range(n):
            img.paste((40 * x + 10, 40 * y + 10, 128), (x * t, y * t, (x + 1) * t, (y + 1) * t))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def drawn(data, genres, style=None, regrid=False, mood=None):
    return Image.open(io.BytesIO(frames.draw_genres(data, genres, style, regrid, HUES, mood))).convert("RGB")


def count(img, box, test):
    x0, y0, x1, y1 = (round(v) for v in box)
    return sum(1 for x in range(x0, x1) for y in range(y0, y1) if test(img.getpixel((x, y))))


def ink(line):
    """The box the line's glyphs actually cover, as drawn at (x, y) with Pillow's default anchor."""
    text, size, x, y = line
    left, top, right, bottom = frames._font(size).getbbox(text)
    return x + left, y + top, x + right, y + bottom


def test_default_style_is_the_shared_contract():
    assert frames.DEFAULT_STYLE == {"bands": 4, "font": 250, "font_min": 110, "darken": 0.62, "footer": 120, "footer2": 56,
                                    "grid": 3}


def test_the_fonts_ship_with_the_plugin():
    assert Path(frames._font(100).path).name == "ArchivoBlack-Regular.ttf"
    assert Path(frames._font(100, "mono").path).name == "DMMono-Medium.ttf"
    assert Path(frames._font(100).path).parent == Path(frames.__file__).parent / "fonts"


def plates_clear(plates, gap):
    """Plates never overlap: each next one starts at least `gap` below the one above."""
    ordered = sorted(plates, key=lambda b: b[1])
    return all(b[1] - a[3] >= gap for a, b in zip(ordered, ordered[1:]))


def test_each_genre_and_footer_line_sits_on_its_own_dark_plate():
    lay = frames.layout((1500, 1500), frames._shares(SIX), {**frames.DEFAULT_STYLE, "bands": 6},
                        frames.footer_text(MOOD))
    assert len(lay["plates"]) == len(lay["lines"]) + len(lay["footer"]) == 8
    px, py = frames.PLATE_PAD
    for line, plate in zip(lay["lines"], lay["plates"]):
        x0, y0, x1, y1 = ink(line)
        assert plate == (x0 - px, y0 - py, x1 + px, y1 + py)
    assert plates_clear(lay["plates"], frames.PLATE_GAP)
    out = drawn(cover(1500, (200, 200, 200)), SIX, {"bands": 6}, mood=MOOD)
    x0, y0, _, _ = lay["plates"][0]
    shade = out.getpixel((round(x0) + 3, round(y0) + 3))        # inside the plate, outside the letters
    open_cover = out.getpixel((1400, 1000))
    assert shade[0] < open_cover[0] * 0.5


def test_the_footer_plates_follow_the_letter_spaced_width():
    lay = frames.layout((1500, 1500), [], dict(frames.DEFAULT_STYLE), frames.footer_text(MOOD))
    px, _ = frames.PLATE_PAD
    for (text, size, x, _, spacing, face), plate in zip(lay["footer"], lay["plates"]):
        assert plate[0] == x - px
        assert plate[2] == round(x + frames.spaced_width(text, frames._font(size, face), spacing)) + px


def test_the_mood_words_are_in_the_display_face_and_the_vocal_label_in_mono():
    lay = frames.layout((1500, 1500), [], dict(frames.DEFAULT_STYLE), frames.footer_text(MOOD))
    assert [line[5] for line in lay["footer"]] == ["type", "mono"]
    one = frames.layout((1500, 1500), [], {**frames.DEFAULT_STYLE, "footer2": 0}, frames.footer_text(MOOD))
    assert [line[5] for line in one["footer"]] == ["type"]


def test_spotify_accent_bar_is_short_green_and_left_above_the_footer():
    lay = frames.layout((1500, 1500), frames._shares(SIX), dict(frames.DEFAULT_STYLE), frames.footer_text(MOOD))
    x0, y0, x1, y1 = lay["bar"]
    assert (x0, x1 - x0 + 1, y1 - y0 + 1) == (frames.MARGIN, frames.BAR_W, frames.BAR_H) == (60, 300, 8)
    assert y1 < min(p[1] for p in lay["plates"][len(lay["lines"]):])
    out = drawn(cover(1500, (20, 20, 20)), SIX, mood=MOOD)
    assert near(out.getpixel((x0 + 150, round((y0 + y1) / 2))), frames.FRAME_COLOURS["spotify"], tol=30)
    assert not near(out.getpixel((x1 + 60, round((y0 + y1) / 2))), frames.FRAME_COLOURS["spotify"], tol=60)


def test_colour_comes_from_the_genres_hue_on_the_wheel_and_is_light():
    red, magenta, green = (frames.genre_colour(g, HUES) for g in ("black metal", "psytrance", "jazz"))
    assert red[0] > red[1] and red[0] > red[2]              # hue 0 is red
    assert magenta[0] > magenta[1] and magenta[2] > magenta[1]
    assert green[1] > green[0]
    assert len({red, magenta, green}) == 3
    assert all(max(c) > 200 for c in (red, magenta, green))   # readable on the darkened cover


def test_genre_without_a_hue_is_light_grey():
    grey = frames.genre_colour("zeuhl", HUES)
    assert grey == frames.genre_colour("jazz", None) == frames.genre_colour("x", {"x": None})
    assert min(grey) > 180 and max(grey) - min(grey) < 12


def test_vocal_label_thresholds():
    assert frames.vocal_label(1.0) == frames.vocal_label(0.75) == "INSTRUMENTAL"
    assert frames.vocal_label(0.74) == frames.vocal_label(0.5) == frames.vocal_label(0.36) == "SOME VOCALS"
    assert frames.vocal_label(0.35) == frames.vocal_label(0.0) == "VOCALS"


def test_footer_text_from_the_moods_entry():
    assert frames.footer_text(MOOD) == "SPACE · ENERGETIC · INSTRUMENTAL"
    assert frames.footer_text({"words": ["dark"], "instrumental": 0.2}) == "DARK · VOCALS"
    assert frames.footer_text({"words": ["calm"], "instrumental": 0.5}) == "CALM · SOME VOCALS"


def test_footer_text_tolerates_missing_or_odd_entries():
    assert frames.footer_text(None) == frames.footer_text({}) == frames.footer_text("x") == ""
    assert frames.footer_text({"words": [], "instrumental": 0.1}) == "VOCALS"
    assert frames.footer_text({"words": ["dark"]}) == "DARK"
    assert frames.footer_text({"words": ["dark"], "instrumental": "?"}) == "DARK"


def test_the_cover_is_darkened_under_the_text():
    out = drawn(cover(), [["dub", 3]])
    dark = tuple(round(v * 0.38 + d * 0.62) for v, d in zip(WHITE, (12, 12, 14)))
    assert near(out.getpixel((590, 590)), dark, tol=6)
    assert near(drawn(cover(), [["dub", 3]], {"darken": 0}).getpixel((590, 590)), WHITE, tol=6)


def test_genres_are_written_top_down_in_their_colours():
    style = {**frames.DEFAULT_STYLE}
    lines = frames.layout((600, 600), [("black metal", 30), ("jazz", 10)], style, "")["lines"]
    out = drawn(cover(colour=(20, 20, 20)), [["black metal", 30], ["jazz", 10]])
    red, green = frames.genre_colour("black metal", HUES), frames.genre_colour("jazz", HUES)
    first, second = ink(lines[0]), ink(lines[1])
    assert [t for t, *_ in lines] == ["BLACK METAL", "JAZZ"]
    assert first[0] < 60 and first[1] < 60 and second[1] > first[3]
    assert count(out, first, lambda p: near(p, red)) > 200 and count(out, first, lambda p: near(p, green)) == 0
    assert count(out, second, lambda p: near(p, green)) > 50 and count(out, second, lambda p: near(p, red)) == 0


def test_genres_are_ordered_by_track_count():
    lines = frames.layout((600, 600), frames._shares([["dub", 2], ["jazz", 9], ["psytrance", 5]]),
                          dict(frames.DEFAULT_STYLE), "")["lines"]
    assert [t for t, *_ in lines] == ["JAZZ", "PSYTRANCE", "DUB"]


def test_text_size_follows_the_share_between_the_limits():
    st = {**frames.DEFAULT_STYLE, "font": 230, "font_min": 70}
    sizes = [s for _, s, *_ in frames.layout((1500, 1500), [("dub", 40), ("jazz", 10), ("pop", 1)], st, "")["lines"]]
    assert sizes == [230, 110, 74]    # the top genre at the largest size, the others by their share of its tracks
    small = [s for _, s, *_ in frames.layout((600, 600), [("dub", 40), ("pop", 1)], st, "")["lines"]]
    assert small == [92, 30]          # sizes scale with the cover


def test_a_genre_is_never_larger_than_one_with_more_tracks():
    shares = [("progressive psytrance", 10), ("dub", 10), ("pop", 6)]   # the long top name has to shrink
    sizes = [s for _, s, *_ in frames.layout((1500, 1500), shares, dict(frames.DEFAULT_STYLE), "")["lines"]]
    assert sizes[0] < 250 and sizes == sorted(sizes, reverse=True)


def test_six_genres_never_overlap_and_stay_clear_of_the_footer():
    for style in ({"bands": 6}, {"bands": 6, "font": 400, "font_min": 300}):
        st = {**frames.DEFAULT_STYLE, **style}
        shares = frames._shares(SIX)
        lay = frames.layout((1500, 1500), shares, st, frames.footer_text(MOOD))
        boxes = [ink(line) for line in lay["lines"]]
        assert len(boxes) == 6
        assert plates_clear(lay["plates"], frames.PLATE_GAP)
        assert lay["plates"][5][3] < lay["bar"][1]
        assert all(b[2] <= 1500 - frames.MARGIN for b in boxes)


def test_long_names_and_footers_are_shrunk_to_fit_the_width():
    mood = {"words": ["motivational", "documentary"], "instrumental": 0.5}
    lay = frames.layout((1500, 1500), [("progressive psytrance", 9)], dict(frames.DEFAULT_STYLE),
                        frames.footer_text(mood))
    assert ink(lay["lines"][0])[2] <= 1500 - frames.MARGIN
    for text, size, x, y, spacing, face in lay["footer"]:
        assert x + frames.spaced_width(text, frames._font(size, face), spacing) <= 1500 - frames.MARGIN


def test_footer_and_rule_only_with_a_moods_entry():
    dark = cover(1500, (20, 20, 20))
    with_mood, without = drawn(dark, SIX, mood=MOOD), drawn(dark, SIX)
    lay = frames.layout((1500, 1500), frames._shares(SIX), dict(frames.DEFAULT_STYLE), frames.footer_text(MOOD))
    text, size, x, y, _, face = lay["footer"][0]
    zone = (x, y, 1500 - frames.MARGIN, min(1500, y + sum(frames._font(size, face).getmetrics())))
    white = lambda p: min(p) > 200     # noqa: E731
    assert count(with_mood, zone, white) > 300 and count(without, zone, white) == 0
    x0, y0, x1, y1 = lay["bar"]
    green = frames.FRAME_COLOURS["spotify"]
    middle = round((y0 + y1) / 2)
    assert near(with_mood.getpixel((x0 + 150, middle)), green, tol=30)
    assert not near(without.getpixel((x0 + 150, middle)), green, tol=60)


def test_footer_without_genres_is_still_drawn():
    out = drawn(cover(1500, (20, 20, 20)), [], mood=MOOD)
    assert count(out, (0, 1300, 1500, 1500), lambda p: min(p) > 200) > 300


def test_at_most_the_configured_number_of_genres_is_drawn():
    genres = [[g, 10 - i] for i, g in enumerate(["jazz", "dub", "techno", "folk", "pop", "rock", "metal", "trance"])]
    lay = frames.layout((600, 600), frames._shares(genres)[:2], {**frames.DEFAULT_STYLE, "bands": 2}, "")
    assert len(lay["lines"]) == 2
    out = drawn(cover(colour=(20, 20, 20)), genres, {"bands": 2})
    f = frame_px(600) + 1   # inside the frame
    assert count(out, (f, ink(lay["lines"][1])[3] + 5, 600 - f, 600 - f), lambda p: max(p) > 120) == 0


def test_plain_genre_names_without_counts_are_accepted():
    out = drawn(cover(colour=(20, 20, 20)), ["jazz", "dub"])
    lines = frames.layout((600, 600), frames._shares(["jazz", "dub"]), dict(frames.DEFAULT_STYLE), "")["lines"]
    assert count(out, ink(lines[0]), lambda p: near(p, frames.genre_colour("jazz", HUES))) > 50


def test_unknown_and_old_style_keys_are_ignored():
    out = drawn(cover(), [["jazz", 3]], {"min_band": 150, "pad": 50, "font": 170, "whatever": 1})
    assert out.size == (600, 600)


def test_regrid_shows_fewer_larger_covers():
    out = drawn(tiles(), [], {"grid": 3}, regrid=True)
    assert out.size == (600, 600)
    for i in range(3):      # the top-left 3 x 3 tiles of the 6 x 6 collage now fill the cover
        assert near(out.getpixel((100 + 200 * i, 100 + 200 * i)), (40 * i + 10, 40 * i + 10, 128), tol=25)


def test_nothing_to_draw_only_adds_the_frame():
    out = Image.open(io.BytesIO(frames.draw_genres(tiles(), [], {"grid": 3}, False, HUES, None))).convert("RGB")
    assert near(out.getpixel((1, 300)), frames.FRAME_COLOURS["spotify"], tol=30)
    assert near(out.getpixel((50, 50)), (10, 10, 128), tol=25)        # not darkened, not regridded


def frame_px(size):
    return max(1, round(frames.FRAME * size / frames.BASE))


def assert_framed(img, colour, tol=30):
    w, h = img.size
    f = frame_px(w)
    for x, y in ((f // 2, h // 2), (w - 1 - f // 2, h // 2), (w // 2, f // 2), (w // 2, h - 1 - f // 2),
                 (w // 3, f // 2), (w // 3, h - 1 - f // 2)):
        assert near(img.getpixel((x, y)), colour, tol), (x, y, img.getpixel((x, y)))


def test_spotify_cover_has_a_thin_green_frame_on_all_four_sides():
    out = drawn(cover(1500, (90, 20, 160)), SIX, mood=MOOD)
    assert frames.FRAME == 10 and frames.FRAME_COLOURS["spotify"] == (0x1D, 0xB9, 0x54)
    assert_framed(out, frames.FRAME_COLOURS["spotify"])
    inside = out.getpixel((frame_px(1500) + 4, 750))
    assert not near(inside, frames.FRAME_COLOURS["spotify"], tol=30)   # thin: only the frame is green


def test_restyle_file_rewrites_the_cover_in_place(tmp_path):
    path = tmp_path / "7_1_thumb.jpg"
    path.write_bytes(cover())
    frames.restyle_file(str(path), [["jazz", 3]], None, False, HUES, MOOD)
    assert near(Image.open(path).convert("RGB").getpixel((590, 300)), (102, 102, 103), tol=8)
    assert [p.name for p in tmp_path.iterdir()] == ["7_1_thumb.jpg"]


def test_footer_has_the_mood_words_on_top_and_the_vocal_label_below_in_smaller_type():
    lay = frames.layout((1500, 1500), frames._shares(SIX), dict(frames.DEFAULT_STYLE), frames.footer_text(MOOD))
    (top, top_size, _, top_y, _, top_face), (low, low_size, _, low_y, _, low_face) = lay["footer"]
    assert (top, low) == ("SPACE · ENERGETIC", "INSTRUMENTAL")
    top_ink = frames._font(top_size, top_face).getbbox(top)
    low_ink = frames._font(low_size, low_face).getbbox(low)
    assert low_size < top_size and low_y + low_ink[1] > top_y + top_ink[3]
    assert low_y + low_ink[3] + frames.PLATE_PAD[1] <= 1500 - frames.MARGIN
    assert lay["bar"][3] < top_y + top_ink[1]


def test_the_footer_is_much_larger_than_one_line_could_be():
    one = frames.layout((1500, 1500), [], {**frames.DEFAULT_STYLE, "footer2": 0}, frames.footer_text(MOOD))["footer"]
    two = frames.layout((1500, 1500), [], dict(frames.DEFAULT_STYLE), frames.footer_text(MOOD))["footer"]
    assert len(one) == 1 and len(two) == 2 and two[0][1] >= 1.4 * one[0][1]


def test_a_footer_without_mood_words_stays_on_one_line():
    lay = frames.layout((1500, 1500), [], dict(frames.DEFAULT_STYLE), frames.footer_text({"words": [], "instrumental": 0.1}))
    assert [line[0] for line in lay["footer"]] == ["VOCALS"]


def test_the_vocal_line_is_drawn_lighter_than_the_mood_line():
    out = drawn(cover(1500, (20, 20, 20)), [], mood=MOOD)
    (_, s1, x1, y1, _, _), (_, s2, x2, y2, _, _) = frames.layout((1500, 1500), [], dict(frames.DEFAULT_STYLE),
                                                         frames.footer_text(MOOD))["footer"]
    white = lambda p: min(p) > 235                    # noqa: E731
    grey = lambda p: 150 < min(p) < 215 and max(p) - min(p) < 12   # noqa: E731
    assert count(out, (x1, y1, 1500, y1 + s1), white) > 300
    assert count(out, (x2, y2, 1500, y2 + s2), grey) > 300 and count(out, (x2, y2, 1500, y2 + s2), white) < 50
