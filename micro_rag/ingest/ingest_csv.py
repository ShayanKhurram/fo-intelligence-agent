"""Ingest an externally-curated family-office CSV (data/data_fo.csv) into the same
Postgres+pgvector store the pipeline uses, as a SEPARATE, co-resident source.

These records did NOT pass this project's Layer-1/E/V validation pipeline — they are an
imported dataset. They are marked `source='csv_import'`, `record_confidence=
'imported_unvalidated'`, and each carries its own `verdict` (prospect/reject) as
`source_verdict` and in the record's chunk/provenance text so it is searchable and
citable. The pipeline ingest and this CSV ingest each prune ONLY their own `source`, so
the two never delete each other.

Contact discipline is preserved: principal_phone is stored as a structural column and in
provenance is EXCLUDED, and it never enters an embedded chunk (chunks.py's leak guard is
mirrored here).

Run:  DATABASE_URL=... python micro_rag/ingest/ingest_csv.py [path/to.csv]
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HERE))
from embeddings import embed_texts  # noqa: E402

DEFAULT_CSV = _REPO_ROOT / "data" / "data_fo.csv"
SOURCE_TAG = "csv_import"

# CSV columns exposed as citable facts (grounding Gate 3 reads `provenance`). principal_phone
# is deliberately excluded — it stays a structural column, surfaced in the record detail, and
# is never fed to the answer generator.
_PROVENANCE_FIELDS = [
    "aum_usd", "aum_basis", "aum_as_of", "investing_mandates", "sector_focus",
    "direct_vs_fund", "recent_investments", "why_now_trigger_concentration",
    "why_now_trigger_liquidity", "why_now_trigger_access_window", "principal_name",
    "principal_title", "headcount", "discovery_class", "public_list_overlap", "verdict",
]

_EMPTY_MARKERS = {"", "not found", "none identified", "none", "n/a", "na", "unknown", "not identified"}


def _blank(v: Any) -> bool:
    return v is None or str(v).strip().lower() in _EMPTY_MARKERS


def _clean(v: Any) -> str | None:
    return None if _blank(v) else str(v).strip()


def _to_int(v: Any) -> int | None:
    v = _clean(v)
    if v is None:
        return None
    try:
        return int(float(v.replace(",", "").replace("$", "")))
    except (ValueError, AttributeError):
        return None


def _split_list(v: Any) -> list[str]:
    v = _clean(v)
    if v is None:
        return []
    # split on ';' first (the dataset's primary separator), fall back to the whole string
    parts = [p.strip() for p in v.split(";") if p.strip()]
    return parts or [v]


def parse_csv(path: Path) -> list[dict[str, str]]:
    """The file is double-encoded: every line is itself a single quoted CSV field whose
    inner quotes are doubled. Unwrap each line, then parse the inner CSV."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    decoded = [next(csv.reader([ln]))[0] for ln in lines if ln.strip()]
    reader = csv.DictReader(io.StringIO("\n".join(decoded)))
    return [dict(r) for r in reader]


def build_record(row: dict[str, str]) -> dict[str, Any]:
    verdict = _clean(row.get("verdict")) or "unknown"
    phone = _clean(row.get("principal_phone"))
    return {
        "record_id": _clean(row.get("entity_id")),
        "entity_name": _clean(row.get("entity_name")) or "(unnamed)",
        "entity_type": "type_unconfirmed",  # CSV carries no SFO/MFO distinction; column is NOT NULL
        "hq_state": None,
        "hq_country": None,
        "aum_usd": _to_int(row.get("aum_usd")),
        "aum_basis": _clean(row.get("aum_basis")),
        "aum_as_of": _clean(row.get("aum_as_of")),
        "mandates": _split_list(row.get("investing_mandates")),
        "fit_tags": _split_list(row.get("sector_focus")),
        "check_size_min": None,
        "check_size_max": None,
        "principal_name": _clean(row.get("principal_name")),
        "principal_title": _clean(row.get("principal_title")),
        "principal_email": None,
        "principal_email_status": "could_not_verify",  # CSV has no emails; column is NOT NULL
        "principal_phone": phone,
        "principal_phone_status": "imported" if phone else "could_not_verify",
        "most_recent_signal_date": None,
        "urgency_tier": "triggered" if any(
            not _blank(row.get(k)) for k in
            ("why_now_trigger_concentration", "why_now_trigger_liquidity", "why_now_trigger_access_window")
        ) else None,
        "activity_status": "active" if not _blank(row.get("recent_investments")) else None,
        "actionability_score": None,
        "discovery_class_primary": _clean(row.get("discovery_class")),
        "public_list_overlap": _split_list(row.get("public_list_overlap")),
        "record_confidence": "imported_unvalidated",
        "outcome": verdict,          # prospect / reject — the CSV's own disposition
        "source": SOURCE_TAG,
        "source_verdict": verdict,
        # extra fields for chunk building (not columns):
        "_sector_focus": _clean(row.get("sector_focus")),
        "_direct_vs_fund": _clean(row.get("direct_vs_fund")),
        "_recent_investments": _clean(row.get("recent_investments")),
        "_headcount": _clean(row.get("headcount")),
        "_wn_concentration": _clean(row.get("why_now_trigger_concentration")),
        "_wn_liquidity": _clean(row.get("why_now_trigger_liquidity")),
        "_wn_access": _clean(row.get("why_now_trigger_access_window")),
    }


