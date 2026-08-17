"""T43.1 — corpus validator worklist + checkpoint plumbing.

Offline by construction: psycopg2 is stubbed, so no Postgres is reached. The SQLite
side uses the tmp `db_path` fixture from conftest, which runs init_db() and therefore
creates `validator_checkpoints` via the migration.

What is under test:
  * build_worklist selects the right predicate per mode and orders by actionability.
  * Checkpoint skip is per (record_id, mode): a 'done' for the type pass does not
    suppress the email pass on the same record.
  * attempts >= 3 is skipped (poisoned).
  * --limit N yields N *processable* rows, not N rows of which some are skipped.
  * No write SQL ever reaches Postgres (every executed statement starts with SELECT).
"""
from __future__ import annotations

import sqlite3

import pytest

import app.corpus_validator as cv
from app.corpus_validator import WorkItem, build_worklist
from app.db import (
    connection,
    get_validator_checkpoint,
    init_db,
    upsert_validator_checkpoint,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_COLUMNS = ["record_id", "entity_name", "principal_name", "entity_type", "principal_email"]


class _FakeCursor:
    """Records every executed statement into the shared `executed` list on its
    connection, and returns a configured row set. fetchall returns rows as tuples in
    `_COLUMNS` order, exactly as psycopg2 would; build_worklist zips them with
    `cur.description` to build dicts."""

    def __init__(self, rows: list[tuple], executed: list[str]):
        self._rows = rows
        self._executed = executed

    @property
    def description(self):
        return [(c,) for c in _COLUMNS]

    def execute(self, sql: str, *args, **kwargs):
        self._executed.append(sql)
        return self

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        # Accumulate executed statements across ALL cursors this connection ever hands
        # out, so a test that runs build_worklist twice can assert on every statement,
        # not just the last cursor's.
        self.executed: list[str] = []

    def cursor(self):
        return _FakeCursor(self._rows, self.executed)

    def close(self):
        pass


def _stub_pg(monkeypatch, rows: list[tuple]) -> _FakeConn:
    """Wire a fake psycopg2.connect that returns a connection yielding `rows`."""
    fake_conn = _FakeConn(rows)
    monkeypatch.setattr(cv.psycopg2, "connect", lambda *_a, **_k: fake_conn)
    monkeypatch.setattr(cv, "_pg_dsn", lambda: "postgres://stub")
    return fake_conn


# A known row set, deliberately NOT in actionability order, so a test that asserts the
# SQL carries the right ORDER BY is meaningful (the stub itself does no sorting).
_ROWS = [
    # record_id, entity_name, principal_name, entity_type, principal_email
    ("r-no-email", "NoEmail Capital", None, "type_unconfirmed", None),
    ("r-with-email", "Email Capital", "Jane Doe", "type_unconfirmed", "jane@email.com"),
    ("r-blank-email", "Blank Capital", "Bob Smith", "type_unconfirmed", "  "),
    ("r-no-name", "NoName Capital", None, "type_unconfirmed", None),
    ("r-mfo", "Acme MFO", "Carl Roe", "MFO", "carl@acme.com"),
]


# ---------------------------------------------------------------------------
# 1. ordering + predicate
# ---------------------------------------------------------------------------


def test_type_mode_orders_email_bearing_rows_first(monkeypatch, db_path):
    fake = _stub_pg(monkeypatch, _ROWS)
    items = build_worklist("type", db_path=db_path)

    sql = fake.executed[-1]
    # The predicate selects only type_unconfirmed rows.
    assert "entity_type = 'type_unconfirmed'" in sql
    # The ORDER BY must put email-bearing rows first, then record_id for stability.
    assert "principal_email IS NOT NULL AND btrim(principal_email) <> ''" in sql
    assert "DESC" in sql
    assert "record_id ASC" in sql

    # The stub does no sorting/filtering (that is Postgres' job); build_worklist must
    # preserve the DB order it was handed. All _ROWS are returned in declaration order.
    assert [it.record_id for it in items] == [r[0] for r in _ROWS]
    # WorkItem carries the joined fields faithfully.
    by_id = {it.record_id: it for it in items}
    assert by_id["r-with-email"].principal_email == "jane@email.com"
    assert by_id["r-mfo"].entity_type == "MFO"


def test_email_mode_orders_name_bearing_rows_first(monkeypatch, db_path):
    fake = _stub_pg(monkeypatch, _ROWS)
    items = build_worklist("email", db_path=db_path)

    sql = fake.executed[-1]
    # The predicate selects rows with null/blank principal_email.
    assert "principal_email IS NULL OR btrim(principal_email) = ''" in sql
    # The ORDER BY must put name-bearing rows first, then record_id for stability.
    assert "principal_name IS NOT NULL AND btrim(principal_name) <> ''" in sql
    assert "DESC" in sql
    assert "record_id ASC" in sql

    # Stub returns all rows in declaration order; pass-through preserves it.
    assert [it.record_id for it in items] == [r[0] for r in _ROWS]


# ---------------------------------------------------------------------------
# 2. checkpoint skip is per (record_id, mode)
# ---------------------------------------------------------------------------


def test_done_checkpoint_skips_only_that_mode(monkeypatch, db_path):
    _stub_pg(monkeypatch, _ROWS)
    # Mark r-with-email done for the TYPE pass only.
    with connection(db_path) as conn:
        upsert_validator_checkpoint(conn, "r-with-email", "type", "done")

    type_items = build_worklist("type", db_path=db_path)
    email_items = build_worklist("email", db_path=db_path)

    assert "r-with-email" not in {it.record_id for it in type_items}
    # Still present for the email pass — the key is (record_id, mode), not record_id.
    assert "r-with-email" in {it.record_id for it in email_items}


def test_get_validator_checkpoint_is_per_mode(db_path):
    with connection(db_path) as conn:
        upsert_validator_checkpoint(conn, "r1", "type", "done")
    with connection(db_path) as conn:
        assert get_validator_checkpoint(conn, "r1", "type") is not None
        assert get_validator_checkpoint(conn, "r1", "email") is None


# ---------------------------------------------------------------------------
# 3. attempts >= 3 is skipped (poisoned)
# ---------------------------------------------------------------------------


def test_attempts_cap_skips_poisoned_rows(monkeypatch, db_path):
    _stub_pg(monkeypatch, _ROWS)
    with connection(db_path) as conn:
        # Bump r-no-email to attempts=3 for the type pass without marking it done.
        upsert_validator_checkpoint(conn, "r-no-email", "type", "failed")
        upsert_validator_checkpoint(conn, "r-no-email", "type", "failed")
        upsert_validator_checkpoint(conn, "r-no-email", "type", "failed")

    items = build_worklist("type", db_path=db_path)
    assert "r-no-email" not in {it.record_id for it in items}
    # r-no-email is still selectable for the email pass (no checkpoint there).
    email_items = build_worklist("email", db_path=db_path)
    assert "r-no-email" in {it.record_id for it in email_items}


def test_upsert_checkpoint_increments_attempts(db_path):
    with connection(db_path) as conn:
        upsert_validator_checkpoint(conn, "r1", "type", "failed")
        upsert_validator_checkpoint(conn, "r1", "type", "failed")
        row = get_validator_checkpoint(conn, "r1", "type")
    assert row is not None
    assert row["attempts"] == 2


# ---------------------------------------------------------------------------
# 4. --limit yields N processable rows even when earlier rows are checkpointed
# ---------------------------------------------------------------------------


def test_limit_counts_processable_rows_not_raw_rows(monkeypatch, db_path):
    # 10 type_unconfirmed rows, all with emails so they sort equally; order by record_id.
    rows = [
        (f"r{i:02d}", f"Entity {i}", f"Name {i}", "type_unconfirmed", f"n{i}@x.com")
        for i in range(10)
    ]
    _stub_pg(monkeypatch, rows)
    # Checkpoint the first 4 done for the type pass. They sort first by record_id, so
    # a naive LIMIT 5 in Postgres would return r00..r04 and yield only 1 processable row.
    with connection(db_path) as conn:
        for i in range(4):
            upsert_validator_checkpoint(conn, f"r{i:02d}", "type", "done")

    items = build_worklist("type", limit=5, db_path=db_path)
    # 5 processable rows, none of the 4 done ones — the limit counts post-skip rows.
    assert len(items) == 5
    assert [it.record_id for it in items] == ["r04", "r05", "r06", "r07", "r08"]
    for it in items:
        assert it.record_id not in {"r00", "r01", "r02", "r03"}


# ---------------------------------------------------------------------------
# 5. no write SQL reaches Postgres
# ---------------------------------------------------------------------------


def test_no_write_sql_reaches_postgres(monkeypatch, db_path):
    fake = _stub_pg(monkeypatch, _ROWS)
    build_worklist("type", db_path=db_path)
    build_worklist("email", db_path=db_path)

    statements = fake.executed
    assert statements, "expected at least one executed statement"
    for s in statements:
        stripped = s.strip().lstrip("(")
        assert stripped[:6].upper() == "SELECT", f"non-SELECT statement reached Postgres: {s!r}"


# ---------------------------------------------------------------------------
# init_db creates the validator_checkpoints table via the migration
# ---------------------------------------------------------------------------


def test_init_db_creates_validator_checkpoints(tmp_path):
    path = str(tmp_path / "fresh.db")
    init_db(path)
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='validator_checkpoints'"
        ).fetchall()
    assert rows and rows[0][0] == "validator_checkpoints"


