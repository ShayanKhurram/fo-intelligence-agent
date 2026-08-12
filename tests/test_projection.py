"""PLAN.md T19.6 — end-to-end proof at the ROW level that the researcher's findings
reach the row. Seeds an entity in Class VI's real signal shape, runs wave_minus_1 with
the Layer-1 claims, then gathers survivors and reads the emitted record row.

This is the regression guard for the live defect the whole task exists to fix: Layer 1
confirmed G2.Q1 (the decision-maker) with a real source URL, but the Layer-D row came
out with `principal_name` blank because (a) Layer 1 keys claims on `question_id` while
the row pivots on `field_name` — nothing maps between them, and (b) the stored answer
was the literal string "Yes", so even a mapping would have had nothing to project.
T19.1-T19.5 fix both: the claim carries a `subject_value`, the compress step fills it,
and wave -1 projects it into a `field_name` claim the row can pivot on."""
from __future__ import annotations

from app.db import add_entity_source, connection, get_claims, upsert_claims, upsert_entity
from app.validation import check_v5_staleness
from app.dataset import _records_rows, gather_survivors
from app.enrichment import _claim_from_row, wave_minus_1


_CLASS_VI_URL = "https://classvifamilyoffice.com/team-member/matt-blackburn/"
_RESEARCHER_SOURCE_CLASS = "web_page"


def _seed_classvi(conn, entity_id="classvi"):
    """An entity shaped like CLASS VI FAMILY OFFICE, LLC's real ingestion record:
    an adv_name row carrying RAUM, a firm website, and HNW-client count, plus the
    two Layer-1 claims the researcher confirmed (decision-maker + title)."""
    upsert_entity(conn, entity_id, "CLASS VI FAMILY OFFICE, LLC")
    add_entity_source(
        conn, entity_id, "adv_name",
        {"signals": {
            "raum_usd": 1498011942,
            "website": "HTTPS://WWW.CLASSVIPARTNERS.COM",
            "hnw_clients": 148,
        }},
        url="https://adviserinfo.sec.gov/classvi",
    )
    # Layer-1 claims as the compress step would emit them post-T19.2: a self-contained
    # answer AND a bare subject_value the projection can lift into a field claim.
    upsert_claims(conn, entity_id, [
        {
            "question_id": "G2.Q1",
            "answer": "Matt Blackburn is Managing Director of Class VI Family Office",
            "subject_value": "Matt Blackburn",
            "status": "confirmed",
            "source_url": _CLASS_VI_URL,
            "source_class": _RESEARCHER_SOURCE_CLASS,
            "confidence": "high",
            "produced_by": "research",
        },
        {
            "question_id": "G2.Q3",
            "answer": "Matt Blackburn's current title is Managing Director",
            "subject_value": "Managing Director",
            "status": "confirmed",
            "source_url": _CLASS_VI_URL,
            "source_class": _RESEARCHER_SOURCE_CLASS,
            "confidence": "high",
            "produced_by": "research",
        },
    ])


def _row_for(conn, entity_id):
    sources = []
    from app.db import get_entity_sources
    sources = get_entity_sources(conn, entity_id)
    layer1 = [_claim_from_row(r) for r in get_claims(conn, entity_id)]
    minus1 = wave_minus_1(sources, "CLASS VI FAMILY OFFICE, LLC", [], entity_id=entity_id,
                          layer1_claims=layer1)
    upsert_claims(conn, entity_id, [c.model_dump(mode="json") for c in minus1])
    candidates = gather_survivors(conn, [(entity_id, "ship")])
    assert candidates, "expected a survivor candidate"
    _, rows = _records_rows(candidates)
    assert rows, "expected at least one record row"
    return rows[0]