def _fmt_list(vals: list[str]) -> str:
    return ", ".join(vals) if vals else "not specified"


def build_chunks(r: dict[str, Any]) -> list[dict[str, str]]:
    name = r["entity_name"]
    aum = f"${r['aum_usd']:,}" if r["aum_usd"] else "not disclosed"
    facets: dict[str, str] = {}

    identity = [f"{name} is a family office / advisory entity (imported record, verdict: {r['source_verdict']})."]
    identity.append(f"Assets under management: {aum}"
                    + (f" ({r['aum_basis']}, as of {r['aum_as_of']})." if r["aum_basis"] else "."))
    if r["_headcount"]:
        identity.append(f"Headcount: {r['_headcount']}.")
    if r["discovery_class_primary"]:
        identity.append(f"Discovery sources: {r['discovery_class_primary']}.")
    if r["public_list_overlap"]:
        identity.append(f"Public-list overlap: {_fmt_list(r['public_list_overlap'])}.")
    facets["identity"] = " ".join(identity)

    mandate = [f"{name}'s investing mandate and focus."]
    if r["mandates"]:
        mandate.append(f"Mandates: {_fmt_list(r['mandates'])}.")
    if r["_sector_focus"]:
        mandate.append(f"Sector focus: {r['_sector_focus']}.")
    if r["_direct_vs_fund"]:
        mandate.append(f"Instruments: {r['_direct_vs_fund']}.")
    if len(mandate) == 1:
        mandate.append("No mandate details in the record.")
    facets["mandate"] = " ".join(mandate)

    if r["principal_name"]:
        people = [f"{r['principal_name']} is the named contact at {name}."]
        if r["principal_title"]:
            people.append(f"Title: {r['principal_title']}.")
        facets["people"] = " ".join(people)
    else:
        facets["people"] = f"No named decision-maker in the record for {name}."

    if r["_recent_investments"]:
        facets["activity"] = f"{name} recent activity: {r['_recent_investments']}"
    else:
        facets["activity"] = f"No recent activity recorded for {name}."

    wn = []
    if r["_wn_concentration"]:
        wn.append(f"Concentration trigger: {r['_wn_concentration']}.")
    if r["_wn_liquidity"]:
        wn.append(f"Liquidity trigger: {r['_wn_liquidity']}.")
    if r["_wn_access"]:
        wn.append(f"Access-window trigger: {r['_wn_access']}.")
    facets["why_now"] = (f"{name} why-now triggers. " + " ".join(wn)) if wn else \
        f"No specific why-now trigger recorded for {name}."

    facets["summary"] = (
        f"{name} — imported {r['source_verdict']} record. AUM {aum}. "
        f"Principal: {r['principal_name'] or 'not named'}. "
        f"Mandates: {_fmt_list(r['mandates'])}. Confidence: {r['record_confidence']}."
    )

    # contact-leak guard (mirror chunks.py): phone must never appear in an embedded chunk
    chunks = []
    for facet, content in facets.items():
        if r.get("principal_phone") and str(r["principal_phone"]) in content:
            raise ValueError(f"principal_phone leaked into {facet} chunk for {r['record_id']}")
        chunks.append({"chunk_id": f"{r['record_id']}::{facet}", "facet": facet, "content": content})
    return chunks


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT, timeout=5).decode().strip()
    except Exception:
        return "unknown"


_RECORD_COLS = [
    "record_id", "build_hash", "entity_name", "entity_type", "hq_state", "hq_country", "aum_usd",
    "aum_basis", "aum_as_of", "mandates", "fit_tags", "check_size_min", "check_size_max",
    "principal_name", "principal_title", "principal_email", "principal_email_status",
    "principal_phone", "principal_phone_status", "most_recent_signal_date", "urgency_tier",
    "activity_status", "actionability_score", "discovery_class_primary", "public_list_overlap",
    "record_confidence", "outcome", "source", "source_verdict",
]