# ===========================================================================
# T43.3 / T43.4 — backfill_principal_email, record_verdict (round 2)
# ===========================================================================
# Offline: the enrichment tier functions are stubbed so no network call is made.
# The SQLite side uses the tmp `db_path` fixture; claims / checkpoints are real.

import asyncio  # noqa: E402

from app.corpus_validator import (  # noqa: E402
    EmailResult,
    backfill_principal_email,
    record_verdict,
)
from app.db import get_claims, upsert_claim, upsert_entity  # noqa: E402
from app.state import Claim  # noqa: E402


def _item(record_id="e1", entity_name="Acme Capital", principal_name=None,
          aliases=None, entity_type="MFO", principal_email=None) -> WorkItem:
    return WorkItem(record_id=record_id, entity_name=entity_name,
                    principal_name=principal_name, aliases=aliases or [],
                    entity_type=entity_type, principal_email=principal_email)


def _claim(field_name, email, status="confirmed", source_class="snov",
            extraction_method="snov_emails_by_name_domain", source_url=None,
            confidence="medium") -> Claim:
    return Claim(field_name=field_name, answer=email, status=status,
                 source_url=source_url or "https://acme.com",
                 source_class=source_class, extraction_method=extraction_method,
                 confidence=confidence, produced_by="enrichment", wave="1")


