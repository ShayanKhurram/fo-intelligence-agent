"""T36.1/T36.2 — schedules and the termination rules of a scheduled run.

Fully offline: `run_lead` and `process_entity` are monkeypatched at their module-level
names in `app.scheduler` (the pattern tests/test_runner.py established), so no LLM, no
network, no API key. What is under test is the ORCHESTRATION — when a run stops and why —
not the agent it drives.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.scheduler as sched_mod
from app.db import connection, get_run, upsert_entity
from app.scheduler import (
    compute_next_run,
    create_schedule,
    delete_schedule,
    due_schedules,
    get_schedule,
    list_schedules,
    run_scheduled_job,
    tick,
    update_schedule,
)

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _seed_leads(db_path: str, n: int) -> list[str]:
    ids = [f"e{i}" for i in range(n)]
    with connection(db_path) as conn:
        for eid in ids:
            upsert_entity(conn, eid, f"Firm {eid}")
    return ids


def _stub_agent(monkeypatch, *, verdict="pursue", outcomes=None, confirm_every=1):
    """Stub Layer 1 + enrichment. `confirm_every`=1 confirms every lead; 2 confirms every
    second one, etc. `outcomes` overrides per entity_id."""
    async def fake_run_lead(entity_id, db_path=None):
        return {"cost_usd": 0.01}

    calls: list[str] = []

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        calls.append(entity_id)
        if outcomes is not None:
            outcome = outcomes.get(entity_id, "reject_thin")
        else:
            outcome = "ship" if len(calls) % confirm_every == 0 else "reject_thin"
        return {"entity_id": entity_id, "outcome": outcome, "calls_spent": 1, "usd_spent": 0.02}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    # A decision row is what _process_one reads to decide whether to enrich.
    real_conn = connection

    def _write_decision(db_path, entity_id):
        with real_conn(db_path) as conn:
            conn.execute(
                "INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, ?, '')",
                (entity_id, verdict),
            )

    return calls, _write_decision


# ---------------------------------------------------------------------------
# schedule CRUD + next-run arithmetic
# ---------------------------------------------------------------------------


def test_compute_next_run_daily_today_then_tomorrow():
    # 12:00 now, 15:00 target -> today; 09:00 target -> tomorrow.
    assert compute_next_run("daily", time_utc="15:00", now=NOW) == NOW.replace(hour=15, minute=0)
    assert compute_next_run("daily", time_utc="09:00", now=NOW) == NOW.replace(hour=9) + timedelta(days=1)


def test_compute_next_run_daily_exactly_now_moves_to_tomorrow():
    """Strictly after, never equal — otherwise a schedule fires twice on the same tick."""
    assert compute_next_run("daily", time_utc="12:00", now=NOW) == NOW + timedelta(days=1)


def test_compute_next_run_interval():
    assert compute_next_run("interval", interval_minutes=15, now=NOW) == NOW + timedelta(minutes=15)


def test_create_and_list_schedule(db_path):
    with connection(db_path) as conn:
        sid = create_schedule(conn, "nightly", "daily", time_utc="03:00",
                              target_confirmed=10, max_leads=50, now=NOW)
        rows = list_schedules(conn)
        one = get_schedule(conn, sid)
    assert len(rows) == 1
    assert one["name"] == "nightly"
    assert one["target_confirmed"] == 10 and one["max_leads"] == 50
    assert one["enabled"] is True
    assert one["next_run_at"].startswith("2026-08-15T03:00")


@pytest.mark.parametrize("kind,time_utc,interval", [
    ("daily", None, None),          # daily without a time
    ("daily", "25:00", None),       # not a real hour
    ("interval", None, 0),          # interval of zero
    ("weekly", "03:00", None),      # unsupported kind
])
def test_invalid_schedule_specs_rejected(db_path, kind, time_utc, interval):
    with connection(db_path) as conn:
        with pytest.raises(ValueError):
            create_schedule(conn, "bad", kind, time_utc=time_utc, interval_minutes=interval)


def test_update_recomputes_next_run(db_path):
    with connection(db_path) as conn:
        sid = create_schedule(conn, "nightly", "daily", time_utc="03:00", now=NOW)
        before = get_schedule(conn, sid)["next_run_at"]
        after = update_schedule(conn, sid, kind="interval", interval_minutes=15, now=NOW)
    # Switching daily->interval must not leave the schedule waiting for tomorrow morning.
    assert after["next_run_at"] != before
    assert after["next_run_at"].startswith("2026-08-14T12:15")


def test_delete_schedule(db_path):
    with connection(db_path) as conn:
        sid = create_schedule(conn, "x", "interval", interval_minutes=5)
        assert delete_schedule(conn, sid) is True
        assert delete_schedule(conn, sid) is False
        assert get_schedule(conn, sid) is None


def test_due_schedules_respects_enabled_and_time(db_path):
    with connection(db_path) as conn:
        due_id = create_schedule(conn, "due", "daily", time_utc="09:00", now=NOW)          # tomorrow 09:00
        create_schedule(conn, "later", "daily", time_utc="15:00", now=NOW)                 # today 15:00
        paused = create_schedule(conn, "paused", "daily", time_utc="09:00", now=NOW)
        update_schedule(conn, paused, enabled=False, now=NOW)
        # ...at 09:01 tomorrow, only the enabled one that has come due fires.
        due = due_schedules(conn, now=NOW + timedelta(days=1, minutes=1))
        names = {d["name"] for d in due}
    assert "due" in names
    assert "paused" not in names          # disabled schedules never fire
    assert "later" in names               # today 15:00 is also in the past by then


# ---------------------------------------------------------------------------
# termination rules — the point of the feature
# ---------------------------------------------------------------------------


async def test_stops_at_target_confirmed(db_path, monkeypatch):
    """The target is the whole reason a scheduled run exists: stop as soon as it has what
    it was asked for, rather than burning the rest of the queue."""
    ids = _seed_leads(db_path, 20)
    calls, write_decision = _stub_agent(monkeypatch, confirm_every=1)
    for eid in ids:
        write_decision(db_path, eid)

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": 3, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=1,
    )
    assert result["termination"] == "target_reached"
    assert result["confirmed"] == 3
    # It stopped early: 20 leads were available, 3 were touched.
    assert result["processed"] == 3
    with connection(db_path) as conn:
        run = get_run(conn, result["run_id"])
    assert run["status"] == "done"
    assert run["notes"]["termination"] == "target_reached"


async def test_stops_at_max_leads_when_target_never_reached(db_path, monkeypatch):
    ids = _seed_leads(db_path, 20)
    # Nothing ever confirms, so only the cap can stop it.
    calls, write_decision = _stub_agent(monkeypatch, outcomes={eid: "reject_thin" for eid in ids})
    for eid in ids:
        write_decision(db_path, eid)

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": 99, "max_leads": 5},
        db_path=db_path, model=object(), chunk_size=2,
    )
    assert result["termination"] == "max_leads_reached"
    assert result["confirmed"] == 0
    # The cap is a cap, not a suggestion: a chunk may not overshoot it.
    assert result["processed"] == 5


async def test_terminates_automatically_when_leads_run_out(db_path, monkeypatch):
    """The automatic termination: a run that exhausts the queue ends cleanly rather than
    waiting for work that will never arrive."""
    ids = _seed_leads(db_path, 3)
    calls, write_decision = _stub_agent(monkeypatch, outcomes={eid: "reject_thin" for eid in ids})
    for eid in ids:
        write_decision(db_path, eid)

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": 100, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=2,
    )
    assert result["termination"] == "leads_exhausted"
    assert result["processed"] == 3


async def test_empty_queue_terminates_immediately(db_path, monkeypatch):
    _stub_agent(monkeypatch)
    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": 5, "max_leads": None},
        db_path=db_path, model=object(),
    )
    assert result["termination"] == "leads_exhausted"
    assert result["processed"] == 0


async def test_target_reached_on_the_final_chunk_reports_target_not_exhaustion(db_path, monkeypatch):
    """A run whose last lead completes the target stopped because it succeeded, not
    because it ran dry — the log has to say which."""
    ids = _seed_leads(db_path, 2)
    calls, write_decision = _stub_agent(monkeypatch, confirm_every=1)
    for eid in ids:
        write_decision(db_path, eid)
    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": 2, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=2,
    )
    assert result["confirmed"] == 2
    assert result["termination"] == "target_reached"


async def test_no_target_no_cap_runs_the_whole_queue(db_path, monkeypatch):
    ids = _seed_leads(db_path, 4)
    calls, write_decision = _stub_agent(monkeypatch, outcomes={eid: "ship" for eid in ids})
    for eid in ids:
        write_decision(db_path, eid)
    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=3,
    )
    assert result["processed"] == 4
    assert result["termination"] == "leads_exhausted"


async def test_a_lead_that_crashes_does_not_end_the_run(db_path, monkeypatch):
    """One bad lead never stalls an unattended run (plan §7's rule for the batch runner)."""
    ids = _seed_leads(db_path, 3)

    async def fake_run_lead(entity_id, db_path=None):
        if entity_id == "e1":
            raise RuntimeError("boom")
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    with connection(db_path) as conn:
        for eid in ids:
            conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue', '')", (eid,))

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=3,
    )
    assert result["processed"] == 3
    assert result["confirmed"] == 2          # the crashed lead is not confirmed
    with connection(db_path) as conn:
        assert get_run(conn, result["run_id"])["status"] == "done"