def write_to_postgres(dsn: str, records: list[dict[str, Any]]) -> str:
    build_hash = f"csv-{_git_sha()}-{len(records)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute((_HERE / "schema.sql").read_text(encoding="utf-8"))
            # additive columns for co-resident sources
            cur.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'pipeline'")
            cur.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS source_verdict TEXT")

            # Pipeline records are authoritative for any overlapping entity_id — an entity
            # this project's pipeline actually validated (with per-field provenance) must not
            # be silently overwritten by a flat CSV row, especially when the two DISAGREE
            # (e.g. 1015 Capital / 21 West: pipeline shipped them, the CSV verdict is reject).
            # Skip those record_ids here; they keep their pipeline representation.
            cur.execute("SELECT record_id FROM records WHERE COALESCE(source,'pipeline') = 'pipeline'")
            pipeline_ids = {row[0] for row in cur.fetchall()}
            skipped = [r for r in records if r["record_id"] in pipeline_ids]
            records = [r for r in records if r["record_id"] not in pipeline_ids]
            if skipped:
                print(f"Skipped {len(skipped)} CSV row(s) that overlap authoritative pipeline records: "
                      + ", ".join(r["entity_name"] for r in skipped))

            cur.execute(
                "INSERT INTO dataset_meta (build_hash, record_count, class_concentration) VALUES (%s,%s,%s)",
                (build_hash, len(records), "{}"),
            )

            placeholders = ",".join(["%s"] * len(_RECORD_COLS))
            updates = ",".join(f"{c}=EXCLUDED.{c}" for c in _RECORD_COLS if c != "record_id")
            insert_sql = (
                f"INSERT INTO records ({','.join(_RECORD_COLS)}) VALUES ({placeholders}) "
                f"ON CONFLICT (record_id) DO UPDATE SET {updates}, updated_at = now()"
            )
            for r in records:
                r = {**r, "build_hash": build_hash}
                cur.execute(insert_sql, [r.get(c) for c in _RECORD_COLS])

                cur.execute("DELETE FROM chunks WHERE record_id = %s", (r["record_id"],))
                chunks = build_chunks(r)
                vectors = embed_texts([c["content"] for c in chunks])
                for ch, vec in zip(chunks, vectors):
                    vec_literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                    cur.execute(
                        "INSERT INTO chunks (chunk_id, record_id, facet, content, embedding, token_count) VALUES (%s,%s,%s,%s,%s,%s)",
                        (ch["chunk_id"], r["record_id"], ch["facet"], ch["content"], vec_literal, len(ch["content"].split())),
                    )

                cur.execute("DELETE FROM provenance WHERE record_id = %s", (r["record_id"],))
                for field in _PROVENANCE_FIELDS:
                    # map the record/extra keys back for provenance values
                    val = {
                        "investing_mandates": _fmt_list(r["mandates"]),
                        "sector_focus": r["_sector_focus"],
                        "direct_vs_fund": r["_direct_vs_fund"],
                        "recent_investments": r["_recent_investments"],
                        "why_now_trigger_concentration": r["_wn_concentration"],
                        "why_now_trigger_liquidity": r["_wn_liquidity"],
                        "why_now_trigger_access_window": r["_wn_access"],
                        "headcount": r["_headcount"],
                        "discovery_class": r["discovery_class_primary"],
                        "public_list_overlap": _fmt_list(r["public_list_overlap"]) if r["public_list_overlap"] else None,
                        "verdict": r["source_verdict"],
                    }.get(field, r.get(field))
                    if val in (None, "", []):
                        continue
                    cur.execute(
                        """INSERT INTO provenance (record_id, field_name, value, source_class, status, confidence)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (r["record_id"], field, str(val), SOURCE_TAG, "imported", None),
                    )

            # prune ONLY this source's stale rows (never touch pipeline records)
            cur.execute("DELETE FROM records WHERE source = %s AND build_hash <> %s", (SOURCE_TAG, build_hash))
            pruned = cur.rowcount
            if pruned:
                print(f"Pruned {pruned} stale csv_import record(s)")
            conn.commit()
        return build_hash
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL/POSTGRES_URL not set")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    rows = parse_csv(path)
    records = [build_record(r) for r in rows if _clean(r.get("entity_id"))]
    verdicts: dict[str, int] = {}
    for r in records:
        verdicts[r["source_verdict"]] = verdicts.get(r["source_verdict"], 0) + 1
    print(f"Parsed {len(records)} CSV records from {path.name} (verdicts: {verdicts})")
    build_hash = write_to_postgres(dsn, records)
    print(f"Ingested build_hash={build_hash}, {len(records)} records, {len(records) * 6} chunks (source={SOURCE_TAG})")


if __name__ == "__main__":
    main()
