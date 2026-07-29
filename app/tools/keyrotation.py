"""Multi-key rotation for tools with a per-key rate/credit limit (Serper, Hunter).
When a key hits its limit (HTTP 429), rotation moves forward to the next configured
key so a long batch run doesn't stall on one exhausted key. Rotation is forward-only
and process-global — once a key is known exhausted there's no reason to retry it
again this run, since its quota resets on its own schedule (daily/monthly), not
something a live run should wait around for."""
from __future__ import annotations


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
