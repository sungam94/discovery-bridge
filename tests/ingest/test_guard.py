from bridge.ingest.guard import GuardState, check_fetch


def test_normal_fetch_accepted_and_resets():
    ok, st = check_fetch(["a"] * 30, 30, GuardState(guard_count=2, guard_hash="x", empty_count=2))
    assert ok and st == GuardState()


def test_empty_never_accepted():
    st = GuardState()
    for _ in range(5):
        ok, st = check_fetch([], 30, st)
        assert not ok
    assert st.empty_count == 5


def test_short_list_accepted_after_three_identical():
    short = ["a", "b"]
    ok1, st = check_fetch(short, 30, GuardState())
    ok2, st = check_fetch(short, 30, st)
    ok3, st = check_fetch(short, 30, st)
    assert (ok1, ok2, ok3) == (False, False, True) and st == GuardState()


def test_different_short_list_restarts_count():
    _, st = check_fetch(["a"], 30, GuardState())
    _, st = check_fetch(["b"], 30, st)
    assert st.guard_count == 1


def test_first_ever_fetch_accepted():
    ok, _ = check_fetch(["a"], None, GuardState())
    assert ok
