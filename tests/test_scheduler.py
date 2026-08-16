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
from app.db import add_entity_source, connection, get_run, upsert_entity
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
    """Seed leads WITH a discovery source class. T39's queue draws by source class and
    excludes anything with none — which is right, because a real lead always arrives from
    a connector. `fec_employer` is tier 1, so seeded leads are queued in insertion order."""
    ids = [f"e{i}" for i in range(n)]
    with connection(db_path) as conn:
        for eid in ids:
            upsert_entity(conn, eid, f"Firm {eid}")
            add_entity_source(conn, eid, "fec_employer", {})
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


async def test_progress_is_published_while_the_run_is_still_going(db_path, monkeypatch):
    """T38 — a run must report what it is doing WHILE it does it.

    Before this, a run was invisible until it finished: the row said 'running' and
    nothing else. The counters are written onto the run row after each lead, so any
    reader on a separate connection (the API, the UI) sees them live."""
    from app.db import get_active_run
    ids = _seed_leads(db_path, 4)
    seen: list[dict] = []

    async def fake_run_lead(entity_id, db_path=None):
        # Snapshot what an outside reader would see mid-run.
        with connection(db_path) as conn:
            active = get_active_run(conn)
        seen.append((active or {}).get("notes", {}))
        return {"cost_usd": 0.01}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 1, "usd_spent": 0.02}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    with connection(db_path) as conn:
        for eid in ids:
            conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue', '')", (eid,))

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=1,
    )

    assert result["processed"] == 4
    # The run was visible as 'running' throughout, with counters that climbed.
    assert len(seen) == 4
    assert seen[0].get("phase") in ("starting", "researching")
    processed_seen = [s.get("processed", 0) for s in seen]
    assert processed_seen == sorted(processed_seen), "counters must not go backwards"
    assert processed_seen[-1] > processed_seen[0], "counters must actually advance mid-run"
    # The lead being worked on is named while it is in flight.
    assert any(s.get("in_flight") for s in seen)
    # ...and once finished, no run is active any more.
    with connection(db_path) as conn:
        assert get_active_run(conn) is None


async def test_progress_names_the_target_so_a_bar_can_be_drawn(db_path, monkeypatch):
    """`target_confirmed` rides along in the progress notes; without it the UI has no
    finish line to draw and deliberately shows none."""
    from app.db import get_active_run
    _seed_leads(db_path, 2)
    captured: list[dict] = []

    async def fake_run_lead(entity_id, db_path=None):
        with connection(db_path) as conn:
            captured.append((get_active_run(conn) or {}).get("notes", {}))
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    with connection(db_path) as conn:
        for eid in ("e0", "e1"):
            conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue', '')", (eid,))

    await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": 2, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=1,
    )
    assert captured and captured[0].get("target_confirmed") == 2


async def test_the_hosted_mirror_is_pushed_after_the_run_is_closed(db_path, monkeypatch):
    """The push must happen AFTER finish_run and after the provenance rows are written.

    Originally it ran before both, so the hosted view received the run while it was still
    'running' with zero provenance rows — and nothing pushed again afterwards, leaving
    every completed run displayed as permanently in-flight with 0 leads. Observed on the
    live site before this was fixed."""
    pushed: list[dict] = []

    def fake_sync(db, *, limit=50):
        # Snapshot what the mirror would have seen at push time.
        with connection(db) as conn:
            row = conn.execute(
                "SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
            fp = conn.execute("SELECT COUNT(*) AS n FROM field_provenance").fetchone()["n"]
        pushed.append({"status": row["status"], "field_rows": fp})
        return {"status": "ok", "runs": 1, "fields": fp}

    async def fake_run_lead(entity_id, db_path=None):
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "sync_runs", fake_sync)
    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    _seed_leads(db_path, 1)
    with connection(db_path) as conn:
        conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e0','pursue','')")

    await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(),
    )

    assert pushed, "the hosted mirror was never pushed"
    final = pushed[-1]
    assert final["status"] == "done", "the last push must carry the run's FINAL status"
    assert final["field_rows"] > 0, "the last push must happen after provenance rows exist"


async def test_orphaned_runs_are_closed_at_startup(db_path):
    """A run only executes inside a live process. A row still marked `running` at startup
    belonged to a process that is gone, and leaving it makes it the newest 'active' run
    forever: the live panel shows its frozen counters, and a fresh run beside it reads as
    the old one restarting from zero. Three had accumulated across restarts."""
    from app.db import get_active_run, reconcile_orphaned_runs, start_run, upsert_checkpoint

    with connection(db_path) as conn:
        _seed_leads(db_path, 1)
        old = start_run(conn, "scheduled", entity_count=5)
        upsert_checkpoint(conn, "e0", status="running")

    with connection(db_path) as conn:
        assert get_active_run(conn) is not None          # before: looks active
        closed = reconcile_orphaned_runs(conn)

    assert closed == 1
    with connection(db_path) as conn:
        assert get_active_run(conn) is None, "no run may look active after a restart"
        assert get_run(conn, old)["status"] == "interrupted"
        # ...and its in-flight lead is retryable rather than stuck as busy forever.
        row = conn.execute(
            "SELECT status FROM lead_checkpoints WHERE entity_id = 'e0'").fetchone()
        assert row["status"] == "retry"