def _stub_tiers(monkeypatch, *, snov=None, site=None, domain="acme.com"):
    """Wire resolve_domain + the two tier functions to fixed returns. Each stub records
    call order into a shared `calls` list so the tier-order test can assert on it."""
    calls: list[str] = []

    async def fake_resolve(name):
        calls.append(f"resolve:{name}")
        return domain

    async def fake_snov(d, p):
        calls.append("snov")
        return snov

    async def fake_site(d, p):
        calls.append("site")
        return site

    async def fake_attribution(email, entity_name, principal_name):
        # Route attribution through the offline deterministic fallback rather than the
        # LLM. Keeps the suite hermetic (an unstubbed classifier makes a real network
        # call and hangs the run) while still exercising the real decision logic: it
        # returns 'role' for info@, 'principal' when a name token is in the local part,
        # and 'unknown' — which maps to firm_email — when nobody is named.
        return cv._deterministic_attribution(email, principal_name), "test_fallback"

    async def fake_harvest(d):
        # The single site read that now serves every tier. Unstubbed it makes live HTTP
        # calls; default it to "the site published nothing" so the tests below exercise
        # the Snov path and the invariants.
        calls.append("harvest")
        return []

    monkeypatch.setattr(cv, "_harvest_site_emails", fake_harvest)

    async def fake_principal_site(d, p, harvested=None):
        # T43.8 tier 0 runs BEFORE Snov and fetches real pages. Unstubbed it makes live
        # network calls and hangs the suite, so default it to "found nothing" — the tier
        # order and invariant tests below are about what happens after it misses. Tests
        # that want to exercise it override this with their own stub.
        calls.append("principal_site")
        return None

    monkeypatch.setattr(cv, "resolve_domain", fake_resolve)
    monkeypatch.setattr(cv, "_find_email_via_snov", fake_snov)
    monkeypatch.setattr(cv, "_find_email_on_site", fake_site)
    monkeypatch.setattr(cv, "classify_email_attribution", fake_attribution)
    monkeypatch.setattr(cv, "find_principal_email_on_site", fake_principal_site)
    return calls


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_entity(db_path, record_id="e1", name="Acme Capital"):
    with connection(db_path) as conn:
        upsert_entity(conn, record_id, name)


