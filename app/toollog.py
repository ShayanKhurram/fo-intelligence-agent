"""T35.2 — capture layer for the per-run field provenance log (PLAN.md T35).

Every external tool call enrichment makes while processing an entity is recorded as a
`tool_calls` row, attributed to (run_id, entity_id, wave), so each output lead field can
later be traced back to the exact call that produced it. This module is the *capture*
side; `app/db.py:write_tool_call` is the *storage* side and `app/schema.sql` defines the
table.

Design constraints, every one of which is load-bearing:

- **ContextVar, not a module global.** `app/enrichment._process_entities_concurrently`
  runs entities concurrently in asyncio tasks; a module global would cross-attribute one
  entity's tool calls to another. The ContextVar gives each task its own bound context,
  so two concurrent tasks never write each other's entity_id.

- **Outside a bound context, recording is a silent no-op** and the wrapped tool still
  returns its value normally. Every existing test calls these tools with no context bound
  and must keep passing untouched — the decorator changes nothing about behaviour when no
  run is active.

- **`logged` never changes behaviour.** It times the call, records, and returns the
  value. A raised exception is recorded as ok=0 and re-raised unchanged.

- **These tools signal failure by RETURNING a dict** with a truthy ``"error"`` key rather
  than by raising (e.g. a missing SERPER_API_KEY returns ``{"results": [], "error": ...}``
  — see app/tools/serper.py). The recorder reads that shape: a truthy ``error`` key =>
  ok=0 with that string; a ``cache_hit: True`` (app/tools/cache.py) => cache_hit=1, so the
  log never claims a cached answer was fetched this run.

- **Redact `args`.** Drop any key matching ``(?i)key|token|secret|password|auth`` and
  truncate every string value to 200 chars. Args are bound from the call's
  positional+keyword arguments via ``inspect.signature(...).bind_partial(...)``; anything
  not JSON-serialisable is skipped rather than raising.

- **`result_summary` is a short deterministic string, never the payload.** ``f"{n}
  results"`` for a dict with a `results` list, ``f"{n} chars"`` for a str return or a dict
  with `content`, ``"empty"`` for a falsy/None return, ``f"error: {msg}"`` on the error
  path. Capped at 200 chars. `result_url` is the `url` arg when present, else the `query`
  arg, else None.

- **A failure inside the recorder must never propagate into the tool path.** The whole
  record/write is wrapped in ``try/except Exception: pass`` (DEBUG log). Losing a log
  line is acceptable; losing a lead is not. The sqlite write goes through
  ``asyncio.to_thread`` + a short-lived ``connection(db_path)``, the same pattern
  ``app/tools/cache.py`` uses, so a blocking sqlite3 call never stalls the event loop.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator

from app.db import connection, write_tool_call

logger = logging.getLogger(__name__)

# The bound provenance context for the current asyncio task. None when no run is active
# (every existing test path) — record_tool_call is a no-op in that case.
_CTX: ContextVar["BoundContext | None"] = ContextVar("toollog_ctx", default=None)

# Keys whose names match this (case-insensitive) are stripped from the persisted args —
# a tool call's arguments must never leak a raw API key / token / secret.
_REDACT_KEY = re.compile(r"(?i)key|token|secret|password|auth")

_MAX_STR = 200

# Strong references to fire-and-forget log-write tasks. The event loop holds only a
# weak reference to tasks created via ensure_future, so an unreferenced task can be
# garbage-collected before it runs and the row silently vanishes — this codebase already
# hit exactly that (app/api.py keeps a strong ref for the same reason). A done callback
# discards the task so the set can't grow without bound.
_PENDING_WRITES: set[asyncio.Task] = set()


@dataclass
class BoundContext:
    """The (entity_id, run_id, wave, db_path) bound for the current task. `run_id` /
    `wave` are optional because the enrichment path binds once per entity with the wave
    left None rather than restructuring per-wave (an unattributed wave is a small loss,
    a restructure is a big risk — see the brief)."""
    entity_id: str
    run_id: str | None = None
    wave: str | None = None
    db_path: str | None = None


@contextmanager
def tool_log_context(
    entity_id: str,
    *,
    run_id: str | None = None,
    wave: str | None = None,
    db_path: str | None = None,
) -> Iterator[BoundContext]:
    """Bind a provenance context for the current asyncio task. Tool calls made by any
    `@logged` tool inside this block are written as `tool_calls` rows attributed to
    (entity_id, run_id, wave). Nesting replaces (does not merge) — the innermost context
    wins, restored on exit."""
    ctx = BoundContext(entity_id=entity_id, run_id=run_id, wave=wave, db_path=db_path)
    token = _CTX.set(ctx)
    try:
        yield ctx
    finally:
        _CTX.reset(token)


def _redact_args(bound_args: inspect.BoundArguments) -> dict[str, Any]:
    """Turn the bound call arguments into a redacted, JSON-safe dict. Drops any key whose
    name matches the redact pattern; truncates string values to _MAX_STR; skips anything
    not JSON-serialisable rather than raising.

    The serialisability check uses a STRICT ``json.dumps(value)`` (no ``default=``) — with
    ``default=str`` every object becomes serialisable and the guard never fires, so a
    callable argument (e.g. ``fetch_page_free_first``'s ``fallback``) would be stored as
    ``"<function ... at 0x...>"``. The final encode in ``_do_write`` keeps ``default=str``
    as belt-and-braces, but the gate here must be strict so junk is dropped, not stringified."""
    try:
        raw = dict(bound_args.arguments)
    except Exception:  # noqa: BLE001 — signature binding is best-effort
        return {}
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if _REDACT_KEY.search(name):
            continue
        try:
            if isinstance(value, str):
                value = value[:_MAX_STR]
            # Strict round-trip: no default=, so a non-serialisable value (a function,
            # a live connection, a coroutine) raises and is skipped rather than stored
            # as a repr string that pretends to be data.
            json.dumps(value)
            out[name] = value
        except (TypeError, ValueError):
            continue
    return out


def _result_summary(value: Any, *, error: str | None) -> str:
    """Short deterministic summary, never the payload. See module docstring for the
    rules. Capped at _MAX_STR."""
    if error:
        return f"error: {error}"[:_MAX_STR]
    if isinstance(value, dict):
        if isinstance(value.get("results"), list):
            return f"{len(value['results'])} results"[:_MAX_STR]
        if isinstance(value.get("content"), str):
            return f"{len(value['content'])} chars"[:_MAX_STR]
        if not value:
            return "empty"
    if isinstance(value, str):
        return f"{len(value)} chars"[:_MAX_STR]
    if not value:
        return "empty"
    return "ok"


def _result_url(bound_args: inspect.BoundArguments) -> str | None:
    """The target URL the call was directed at, when the wrapped function has a `url` arg
    that is a non-empty string, else None.

    Only `url` — never a search `query`. `result_url` is the column T35.4 will match
    against a claim's `source_url` to attribute a cell to a call, and it is what a web
    page renders as the source link; a free-text query in a URL column would be a lie in
    the data model. The query is already captured in `args` (where it belongs)."""
    args = bound_args.arguments
    url = args.get("url")
    if isinstance(url, str) and url:
        return url
    return None


def _do_write(ctx: BoundContext, tool: str, args: dict[str, Any], *, ok: bool,
              error: str | None, result_summary: str | None, result_url: str | None,
              cache_hit: bool, duration_ms: int | None) -> None:
    """The actual sqlite write, on a short-lived connection to `ctx.db_path`. Synchronous
    — must be run off the event loop via asyncio.to_thread (sqlite3 is blocking)."""
    args_json = json.dumps(args, default=str)
    with connection(ctx.db_path) as conn:
        write_tool_call(
            conn,
            run_id=ctx.run_id,
            entity_id=ctx.entity_id,
            wave=ctx.wave,
            tool=tool,
            args=args_json,
            ok=int(ok),
            error=error,
            result_summary=result_summary,
            result_url=result_url,
            cache_hit=int(cache_hit),
            duration_ms=duration_ms,
        )


def record_tool_call(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool,
    error: str | None = None,
    result_summary: str | None = None,
    result_url: str | None = None,
    cache_hit: bool = False,
    duration_ms: int | None = None,
) -> None:
    """Record one tool call against the currently bound context. A no-op when no
    `tool_log_context` is bound (the common case in existing tests). Any failure is
    swallowed — losing a log line is acceptable, losing a lead is not.

    Sync entry point: when called inside a running loop it schedules the sqlite write as
    a fire-and-forget `to_thread` task; when called with no running loop it writes
    inline. The `logged` wrapper below instead awaits the write so a row is visible by
    the time the tool returns (fire-and-forget there would race with a caller's
    immediate DB read)."""
    ctx = _CTX.get()
    if ctx is None:
        return
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            _do_write(ctx, tool, args, ok=ok, error=error, result_summary=result_summary,
                      result_url=result_url, cache_hit=cache_hit, duration_ms=duration_ms)
        else:
            task = asyncio.ensure_future(asyncio.to_thread(
                _do_write, ctx, tool, args, ok=ok, error=error, result_summary=result_summary,
                result_url=result_url, cache_hit=cache_hit, duration_ms=duration_ms))
            # Hold a strong reference until the task finishes — the loop's weak ref can GC
            # an unreferenced task mid-write (same bug app/api.py guards against).
            _PENDING_WRITES.add(task)
            task.add_done_callback(_PENDING_WRITES.discard)
    except Exception:  # noqa: BLE001 — never propagate into the tool path
        logger.debug("toollog: failed to record %s call", tool, exc_info=True)


def logged(tool_name: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Async decorator that records each call as a `tool_calls` row when a
    `tool_log_context` is bound, and is otherwise transparent. Never changes the wrapped
    function's return value, raised exception, retry, cache or rate-limit behaviour.

    The write is awaited (not fire-and-forget) so the row is committed before the tool
    returns — a caller reading the DB immediately after the call sees it. Any recorder
    failure is swallowed so it can never break the tool's own contract."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _CTX.get()
            # When no provenance context is bound, behave exactly like the bare function —
            # every existing test path hits this branch and must be unaffected.
            if ctx is None:
                return await fn(*args, **kwargs)
            start = time.monotonic()
            error: str | None = None
            ok = True
            cache_hit = False
            value: Any = None
            try:
                value = await fn(*args, **kwargs)
                # Tools signal failure by returning a dict with a truthy "error" key
                # rather than by raising — read that shape so the log reflects it.
                if isinstance(value, dict):
                    if value.get("error"):
                        ok = False
                        error = str(value["error"])[:500]
                    if value.get("cache_hit") is True:
                        cache_hit = True
                elif value is None:
                    # A None return means the call FAILED (e.g. fetch_raw_html returns
                    # None on an httpx.HTTPError). It must not be logged as a success —
                    # an outage that looks like an absence is exactly the confusion this
                    # log exists to prevent (PROJECT_LOG.md "an outage must not look like
                    # an absence in the email path"). Distinguish "the tool failed" from
                    # "there was nothing there".
                    ok = False
                    error = "returned None (call failed)"
                return value
            except Exception as exc:
                ok = False
                error = f"{type(exc).__name__}: {exc}"[:500]
                raise
            finally:
                duration_ms = int((time.monotonic() - start) * 1000)
                # Bind the call's arguments to the signature so the recorded args reflect
                # what was actually passed (positional + keyword), regardless of how the
                # caller chose to pass them.
                try:
                    bound = sig.bind_partial(*args, **kwargs)
                    bound.apply_defaults()
                except Exception:  # noqa: BLE001 — best-effort
                    bound = None
                redacted = _redact_args(bound) if bound is not None else {}
                summary = _result_summary(value, error=error)
                url = _result_url(bound) if bound is not None else None
                # Awaited write — see decorator docstring. The whole record is best-effort:
                # a recorder bug must never reach the caller. record_tool_call swallows
                # its own errors; guard here too so a bind/summary failure can't either.
                #
                # BaseException, not Exception: if this await is cancelled (lane timeouts
                # cancel coroutines via asyncio.wait_for — see app/trace_viewer.py's
                # lane_timeout), the CancelledError is a BaseException and would otherwise
                # replace whatever exception the tool was propagating. Losing a log line
                # must never change WHICH exception the caller sees.
                try:
                    await asyncio.to_thread(
                        _do_write, ctx, tool_name, redacted, ok=ok, error=error,
                        result_summary=summary, result_url=url, cache_hit=cache_hit,
                        duration_ms=duration_ms,
                    )
                except BaseException:  # noqa: BLE001 — see comment above
                    logger.debug("toollog: recorder raised for %s", tool_name, exc_info=True)

        return wrapper

    return decorator