def test_researcher_findings_reach_the_row(db_path):
    """The end-to-end fix: a confirmed G2.Q1 with subject_value="Matt Blackburn"
    projects into principal_name on the row, with the researcher's source class
    preserved (NOT "derived"); G2.Q3 projects into principal_title; the adv_name
    RAUM trusts through to aum_usd with aum_basis="adv_raum"."""
    with connection(db_path) as conn:
        _seed_classvi(conn)
        row = _row_for(conn, "classvi")

    assert row["principal_name"] == "Matt Blackburn"
    assert row["principal_title"] == "Managing Director"
    # The researcher's citation survives the projection — the row stays auditable
    # back to the page the fact came from, instead of becoming a bare "derived" cell.
    assert row["principal_name_source_class"] == _RESEARCHER_SOURCE_CLASS
    assert row["principal_name_source_class"] != "derived"
    assert row["aum_usd"] == 1498011942.0
    assert row["aum_basis"] == "adv_raum"


def test_bare_yes_answer_projects_nothing(db_path):
    """Regression guard for the exact defect: a G2.Q1 claim that stored the bare
    literal "Yes" (and carries no subject_value) must yield NO principal_name —
    the projection degrades correctly on pre-T19.2 claims, so nothing breaks on
    the already-processed ADV leads until they are re-run."""
    with connection(db_path) as conn:
        upsert_entity(conn, "mb", "MB FAMILY ADVISORS")
        add_entity_source(
            conn, "mb", "adv_name",
            {"signals": {"raum_usd": 204073110, "website": None, "hnw_clients": 40}},
            url="https://adviserinfo.sec.gov/mb",
        )
        upsert_claims(conn, "mb", [
            {
                "question_id": "G2.Q1",
                "answer": "Yes",
                "status": "confirmed",
                "source_url": "https://mbfamilyadvisors.com/team",
                "source_class": "web_page",
                "confidence": "high",
                "produced_by": "research",
            },
        ])
        row = _row_for(conn, "mb")

    # No subject_value -> nothing to project -> principal_name stays blank, exactly
    # the pre-T19.3 behaviour. This is the regression guard: the fix does not regress
    # on the historical "Yes" answers the three ADV leads already carry.
    assert row.get("principal_name") is None
    assert row.get("principal_name_status") == "could_not_verify"


def test_projection_preserves_claim_age():
    """The projection must carry the original claim's retrieved_at, not drop it —
    `check_v5_staleness` skips any claim whose retrieved_at is None, so dropping the
    timestamp would silently make a two-year-old researched principal look freshly
    found. Asserting through check_v5_staleness (not just on the field) proves the date
    actually reaches the check that consumes it (PLAN.md T19 round-2 correction)."""
    from datetime import datetime, timedelta, timezone

    from app.enrichment import _project_question_claims
    from app.state import Claim

    old = datetime.now(timezone.utc) - timedelta(days=400)
    layer1 = Claim(
        question_id="G2.Q1",
        answer="Matt Blackburn is Managing Director of Class VI Family Office",
        subject_value="Matt Blackburn",
        status="confirmed",
        source_url="https://classvifamilyoffice.com/team-member/matt-blackburn/",
        source_class="web_page",
        retrieved_at=old,
        confidence="high",
        produced_by="research",
    )
    projected = _project_question_claims([layer1])
    assert len(projected) == 1
    assert projected[0].field_name == "principal_name"
    # The projected claim keeps the original's timestamp (not None), so its age is
    # preserved through the projection.
    assert projected[0].retrieved_at == old

    # And that timestamp actually reaches the staleness check: a 400-day-old confirmed
    # claim (> 180d threshold) yields exactly one V5_staleness finding. Before the fix
    # the projected claim carried retrieved_at=None and this check returned zero.
    findings = check_v5_staleness(projected)
    assert len(findings) == 1
    assert findings[0].check_id == "V5_staleness"
    assert findings[0].field == "principal_name"

    # Control: a projection of a claim with no retrieved_at keeps the old (skip)
    # behaviour — no finding, no crash.
    no_ts = layer1.model_copy(update={"retrieved_at": None})
    projected_none = _project_question_claims([no_ts])
    assert len(projected_none) == 1
    assert projected_none[0].retrieved_at is None
    assert check_v5_staleness(projected_none) == []