from bridge.artist_info.compose import clean_lastfm_bio, compose


def test_lastfm_bio_loses_its_link_and_licence_footer_and_html():
    raw = ('Hazy Fern is a <b>shoegaze</b> band &amp; more.\n\n'
           '<a href="https://www.last.fm/music/Hazy+Fern">Read more on Last.fm</a>. User-contributed text is '
           'available under the Creative Commons By-SA License; additional terms may apply.')
    assert clean_lastfm_bio(raw) == "Hazy Fern is a shoegaze band & more."
    assert clean_lastfm_bio("") == "" and clean_lastfm_bio(None) == ""


def test_full_text_has_bio_then_facts_then_source():
    text = compose(
        bio="Hazy Fern is a band.", bio_source="Last.fm",
        mb={"type": "Group", "begin": "2015", "area": "Tempe", "members": ["Alex", "Evan", "Ryan"]},
        lastfm={"listeners": 135318, "tags": ["shoegaze", "post-metal", "blackgaze"], "similar": ["Wren", "Deafheaven"]},
        local={"playlists": ["Hazy Fern Mix", "Warm Mix"], "plays": 12})
    assert text == ("Hazy Fern is a band.\n\n"
                    "Formed 2015 in Tempe\n"
                    "Members: Alex, Evan, Ryan\n"
                    "Last.fm: 135,318 listeners · shoegaze, post-metal, blackgaze\n"
                    "Similar: Wren, Deafheaven\n"
                    "In your Spotify mixes: Hazy Fern Mix, Warm Mix · played 12 times in Music Assistant\n\n"
                    "Bio: Last.fm (CC BY-SA)")


def test_a_person_is_born_and_missing_parts_are_left_out():
    text = compose(bio="", bio_source=None, mb={"type": "Person", "begin": "1986", "area": "Germany", "members": []},
                   lastfm=None, local={"playlists": [], "plays": 1})
    assert text == "Born 1986 in Germany\nplayed once in Music Assistant"


def test_nothing_known_gives_none():
    assert compose(bio="", bio_source=None, mb=None, lastfm=None, local={"playlists": [], "plays": 0}) is None


def test_long_lists_are_cut():
    text = compose(bio="B", bio_source="Wikipedia", mb={"type": "Group", "begin": None, "area": None,
                                                         "members": [f"M{i}" for i in range(9)]},
                   lastfm=None, local={"playlists": [f"P{i}" for i in range(7)], "plays": 0})
    assert "Members: M0, M1, M2, M3, M4, M5 and 3 more" in text
    assert "In your Spotify mixes: P0, P1, P2, P3, P4 and 2 more" in text
    assert text.endswith("Bio: Wikipedia (CC BY-SA)")
