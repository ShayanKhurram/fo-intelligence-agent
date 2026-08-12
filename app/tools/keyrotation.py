"""Multi-key rotation for tools with a per-key rate/credit limit (Serper, Snov.io).
When a key hits its limit, rotation moves forward to the next configured key so a long
batch run doesn't stall on one exhausted key. Rotation is forward-only and process-global
— once a key is known exhausted there's no reason to retry it again this run, since its
quota resets on its own schedule (daily/monthly), not something a live run should wait
around for."""
from __future__ import annotations

# Statuses that can carry a credit-exhaustion message. 429 is the obvious one (rate limit
# OR quota, depending on vendor), but several vendors report a spent balance as a plain
# client error instead — see is_exhaustion_response.
_EXHAUSTION_STATUSES = frozenset({400, 401, 402, 403, 429})

# Substrings that mean "this key has no quota left", matched case-insensitively against the
# response body. Deliberately narrow: a 400 for a malformed query must NOT rotate keys and
# burn the rest of the pool on a request that would fail identically every time.
_EXHAUSTION_PHRASES = (
    "not enough credits",
    "no credits",
    "insufficient credit",
    "consumed all your api credits",
    "credit limit",
    "quota exceeded",
    "out of credits",
    "limit reached",
)


def is_exhaustion_response(status_code: int, body: str | None = None) -> bool:
    """True if this response means "this key is spent, try the next one".

    Why this exists: Serper signals a spent balance as **HTTP 400** with
    `{"message":"Not enough credits"}`, not 429. Rotating on 429 alone therefore never
    fired — the 400 fell through to `raise_for_status()` and every call died on the first
    exhausted key while thousands of credits sat unused on the other configured keys
    (found live 2026-08-12: key[0] balance -11, keys[1]/[2] at 2499 each, every
    web_search/fetch_page failing). ScrapeOps reports the same condition as a 401.

    A bare 429 always counts (rate limit or quota — either way, moving on is correct).
    Any other status must ALSO match a credit-exhaustion phrase in the body, so an
    ordinary 400 (bad query) or 401 (wrong key) doesn't burn the whole pool retrying a
    request that is broken rather than unfunded.
    """
    if status_code == 429:
        return True
    if status_code not in _EXHAUSTION_STATUSES:
        return False
    haystack = (body or "").lower()
    return any(phrase in haystack for phrase in _EXHAUSTION_PHRASES)


class KeyRotator:
    def __init__(self, keys: tuple[str, ...] | list[str]):
        self._keys = [k for k in keys if k]
        self._index = 0

    @property
    def current(self) -> str | None:
        """The key to use right now, or None if every configured key is exhausted (or
        none were configured at all)."""
        if self._index >= len(self._keys):
            return None
        return self._keys[self._index]

    def rotate(self) -> bool:
        """Advances to the next key. Returns True if a new key is now current, False
        if there are no more keys left to try (caller should degrade normally)."""
        if self._index + 1 < len(self._keys):
            self._index += 1
            return True
        self._index = len(self._keys)  # mark exhausted, `current` now returns None
        return False

    def __len__(self) -> int:
        return len(self._keys)