async def test_a_second_scheduled_run_will_not_start_beside_a_live_one(db_path, monkeypatch):
    """Two runs draw from the same queue and the same paid tools, and the second's
    counters overwrite the first's in every view — which is exactly what "it restarted
    from zero" looked like."""
    from app.db import start_run

    _seed_leads(db_path, 3)
    with connection(db_path) as conn:
        conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e0','pursue','')")
        start_run(conn, "scheduled", entity_count=3)     # one already in flight

    ran = []

    async def fake_run_lead(entity_id, db_path=None):
        ran.append(entity_id)
        return {"cost_usd": 0.0}

    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(),
    )

    assert result["termination"] == "already_running"
    assert result["run_id"] is None
    assert ran == [], "no lead may be processed by the refused run"


async def test_a_confirmed_lead_is_published_without_waiting_for_the_run_to_end(db_path, monkeypatch):
    """Draining only at run end means a run that is killed, crashes, or is simply still
    going leaves its confirmed leads unsearchable — measured: 28 confirmed leads sat
    pending against an empty corpus because no run had ended cleanly."""
    drains: list[str] = []

    def fake_drain(db, *, limit=25):
        drains.append("drained")
        return {"status": "ok", "ingested": 1, "failed": 0}

    async def fake_run_lead(entity_id, db_path=None):
        return {"cost_usd": 0.0}

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        return {"entity_id": entity_id, "outcome": "ship", "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "drain_queue", fake_drain)
    monkeypatch.setattr(sched_mod, "run_lead", fake_run_lead)
    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)
    _seed_leads(db_path, 2)
    with connection(db_path) as conn:
        for eid in ("e0", "e1"):
            conn.execute("INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue','')", (eid,))

    await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=1,
    )

    # Two confirmations during the run, plus the end-of-run drain.
    assert len(drains) >= 3, f"expected a drain per confirmed lead, got {len(drains)}"


# ---------------------------------------------------------------------------
# T40 — provenance emitted as leads finish, not only when the run does
# ---------------------------------------------------------------------------


async def test_errored_run_emits_provenance_for_leads_it_finished(db_path, monkeypatch):
    """T40(b) — the run-level except path. When a run raises mid-run, the except handler
    emits provenance for every lead in `processed` before finishing the run `failed`, so
    an ERRORED run still leaves a per-field log for the leads it did finish.

    Note this is the except-handler path, not the per-lead emission (T40(a)): an exception
    raised here propagates to the run-level except, which emits for everything in
    `processed`. The per-lead path is what saves the log when a process is KILLED (no
    except handler runs then) and is covered by its own test below.
    """
    from app.db import get_field_provenance
    ids = _seed_leads(db_path, 3)
    _stub_agent(monkeypatch, outcomes={eid: "ship" for eid in ids})
    with connection(db_path) as conn:
        for eid in ids:
            conn.execute(
                "INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue', '')",
                (eid,))

    sync_calls = {"n": 0}

    def boom_sync(db, *, limit=50):
        sync_calls["n"] += 1
        # The 3rd per-lead sync (lead 3 finishing) raises, mid-_tracked, AFTER leads 1
        # and 2 have already completed and emitted their own provenance. This is the
        # run-level except path, not a single lead failing (a single lead raising is
        # absorbed by _process_one and never ends the run).
        if sync_calls["n"] == 3:
            raise RuntimeError("interrupted mid-run")
        return {"status": "ok", "runs": 0, "fields": 0}

    monkeypatch.setattr(sched_mod, "sync_runs", boom_sync)

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=1,
    )
    assert result["termination"] == "error"
    with connection(db_path) as conn:
        rows = get_field_provenance(conn, run_id=result["run_id"])
    eids = {r["entity_id"] for r in rows}
    assert {"e0", "e1"} <= eids, "the 2 completed leads must have provenance rows"


