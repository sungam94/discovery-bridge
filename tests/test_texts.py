from bridge.texts import LANGUAGES, TEXTS, text


def test_every_language_has_the_same_keys():
    assert set(TEXTS) == set(LANGUAGES) == {"en", "de"}
    assert set(TEXTS["en"]) == set(TEXTS["de"])


def test_text_fills_in_the_values():
    assert text("en", "jobs", failed=3) == "3 feedback jobs failed in 24 h"
    assert text("de", "jobs", failed=3) == "3 Feedback-Jobs in 24 h fehlgeschlagen"
