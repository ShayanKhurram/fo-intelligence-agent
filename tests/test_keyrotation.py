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