async def test_per_lead_provenance_exists_while_the_next_lead_is_still_in_flight(
    db_path, monkeypatch
):
    """T40(a) in isolation — the per-lead emission inside `_tracked`, independent of the
    end-of-run reconciling pass AND independent of the run-level except handler.

    This is the only thing that saves the log when a run is KILLED (a service restart,
    SIGKILL): no except handler runs then, and the clean-completion block never runs
    either. So we prove that lead 1's rows already exist WHILE lead 2 is still being
    processed — mid-run, before the run ends and before any except handler could run.

    Discrimination: chunk_size=1 makes the leads strictly sequential, so when e1's
    enrichment runs, e0 has already passed through `_tracked`'s per-lead emission. If
    that emission is deleted, e0 has zero rows at this snapshot and the test fails.
    """
    from app.db import get_field_provenance
    ids = _seed_leads(db_path, 2)
    # _stub_agent wires fake_run_lead (Layer 1) and writes nothing else we need; we
    # override process_entity below so the e1 branch can snapshot mid-run state.
    _stub_agent(monkeypatch, outcomes={eid: "ship" for eid in ids})
    with connection(db_path) as conn:
        for eid in ids:
            conn.execute(
                "INSERT INTO decisions (entity_id, verdict, rationale) "
                "VALUES (?, 'pursue', '')",
                (eid,))

    seen: dict = {}
    real_conn = connection

    async def fake_process_entity(conn, entity_id, model, *, force=False):
        if entity_id == "e1":
            # e1 is being processed -> e0 has already finished and passed through
            # _tracked's per-lead emission. Snapshot whether e0's provenance rows exist
            # NOW, mid-run, on a SEPARATE connection (the outside-reader view).
            with real_conn(db_path) as c:
                running = c.execute(
                    "SELECT run_id FROM runs WHERE status='running' "
                    "ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if running is not None:
                    seen["run_id"] = running["run_id"]
                    seen["e0_rows_while_e1_in_flight"] = len(
                        get_field_provenance(c, run_id=running["run_id"], entity_id="e0"))
        return {"entity_id": entity_id, "outcome": "ship",
                "calls_spent": 0, "usd_spent": 0.0}

    monkeypatch.setattr(sched_mod, "process_entity", fake_process_entity)

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(), chunk_size=1,
    )
    assert result["termination"] != "error", "the run must finish cleanly for this test"
    assert seen.get("run_id") == result["run_id"], (
        "could not capture the running run_id mid-run")
    assert seen.get("e0_rows_while_e1_in_flight", 0) > 0, (
        "e0's provenance must be emitted by _tracked BEFORE e1 is processed — "
        "this is the only emission that survives a KILLED process (no except "
        "handler runs on SIGKILL/service restart)")


async def test_writing_a_leads_provenance_twice_is_an_upsert_not_a_duplicate(db_path, monkeypatch):
    """T40(d) — re-writing the same lead's provenance is an upsert, not a duplicate.
    field_provenance carries UNIQUE(run_id, entity_id, field) and write_field_provenance
    upserts on it, so the per-lead emission and the end-of-run reconciling pass produce
    one row per field, not two."""
    from app.db import get_field_provenance
    _seed_leads(db_path, 1)
    _stub_agent(monkeypatch, outcomes={"e0": "ship"})
    with connection(db_path) as conn:
        conn.execute(
            "INSERT INTO decisions (entity_id, verdict, rationale) VALUES ('e0', 'pursue', '')")

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(),
    )
    with connection(db_path) as conn:
        after_first = len(get_field_provenance(conn, run_id=result["run_id"]))
    assert after_first > 0

    # Re-emit the same lead's provenance via the same path the per-lead emission uses.
    sched_mod._emit_run_provenance(db_path, result["run_id"], [("e0", "ship")])
    with connection(db_path) as conn:
        after_second = len(get_field_provenance(conn, run_id=result["run_id"]))
    assert after_second == after_first, "re-emitting must upsert, not duplicate"


async def test_clean_run_provenance_shape_is_unchanged(db_path, monkeypatch):
    """T40(c) — a clean run's reconciling pass must produce the same provenance as
    before the per-lead emission was added: same entity set, same row keys, same JSON
    `record` blob. The per-lead upsert must not change what a clean run leaves behind."""
    from app.db import get_field_provenance
    _seed_leads(db_path, 2)
    _stub_agent(monkeypatch, outcomes={eid: "ship" for eid in ("e0", "e1")})
    with connection(db_path) as conn:
        for eid in ("e0", "e1"):
            conn.execute(
                "INSERT INTO decisions (entity_id, verdict, rationale) VALUES (?, 'pursue', '')",
                (eid,))

    result = await run_scheduled_job(
        {"schedule_id": "s1", "name": "t", "target_confirmed": None, "max_leads": None},
        db_path=db_path, model=object(),
    )
    with connection(db_path) as conn:
        rows = get_field_provenance(conn, run_id=result["run_id"])
    assert rows, "a clean run must still emit provenance"
    assert {r["entity_id"] for r in rows} == {"e0", "e1"}
    expected_keys = {"run_id", "entity_id", "field", "value", "status", "shipped",
                     "source_class", "extraction_method", "record"}
    for r in rows:
        assert expected_keys <= set(r.keys()), f"row missing keys: {sorted(r.keys())}"
        assert r["run_id"] == result["run_id"]
        # `record` is the full JSON blob of the field record, parsed back to a dict.
        rec = r["record"]
        assert isinstance(rec, dict)
        assert "field" in rec and "value" in rec and "how" in rec