def _claims_count(db_path, record_id):
    with connection(db_path) as conn:
        return len(get_claims(conn, record_id))


def _checkpoints_count(db_path, mode="email"):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT count(*) FROM validator_checkpoints WHERE mode = ?", (mode,)
        ).fetchone()[0]


# 1. no principal_name + a personal-looking Snov address -> firm_email


def test_no_principal_name_forces_firm_email(monkeypatch, db_path):
    snov = _claim("principal_email", "matt@acme.com")  # tier chain labels it principal
    _stub_tiers(monkeypatch, snov=snov, site=None)
    result = _run(backfill_principal_email(_item(principal_name=None)))
    assert result.field == "firm_email"
    assert result.email == "matt@acme.com"
    # `reason` now carries the attribution that chose the column, on a kept address as
    # well as a dropped one — the audit needs to know WHY a cell landed where it did, not
    # only why one was discarded. The address is kept either way.
    assert result.reason == "attribution_unknown"


# 2. role address + principal_name present -> firm_email


def test_role_address_is_firm_email_even_with_principal(monkeypatch, db_path):
    snov = _claim("principal_email", "info@acme.com")
    _stub_tiers(monkeypatch, snov=snov, site=None)
    result = _run(backfill_principal_email(_item(principal_name="Matt Blackburn")))
    assert result.field == "firm_email"
    assert result.email == "info@acme.com"


# 3. off-domain address -> dropped entirely, NO claim written


def test_off_domain_address_is_dropped_and_writes_no_claim(monkeypatch, db_path):
    snov = _claim("principal_email", "matt@capitalvalue.net", source_url="https://classvipartners.com")
    _stub_tiers(monkeypatch, snov=snov, site=None, domain="classvipartners.com")
    _seed_entity(db_path, "e1")
    result = _run(backfill_principal_email(_item(record_id="e1", principal_name="Matt Blackburn")))
    assert result.field is None
    assert result.email is None
    assert result.reason == "off_domain"
    # record_verdict must write NO claim for a field=None result.
    before = _claims_count(db_path, "e1")
    record_verdict(_item(record_id="e1"), result, db_path=db_path)
    after = _claims_count(db_path, "e1")
    assert before == after == 0


# 4. clean personal address + principal_name present -> principal_email


def test_clean_personal_address_is_principal_email(monkeypatch, db_path):
    snov = _claim("principal_email", "matt@acme.com")
    _stub_tiers(monkeypatch, snov=snov, site=None)
    result = _run(backfill_principal_email(_item(principal_name="Matt Blackburn")))
    assert result.field == "principal_email"
    assert result.email == "matt@acme.com"


# 5. field=None writes no claim: claims row count unchanged


def test_field_none_writes_no_claim(monkeypatch, db_path):
    _stub_tiers(monkeypatch, snov=None, site=None, domain=None)  # domain=None -> no_domain
    _seed_entity(db_path, "e1")
    # Pre-seed an unrelated claim so the count is non-zero and we can see it is unchanged.
    with connection(db_path) as conn:
        upsert_claim(conn, "e1", {"field_name": "aum_usd", "answer": 100, "status": "confirmed",
                                   "confidence": "high", "produced_by": "derived", "wave": "-1"})
    before = _claims_count(db_path, "e1")
    result = _run(backfill_principal_email(_item(record_id="e1")))
    assert result.field is None
    assert result.reason == "no_domain"
    record_verdict(_item(record_id="e1"), result, db_path=db_path)
    assert _claims_count(db_path, "e1") == before


# 6. tier order: principal_name present -> Snov before site; absent -> site before Snov


