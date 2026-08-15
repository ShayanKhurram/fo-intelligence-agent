"""T39 — the order leads are drawn from, and the rule that a submitted lead is never
re-elected. Offline: pure queue construction over a seeded DB, no agent, no network."""
from __future__ import annotations

import pytest

from app.db import add_entity_source, connection, upsert_checkpoint, upsert_entity
from app.scheduler import SOURCE_CLASS_PRIORITY, _queue, queue_breakdown


def _seed(conn, entity_id: str, source_class: str, name: str | None = None) -> None:
    upsert_entity(conn, entity_id, name or entity_id.upper())
    add_entity_source(conn, entity_id, source_class, {})


def test_tiers_are_drawn_in_the_configured_order(db_path):
    """Each tier is exhausted before the next is touched — the point of the feature: a run
    with a small target spends its budget on the best source available."""
    with connection(db_path) as conn:
        # Seeded deliberately in REVERSE priority order, so passing cannot be an accident
        # of insertion order.
        _seed(conn, "e_13dg", "schedule_13dg")
        _seed(conn, "e_conf", "c5_conferences")
        _seed(conn, "e_serp", "c6_serp")
        _seed(conn, "e_offers", "offers_services")
        _seed(conn, "e_edgar", "edgar_entity")
        _seed(conn, "e_adv", "adv_brochure")
        _seed(conn, "e_fec", "fec_employer")

    with connection(db_path) as conn:
        assert _queue(conn) == ["e_fec", "e_adv", "e_edgar", "e_offers",
                                "e_serp", "e_conf", "e_13dg"]


def test_a_lead_already_submitted_is_never_elected_again(db_path):
    """The standing instruction. `lead_checkpoints` is the record of what has been handed
    to the agent; anything in it is out."""
    with connection(db_path) as conn:
        _seed(conn, "fresh", "fec_employer")
        _seed(conn, "done", "fec_employer")
        upsert_checkpoint(conn, "done", status="verdict_done")

    with connection(db_path) as conn:
        assert _queue(conn) == ["fresh"]


@pytest.mark.parametrize("status", ["verdict_done", "failed", "running", "retry"])
def test_no_checkpoint_status_lets_an_excluded_class_back_in(db_path, status):
    """Resuming interrupted work must not become a back door for a tier the policy
    excludes. A 13f-only lead left 'running' by a crashed run has already been submitted
    once and does not return — measured: 26 such leads would otherwise have led the very
    next run."""
    with connection(db_path) as conn:
        _seed(conn, "old13f", "13f_filing")
        upsert_checkpoint(conn, "old13f", status=status)
        _seed(conn, "wanted", "fec_employer")

    with connection(db_path) as conn:
        assert _queue(conn) == ["wanted"]


def test_interrupted_work_in_a_wanted_tier_is_finished_first(db_path):
    """The other half: a lead in a listed tier that a crash left unfinished IS resumed,
    and goes ahead of new work, so a crash cannot strand it forever."""
    with connection(db_path) as conn:
        _seed(conn, "new_fec", "fec_employer")
        _seed(conn, "interrupted", "fec_employer")
        upsert_checkpoint(conn, "interrupted", status="retry")

    with connection(db_path) as conn:
        assert _queue(conn) == ["interrupted", "new_fec"]


def test_unlisted_classes_are_excluded_by_default(db_path):
    """13f_filing is absent from the priority list and from the project's discovery spec
    ("Nothing from edgar_13f enters the queue"): a 13F filer is an institutional manager,
    which is not evidence of a family office."""
    with connection(db_path) as conn:
        _seed(conn, "e13f", "13f_filing")
        _seed(conn, "efec", "fec_employer")

    with connection(db_path) as conn:
        assert _queue(conn) == ["efec"]


def test_unlisted_classes_can_be_appended_last_instead(db_path, monkeypatch):
    """The escape hatch, so excluding a large pool is a decision rather than a trap."""
    monkeypatch.setenv("FOIA_SCHEDULER_STRICT_SOURCES", "0")
    with connection(db_path) as conn:
        _seed(conn, "e13f", "13f_filing")
        _seed(conn, "efec", "fec_employer")

    with connection(db_path) as conn:
        assert _queue(conn) == ["efec", "e13f"]


def test_a_lead_is_ranked_by_its_strongest_class(db_path):
    """A lead corroborated by two sources is ordered by the better one — corroboration
    must not push a lead down the queue."""
    with connection(db_path) as conn:
        _seed(conn, "both", "adv_name")
        add_entity_source(conn, "both", "fec_employer", {})
        _seed(conn, "adv_only", "adv_name")

    with connection(db_path) as conn:
        assert _queue(conn) == ["both", "adv_only"]


def test_breakdown_reports_empty_tiers_too(db_path):
    """A tier with no leads means its connector has not been run — a fact worth showing,
    not one to hide."""
    with connection(db_path) as conn:
        _seed(conn, "efec", "fec_employer")

    with connection(db_path) as conn:
        tiers = queue_breakdown(conn)
    assert len(tiers) == len(SOURCE_CLASS_PRIORITY)
    assert tiers[0]["available"] == 1
    assert all(t["available"] == 0 for t in tiers[1:])


def test_a_failed_lead_comes_back_for_another_attempt(db_path):
    """A lead that failed was never actually assessed — a crash, a timeout, an outage.
    Excluding it forever would silently shrink the pool every time something went wrong,
    so it returns, ahead of untried work."""
    with connection(db_path) as conn:
        _seed(conn, "crashed", "fec_employer")
        upsert_checkpoint(conn, "crashed", status="failed")
        _seed(conn, "untried", "fec_employer")

    with connection(db_path) as conn:
        assert _queue(conn) == ["crashed", "untried"]


def test_only_a_lead_that_actually_got_a_verdict_is_retired(db_path):
    """The line between 'submitted' and 'failed': `verdict_done` means the agent reached a
    conclusion, so re-running it would spend money to learn nothing. Every other state is
    unfinished work."""
    with connection(db_path) as conn:
        for i, status in enumerate(["verdict_done", "failed", "retry", "running"]):
            _seed(conn, f"e{i}", "fec_employer")
            upsert_checkpoint(conn, f"e{i}", status=status)

    with connection(db_path) as conn:
        queued = set(_queue(conn))
    assert "e0" not in queued, "a lead with a verdict must not be re-run"
    assert {"e1", "e2", "e3"} <= queued, "failed/retry/interrupted leads must come back"
