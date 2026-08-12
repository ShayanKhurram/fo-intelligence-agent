"""Loads the discovery layer's raw JSONL feed into `entities` + `entity_sources` — the
DB shape the Parser (app/parser.py) actually reads. This layer receives leads "off the
triage queue" per research_layer_plan.md §1; this module is that queue's loading step.

Discovery records are grouped into one entity per **normalized entity_name_raw** (upper-
cased, whitespace-collapsed). This is a deliberate simplification, not real identity
resolution: two different discovery classes naming the same company slightly differently
("Acme Family Office LLC" vs "Acme Family Office") would currently land as two separate
entities. True fuzzy/crosswalk resolution belongs upstream in the discovery layer or a
dedicated resolution pass — flagged here rather than silently pretended away.

discovery_class -> source_class mapping:
  edgar_13f   -> 13f_filing   (matches app/schema.sql's injected-fact convention)
  dol_5500    -> 5500_filing  (matches app/schema.sql's injected-fact convention)
  fec_employer, ppp_loans, and anything else -> passed through under their own
    discovery_class name. compute_injected_facts() (app/parser.py) doesn't have
    extraction logic for these yet, so they land in entity_sources but aren't
    pre-answered — still available for a future Parser enhancement, not lost.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from app.db import add_entity_source, get_entity, upsert_entity


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().upper())


def iter_discovery_records(path: str | Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _entity_id_for(normalized_name: str) -> str:
    import hashlib

    return "disc_" + hashlib.sha1(normalized_name.encode("utf-8")).hexdigest()[:16]


def _holdings_quarter(year: Any, qtr: Any) -> str | None:
    """Convert the discovery feed's FILING quarter into the quarter the holdings are AS OF.

    The feed's `year`/`qtr` are EDGAR's full-text-search bucket — i.e. when the 13F was
    *filed*. A 13F is due within 45 days of quarter end, so a filing bucketed 2026Q3
    reports positions as of **30 June 2026**, the end of 2026Q2.

    Reading the filing quarter as the holdings quarter made every 13F-derived `aum_as_of`
    exactly one quarter too late. That was not a silent error — Layer V's V1 judge caught it
    **57 times**, by far its most common fatal finding ("the page shows a filing date of
    06-30-2026 ... no assets under management for 2026Q3"), and each hit flipped `aum_as_of`
    to `contradicted`. The findings were right and the data was wrong (2026-08-12).

    Q1 wraps to the prior year's Q4 (a filing made in Jan-Mar reports 31 December holdings).
    """
    try:
        year_i, qtr_i = int(year), int(qtr)
    except (TypeError, ValueError):
        return None
    if not 1 <= qtr_i <= 4:
        return None
    if qtr_i == 1:
        return f"{year_i - 1}Q4"
    return f"{year_i}Q{qtr_i - 1}"


def _map_source(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Returns (source_class, payload) for one discovery record."""
    discovery_class = record.get("discovery_class", "unknown")
    signals = record.get("signals", {}) or {}
    raw = record.get("raw_payload", {}) or {}

    if discovery_class == "edgar_13f":
        year = raw.get("year")
        qtr = raw.get("qtr")
        quarter = _holdings_quarter(year, qtr)
        value_usd = raw.get("primary_doc", {}).get("tableValueTotal") or signals.get("aum_13f")
        payload = {
            "quarter": quarter,
            # The filing quarter is kept alongside the holdings quarter so the one-quarter
            # offset below stays auditable rather than looking like a transcription error.
            "filed_in_quarter": f"{year}Q{qtr}" if year and qtr else None,
            "value_usd": int(float(value_usd)) if value_usd not in (None, "") else None,
            "position_count": signals.get("position_count"),
            "discovery_source_id": record.get("discovery_source_id"),
            "state": record.get("state"),
            "address_raw": record.get("address_raw"),
        }
        return "13f_filing", payload

    if discovery_class == "dol_5500":
        plan_year = signals.get("plan_year")
        payload = {
            "plan_year": int(plan_year) if plan_year not in (None, "") else None,
            "participant_count": signals.get("participant_count"),
            "naics": signals.get("naics"),
            "discovery_source_id": record.get("discovery_source_id"),
            "state": record.get("state"),
        }
        return "5500_filing", payload

    # fec_employer, ppp_loans, and anything unrecognized: pass through as-is,
    # preserving signals + people + address so nothing is lost even though the
    # Parser doesn't pre-answer from these yet.
    payload = {
        "signals": signals,
        "people": record.get("people", []),
        "state": record.get("state"),
        "address_raw": record.get("address_raw"),
        "discovery_source_id": record.get("discovery_source_id"),
    }
    return discovery_class, payload


def ingest_discovery_file(conn: sqlite3.Connection, path: str | Path) -> list[str]:
    """Loads every record in the JSONL file into entities + entity_sources. Returns the
    list of entity_ids in first-appearance order (so callers can slice "first N")."""
    order: list[str] = []
    seen_ids: set[str] = set()
    aliases_by_id: dict[str, set[str]] = {}

    for record in iter_discovery_records(path):
        name_raw = (record.get("entity_name_raw") or "").strip()
        if not name_raw:
            continue
        normalized = _normalize_name(name_raw)
        entity_id = _entity_id_for(normalized)

        if entity_id not in seen_ids:
            seen_ids.add(entity_id)
            order.append(entity_id)
            aliases_by_id[entity_id] = set()
        aliases_by_id[entity_id].add(name_raw)

        existing = get_entity(conn, entity_id)
        canonical_name = existing["canonical_name"] if existing else name_raw
        aliases = sorted(a for a in aliases_by_id[entity_id] if a != canonical_name)
        upsert_entity(conn, entity_id, canonical_name, aliases=aliases)

        source_class, payload = _map_source(record)
        add_entity_source(
            conn,
            entity_id,
            source_class,
            payload,
            url=record.get("discovery_url"),
        )

    return order