async def test_rejected_lead_is_never_enriched(db_path, monkeypatch):
    """Layer 1 rejecting a lead must stop the spend there — enrichment is the expensive
    half, and a rejected lead has already been judged."""
    _seed_leads(db_path, 2)
    enriched: list[str] = []

    async def fake_run_lead(entity_id, db_path=None):
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        enriched.append(entity_id)
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    with connection(db_path) as conn:
        conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e0', 'reject', '')")
        conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e1', 'pursue', '')")

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=2,
    )
    assert enriched == ["e1"]
    assert result["confirmed"] == 1


async def test_scheduled_run_writes_a_provenance_log(db_path, monkeypatch):
    """The Log tab's whole drill-down (run -> leads -> field logs) reads the provenance
    rows this emits, so a scheduled run that does not emit them is invisible in the UI."""
    from app.db import get_field_provenance
    _seed_leads(db_path, 1)
    _stub_agent(monkeypatch, outcomes={"e0": "ship"})
    with connection(db_path) as conn:
        conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e0', 'pursue', '')")

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(),
    )
    with connection(db_path) as conn:
        rows = get_field_provenance(conn, run_id=result["run_id"])
    assert rows, "a scheduled run must leave a per-field provenance log"
    assert {r["entity_id"] for r in rows} == {"e0"}


async def test_tick_fires_due_schedules_and_stamps_next_run(db_path, monkeypatch):
    fired: list[str] = []

    async def fake_job(schedule, **kwargs):
        fired.append(schedule["schedule_id"])
        return {"run_id": "r", "processed": 0, "confirmed": 0, "termination": "leads_exhausted",
                "usd_spent": 0.0, "rag": {}}

    monkeypatch.setattr(sched_mod, "run_scheduled_job", fake_job)
    with connection(db_path) as conn:
        sid = create_schedule(conn, "every-5", "interval", interval_minutes=5, now=NOW - timedelta(hours=1))

    await tick(db_path=db_path, now=NOW)
    assert fired == [sid]
    with connection(db_path) as conn:
        after = get_schedule(conn, sid)
    # Stamped BEFORE the run, so a run longer than the poll interval is not double-fired.
    assert after["last_run_at"] is not None
    assert after["next_run_at"] > NOW.isoformat()

    # A second tick at the same instant must not re-fire it.
    fired.clear()
    await tick(db_path=db_path, now=NOW)
    assert fired == []
