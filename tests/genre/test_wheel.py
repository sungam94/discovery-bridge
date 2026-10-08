from datetime import datetime, timezone

import pytest

from bridge.db import connect, migrate
from bridge.genre.store import save_artist
from bridge.genre.wheel import anchor_hue, hues_for

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.sqlite")
    migrate(c)
    return c


def dist(a, b):
    return min((a - b) % 360, (b - a) % 360)


def test_named_genres_sit_on_their_place_of_the_wheel():
    assert anchor_hue("black metal") == 0 and anchor_hue("psytrance") == 310 and anchor_hue("jazz") == 162
    assert anchor_hue("Post-Rock") == anchor_hue("post rock") == 82


def test_the_end_of_a_genre_name_decides_and_longer_names_win():
    assert anchor_hue("psychedelic black metal") == anchor_hue("black metal")
    assert anchor_hue("dark psytrance") == anchor_hue("psytrance")
    assert anchor_hue("instrumental post-rock") == anchor_hue("post-rock")
    assert anchor_hue("dance-punk") == anchor_hue("pop punk") == anchor_hue("punk")
    assert anchor_hue("rap rock") == anchor_hue("rock")
    assert anchor_hue("garage rock") != anchor_hue("uk garage")
    assert anchor_hue("dubstep") != anchor_hue("dub")
    assert dist(anchor_hue("atmospheric sludge metal"), anchor_hue("doom metal")) <= 15


def test_neighbours_on_the_wheel_sound_alike_and_it_closes_between_psycore_and_black_metal():
    assert dist(anchor_hue("psycore"), anchor_hue("black metal")) <= 25
    assert dist(anchor_hue("doom metal"), anchor_hue("sludge metal")) <= 10
    assert dist(anchor_hue("ambient"), anchor_hue("psybient")) <= 15
    assert dist(anchor_hue("psytrance"), anchor_hue("jazz")) > 90


def test_umbrella_and_unknown_names_have_no_fixed_place():
    assert anchor_hue("zeuhl") is None and anchor_hue("progressive") is None and anchor_hue("experimental") is None


def test_unknown_genre_is_placed_between_the_genres_it_shares_artists_with(conn):
    save_artist(conn, "A", "1", [("zeuhl", 3), ("jazz", 2)], NOW)
    save_artist(conn, "B", "2", [("zeuhl", 3), ("jazz", 1)], NOW)
    save_artist(conn, "C", "3", [("zeuhl", 3), ("progressive rock", 1)], NOW)
    hue = hues_for(conn, ["zeuhl", "jazz"], {}, NOW)["zeuhl"]
    assert anchor_hue("progressive rock") < hue < anchor_hue("jazz")
    # once placed it stays, whatever is learned later
    save_artist(conn, "D", "4", [("zeuhl", 3), ("black metal", 1)], NOW)
    save_artist(conn, "E", "5", [("zeuhl", 3), ("black metal", 1)], NOW)
    assert hues_for(conn, ["zeuhl"], {}, NOW)["zeuhl"] == hue


def test_placement_wraps_around_the_wheel(conn):
    for i, other in enumerate(["psycore", "black metal", "psycore", "black metal"]):
        save_artist(conn, f"A{i}", str(i), [("noisecore x", 2), (other, 1)], NOW)
    hue = hues_for(conn, ["noisecore x"], {}, NOW)["noisecore x"]
    assert dist(hue, 350) <= 12


def test_too_little_evidence_leaves_a_genre_unplaced(conn):
    save_artist(conn, "A", "1", [("zeuhl", 3), ("jazz", 2)], NOW)
    assert hues_for(conn, ["zeuhl"], {}, NOW) == {"zeuhl": None}


def test_a_configured_hue_overrides_everything(conn):
    assert hues_for(conn, ["jazz", "zeuhl"], {"jazz": 10, "zeuhl": 200}, NOW) == {"jazz": 10, "zeuhl": 200}


def test_a_genre_linked_to_opposite_sides_of_the_wheel_stays_unplaced(conn):
    for i, other in enumerate(["psytrance", "jazz", "psytrance", "jazz", "doom metal", "ambient"]):
        save_artist(conn, f"A{i}", str(i), [("avant x", 2), (other, 1)], NOW)
    assert hues_for(conn, ["avant x"], {}, NOW) == {"avant x": None}


def test_classical_and_reggae_have_a_place():
    assert anchor_hue("modern classical") is not None and anchor_hue("reggae") is not None
    assert anchor_hue("dancehall") == anchor_hue("reggae") or abs(anchor_hue("dancehall") - anchor_hue("reggae")) <= 5


def test_broad_genres_have_a_place_in_their_range():
    assert anchor_hue("electronic") == anchor_hue("electronica")
    assert anchor_hue("experimental electronic") == anchor_hue("electronic")
