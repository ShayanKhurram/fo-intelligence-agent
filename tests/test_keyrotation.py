from __future__ import annotations

from app.tools.keyrotation import KeyRotator


def test_current_returns_first_key():
    r = KeyRotator(("a", "b", "c"))
    assert r.current == "a"


def test_rotate_advances_and_returns_true():
    r = KeyRotator(("a", "b"))
    assert r.rotate() is True
    assert r.current == "b"


def test_rotate_returns_false_when_no_more_keys():
    r = KeyRotator(("a",))
    assert r.rotate() is False
    assert r.current is None


def test_current_none_when_empty():
    r = KeyRotator(())
    assert r.current is None
    assert r.rotate() is False


def test_filters_out_falsy_keys():
    r = KeyRotator(("a", "", None, "b"))
    assert len(r) == 2
    assert r.current == "a"


def test_rotation_state_persists_after_exhaustion():
    r = KeyRotator(("a", "b"))
    r.rotate()
    assert r.rotate() is False
    assert r.current is None  # stays exhausted, doesn't wrap back to "a"


# --- is_exhaustion_response: the 400-not-429 credit signal ---


def test_429_always_counts_as_exhaustion():
    from app.tools.keyrotation import is_exhaustion_response

    assert is_exhaustion_response(429, "")
    assert is_exhaustion_response(429, "slow down")


def test_serper_400_not_enough_credits_counts_as_exhaustion():
    """The live signal that broke rotation: Serper reports a spent balance as HTTP 400
    {"message":"Not enough credits"}, not 429. Rotating only on 429 meant every
    web_search/fetch_page died on the first exhausted key while 4,998 credits sat unused on
    two other configured keys (found live 2026-08-12)."""
    from app.tools.keyrotation import is_exhaustion_response

    assert is_exhaustion_response(400, '{"message":"Not enough credits","statusCode":400}')


def test_scrapeops_401_consumed_all_credits_counts_as_exhaustion():
    from app.tools.keyrotation import is_exhaustion_response

    body = '{"API Credits":"You have consumed all your API credits. Please upgrade to a larger plan."}'
    assert is_exhaustion_response(401, body)


def test_ordinary_400_does_not_rotate():
    """A malformed query must NOT burn the whole key pool retrying a request that would
    fail identically on every key."""
    from app.tools.keyrotation import is_exhaustion_response

    assert not is_exhaustion_response(400, '{"message":"Invalid query parameter"}')
    assert not is_exhaustion_response(400, "")


def test_wrong_key_401_does_not_rotate():
    from app.tools.keyrotation import is_exhaustion_response

    assert not is_exhaustion_response(401, '{"message":"Unauthorized"}')


def test_unrelated_statuses_never_count():
    from app.tools.keyrotation import is_exhaustion_response

    assert not is_exhaustion_response(200, "not enough credits")
    assert not is_exhaustion_response(500, "not enough credits")
    assert not is_exhaustion_response(404, "")


def test_matching_is_case_insensitive():
    from app.tools.keyrotation import is_exhaustion_response

    assert is_exhaustion_response(402, "NOT ENOUGH CREDITS")


def test_none_body_is_safe():
    from app.tools.keyrotation import is_exhaustion_response

    assert not is_exhaustion_response(400, None)