def test_tier_order_snov_first_when_principal_named(monkeypatch, db_path):
    calls = _stub_tiers(monkeypatch, snov=_claim("principal_email", "matt@acme.com"),
                        site=_claim("principal_email", "site@acme.com"))
    _run(backfill_principal_email(_item(principal_name="Matt Blackburn")))
    # Snov is called; site is NOT (Snov returned a confirmed address).
    assert "snov" in calls
    assert "site" not in calls


def test_site_is_harvested_once_before_snov(monkeypatch, db_path):
    """The site is read ONCE, before Snov, and that single harvest serves every tier.

    Replaces two older tier-order tests that asserted wave_1's snov/site interleaving.
    That order was abandoned deliberately: the old fallback re-fetched the same pages
    through the headless browser, which serialises on one shared renderer and pushed
    every row into the 120s timeout (~1 row per 2 minutes, measured live 2026-08-16)."""
    calls = _stub_tiers(monkeypatch, snov=_claim("firm_email", "info@acme.com"))
    _run(backfill_principal_email(_item(principal_name="Matt Blackburn")))
    assert calls.index("harvest") < calls.index("snov")
    # The browser-backed enrichment site tier is no longer part of the chain at all.
    assert "site" not in calls


def test_harvested_address_is_used_when_snov_misses(monkeypatch, db_path):
    """When Snov returns nothing, an address from the single site harvest is still used.

    This branch was shipped unexercised once: the default harvest stub returns [], so the
    fallback never ran in tests and an un-awaited coroutine reached production
    (`AttributeError: 'coroutine' object has no attribute 'field'`, 2026-08-16). Overriding
    the harvest is what makes the path real."""
    calls = _stub_tiers(monkeypatch, snov=None)

    async def harvest_with_hit(d):
        calls.append("harvest")
        return [("info@acme.com", "https://acme.com/contact")]

    monkeypatch.setattr(cv, "_harvest_site_emails", harvest_with_hit)
    result = _run(backfill_principal_email(_item(principal_name="Matt Blackburn")))
    assert result is not None and not asyncio.iscoroutine(result)
    assert result.email == "info@acme.com"
    assert result.field == "firm_email"  # role address -> never principal_email


def test_snov_still_called_when_no_principal_named(monkeypatch, db_path):
    """With nobody named, Snov runs a domain-wide search. Its result can only ever be
    firm_email (invariant 1), but a firm channel is still a channel."""
    calls = _stub_tiers(monkeypatch, snov=_claim("firm_email", "info@acme.com"))
    result = _run(backfill_principal_email(_item(principal_name=None)))
    assert "snov" in calls
    assert result.field == "firm_email"


# 7. dry_run=True writes nothing: claims and checkpoints both unchanged


def test_dry_run_writes_nothing(monkeypatch, db_path):
    snov = _claim("principal_email", "matt@acme.com")
    _stub_tiers(monkeypatch, snov=snov, site=None)
    _seed_entity(db_path, "e1")
    claims_before = _claims_count(db_path, "e1")
    ckpt_before = _checkpoints_count(db_path, "email")
    result = _run(backfill_principal_email(_item(record_id="e1", principal_name="Matt")))
    record_verdict(_item(record_id="e1"), result, dry_run=True, db_path=db_path)
    assert _claims_count(db_path, "e1") == claims_before
    assert _checkpoints_count(db_path, "email") == ckpt_before


# record_verdict writes a claim + checkpoint for a real verdict


def test_record_verdict_writes_claim_and_checkpoint(monkeypatch, db_path):
    snov = _claim("principal_email", "matt@acme.com", source_class="snov",
                  extraction_method="snov_emails_by_name_domain")
    _stub_tiers(monkeypatch, snov=snov, site=None)
    _seed_entity(db_path, "e1")
    result = _run(backfill_principal_email(_item(record_id="e1", principal_name="Matt Blackburn")))
    record_verdict(_item(record_id="e1"), result, db_path=db_path)
    with connection(db_path) as conn:
        rows = get_claims(conn, "e1")
    email_claims = [c for c in rows if c["field_name"] == "principal_email"]
    assert len(email_claims) == 1
    assert email_claims[0]["answer"] == "matt@acme.com"
    assert email_claims[0]["produced_by"] == "corpus_validator"
    assert _checkpoints_count(db_path, "email") == 1