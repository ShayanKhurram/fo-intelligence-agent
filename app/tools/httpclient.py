"""One pooled httpx client per distinct configuration, shared across the whole process.

Every tool module used to build a fresh `httpx.AsyncClient` per call. That is expensive in
a way that is invisible until it is fatal: constructing a client builds an SSL context, and
`ssl.create_default_context()` parses the entire system CA store — synchronously, on the
event loop. One call costs a few milliseconds; a batch run makes thousands, and the loop
stops running coroutines.

It is not hypothetical. A scheduled run wedged with 0 leads processed, no tool calls, no
log output and a core burning, and py-spy put the stack squarely in it:

    create_default_context (ssl.py:707)
    create_ssl_context (httpx/_config.py:40)
    __init__ (httpx/_transports/default.py:297)
    free_fetch_raw (app/tools/freefetch.py:30)

`app/llm.py` had already hit the same wall from the other direction (connection resets
under batch concurrency) and solved it with a private shared client; `app/tools/freefetch.py`
followed after the stall above. This module is that fix made available to every tool
module, so the next one does not have to rediscover it.

Clients are cached by their configuration, so callers that genuinely need different
timeouts or headers still get their own pooled client rather than sharing an unsuitable
one. No shutdown hook: a client's lifetime is the process's, and both the CLI and the
scheduler service exit as a unit.
"""
from __future__ import annotations

import httpx

_clients: dict[tuple, httpx.AsyncClient] = {}

# Bounded so a burst of concurrent leads cannot open an unlimited number of sockets — the
# condition that was producing connection resets before app/llm.py started pooling.
_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20)


def shared_client(
    *,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """A pooled client for this configuration, created once and reused.

    `headers` are the client's defaults; per-request headers (an API key that rotates, for
    instance) must still be passed at the call site rather than baked in here, or callers
    with different credentials would silently share one client's defaults.
    """
    key = (timeout, follow_redirects, tuple(sorted((headers or {}).items())))
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=follow_redirects,
            limits=_LIMITS,
        )
        _clients[key] = client
    return client


async def close_all() -> None:
    """Close every pooled client. For tests and for an orderly shutdown; not needed for
    process exit."""
    for client in list(_clients.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 — closing must not raise on the way out
            pass
    _clients.clear()
