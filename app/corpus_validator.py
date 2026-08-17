"""T43 — Corpus validator: re-classify `type_unconfirmed` rows and backfill missing
principal emails across the already-shipped corpus.

Why this module exists, and the one rule it must never violate
--------------------------------------------------------------
Supabase `records` is a **projection** of the SQLite store at `data/foia.db`, not a
source of truth. `micro_rag/ingest/ingest.py` upserts with `ON CONFLICT (record_id) DO
UPDATE SET ...`, and `micro_rag/ingest/build_records.py:_type_final` recomputes
`entity_type` from the G1.Q4 claim. **Any direct UPDATE against Postgres is reverted on
the next ingest.** So this module:

  * READS Postgres (Supabase) only to *select* which rows need work — never writes to it.
  * WRITES verdicts to SQLite (claims + field_status), then a later round re-projects
    the touched record_ids through the normal ingest path. That is the only durable path.

Round 1 implemented the worklist + checkpoint plumbing and the dry-run CLI. Round 2
(this state) implements the email backfill (T43.3), the durable write + re-project
path (T43.4), and the non-dry-run half of the CLI (T43.5). `classify_entity_type`
(T43.2) is still a stub; it lands in round 3.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import psycopg2
from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_model
from app.tools.cache import cached_call

from app.db import (
    connection as sqlite_connection,
    get_claims,
    get_entity,
    get_validator_checkpoint,
    upsert_claim,
    upsert_validator_checkpoint,
    write_audit_rejected_value,
    write_field_status,
)
from app.enrichment import (
    _email_matches_domain,
    _EMAIL_RE,
    _find_email_on_site,
    _find_email_via_snov,
    _is_role_address,
    _ROLE_LOCAL_PARTS,
    resolve_domain,
)
from app.tools.adv import adv_firm_search_raw
from app.tools.freefetch import fetch_raw_html, free_fetch_raw
from app.tools.serper import serper_search_raw
from app.rag_sync import is_confirmed

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
Mode = Literal["type", "email"]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WorkItem:
    """One row from Supabase `records` selected for validation, joined to its SQLite
    entity for `aliases`. Carries everything a verdict pass needs to decide and write."""

    record_id: str
    entity_name: str
    principal_name: str | None
    aliases: list[str]
    entity_type: str
    principal_email: str | None


@dataclass
class TypeVerdict:
    """T43.2 result. `verdict` is 'SFO' | 'MFO' | 'type_unconfirmed'. A non-unconfirmed
    verdict REQUIRES a verbatim `snippet` that is a literal substring of `source_text`
    (checked in code, not by prompt) plus a `source_url`. `type_unconfirmed` is a
    legitimate outcome, never forced away from."""

    verdict: Literal["SFO", "MFO", "type_unconfirmed"]
    source_url: str | None = None
    snippet: str | None = None
    confidence: str = "low"  # low | medium | high
    basis: str = ""
    source_text: str | None = None  # the text the snippet was quoted from (for the assertion)


@dataclass
class EmailResult:
    """T43.3 result. `field` is 'principal_email' | 'firm_email' | None (None = dropped,
    e.g. an off-domain hit per the 2026-08-13 incident rule). Hard invariants enforced by
    the backfill, not by prompt: a row with no principal_name can only yield firm_email;
    a role address is always firm_email; an off-domain address is dropped entirely."""

    field: Literal["principal_email", "firm_email"] | None = None
    email: str | None = None
    source_url: str | None = None
    source_class: str | None = None
    extraction_method: str | None = None
    status: str = "could_not_verify"
    confidence: str = "low"
    reason: str = ""


# ---------------------------------------------------------------------------
# Worklist (T43.1)
# ---------------------------------------------------------------------------


def _pg_dsn() -> str:
    """Same env precedence as micro_rag/ingest/ingest.py:213."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        raise SystemExit(
            "DATABASE_URL/POSTGRES_URL not set — corpus validator reads Supabase "
            "read-only for selection"
        )
    return dsn


def _row_to_workitem(row: dict[str, Any], aliases_by_id: dict[str, list[str]]) -> WorkItem:
    return WorkItem(
        record_id=row["record_id"],
        entity_name=row["entity_name"],
        principal_name=row.get("principal_name"),
        aliases=list(aliases_by_id.get(row["record_id"], [])),
        entity_type=row.get("entity_type") or "type_unconfirmed",
        principal_email=row.get("principal_email"),
    )


def _load_skipped_ids(sqlite_conn: sqlite3.Connection, mode: Mode) -> set[str]:
    """record_ids to exclude for this mode: already done, or poisoned (attempts >= 3).
    The key is (record_id, mode), so a 'done' on the type pass does not suppress the
    email pass on the same record.

    Tolerates a missing `validator_checkpoints` table (treats it as "no checkpoints")
    so a fresh DB that has not yet run init_db() does not crash the read-only worklist
    path — the table is created by a migration, and a read should not require it to
    exist."""
    try:
        rows = sqlite_conn.execute(
            """
            SELECT record_id FROM validator_checkpoints
            WHERE mode = ? AND (status = 'done' OR attempts >= ?)
            """,
            (mode, MAX_ATTEMPTS),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise
    return {r["record_id"] for r in rows}


def _load_aliases(sqlite_conn: sqlite3.Connection, record_ids: list[str]) -> dict[str, list[str]]:
    if not record_ids:
        return {}
    placeholders = ",".join("?" for _ in record_ids)
    rows = sqlite_conn.execute(
        f"SELECT entity_id, aliases FROM entities WHERE entity_id IN ({placeholders})",
        record_ids,
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        try:
            out[r["entity_id"]] = json.loads(r["aliases"]) if r["aliases"] else []
        except (json.JSONDecodeError, TypeError):
            out[r["entity_id"]] = []
    return out


def build_worklist(mode: Mode, limit: int | None = None, db_path: str | None = None) -> list[WorkItem]:
    """Select rows from Supabase `records` needing validation for `mode`, ordered by
    actionability descending, minus checkpointed/poisoned rows.

    SELECT ONLY — this function never issues INSERT/UPDATE/DELETE against Postgres. The
    limit is applied AFTER checkpoint filtering, so `limit=5` yields 5 *processable*
    rows, not 5 rows of which some are skipped.

    Ordering (a verdict on an unreachable row is worth nothing, so a capped batch spends
    its budget on rows that can actually convert):
      * mode='type'  : rows that already have a principal_email come FIRST
      * mode='email' : rows that already have a principal_name come FIRST
    then by record_id for a stable, reproducible order.
    """
    if mode not in ("type", "email"):
        raise ValueError(f"mode must be 'type' or 'email', got {mode!r}")

    # Read Supabase read-only for selection. No write statements anywhere in this path.
    conn = psycopg2.connect(_pg_dsn())
    try:
        with conn.cursor() as cur:
            if mode == "type":
                where = "entity_type = 'type_unconfirmed'"
                order = "(principal_email IS NOT NULL AND btrim(principal_email) <> '') DESC, record_id ASC"
            else:
                # Any row still missing a principal_email is eligible, EVEN IF it already
                # has a firm_email. An earlier version also required `firm_email IS NULL`
                # to avoid re-scraping the T43.7 demotions — but that silently made a
                # firm_email found today *block* the principal's real address from ever
                # being looked up tomorrow. With Snov down, the site tier returns firm
                # inboxes; with Snov up, a name-targeted lookup on those same rows can
                # find the actual principal. Locking them out to save a scrape trades the
                # valuable answer for the cheap one. De-duplication is the checkpoint's
                # job (see `_load_skipped_ids`), not the predicate's.
                where = "principal_email IS NULL OR btrim(principal_email) = ''"
                order = "(principal_name IS NOT NULL AND btrim(principal_name) <> '') DESC, record_id ASC"
            # We do not push LIMIT to Postgres: the limit counts *processable* rows, and
            # checkpoint filtering happens in SQLite. Fetch ordered candidates, filter in
            # Python, slice to limit. The unconfirmed / email-less sets are small enough
            # that fetching them whole is fine and keeps the limit semantics honest.
            cur.execute(
                f"""
                SELECT record_id, entity_name, principal_name, entity_type, principal_email
                FROM records
                WHERE {where}
                ORDER BY {order}
                """
            )
            cols = [d[0] for d in cur.description]
            pg_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    # Filter by SQLite checkpoints and attach aliases from the SQLite entity.
    with sqlite_connection(db_path) as sconn:
        skipped = _load_skipped_ids(sconn, mode)
        aliases_by_id = _load_aliases(sconn, [r["record_id"] for r in pg_rows])

    work: list[WorkItem] = []
    for row in pg_rows:
        if row["record_id"] in skipped:
            continue
        work.append(_row_to_workitem(row, aliases_by_id))
        if limit is not None and len(work) >= limit:
            break
    return work


# ---------------------------------------------------------------------------
# T43.6 — LLM email attribution ("is this the principal's address, or the firm's?")
# ---------------------------------------------------------------------------

# Why an LLM and not a word list: the shipped list (`enrichment.py:_ROLE_LOCAL_PARTS`)
# is 22 exact-match words, and a live audit on 2026-08-16 found it missing
# `compliance@`, `marketing@`, `clientservices@`, `investments@`, `connect@`, `retire@`
# — and defeated outright by a single leading underscore (`info@` caught, `_info@` not).
# A list also cannot answer the question that actually matters: `mark@comptonwealth.com`
# is a perfectly personal address, just not C. Todd Compton's. And the inverse — the list
# would wrongly flag `tripp@shaddayco.com` for a principal named Jay Shadday III, where
# "Tripp" is a common nickname for a third. Deciding "is this local part the *named
# principal*, some other human, or a company inbox" is a language judgement.

_ATTRIBUTION_SYSTEM = """You classify a single email address for a lead database.

Given a firm name, the name of that firm's named principal (may be absent), and an email
address, answer which ONE of these the address is:

  principal    - the personal work address of THAT named principal. Nicknames and
                 initials count (Bob/Robert, Chuck/Charles, Tripp for a third,
                 j.smith for John Smith, jsmith, smithj).
  other_person - a real person's personal address, but NOT the named principal.
  role         - a shared/company inbox rather than any individual: info, contact,
                 admin, compliance, marketing, clientservices, investments, connect,
                 retire, enquiries, the firm's own name as the local part, and so on.
                 Punctuation or digits around a role word do not make it personal
                 (_info, admin2, adminvfo are all role).
  unknown      - genuinely cannot tell.

Answer with ONLY a single lowercase word: principal, other_person, role, or unknown.
No punctuation, no explanation."""

_VALID_ATTRIBUTIONS = {"principal", "other_person", "role", "unknown"}


def _deterministic_attribution(email: str, principal_name: str | None) -> str:
    """The offline fallback, used only when the LLM is unavailable or unparseable.

    Deliberately conservative: it never returns 'principal' unless a token of the
    principal's name actually appears in the local part. An LLM outage must not be able
    to promote an address into `principal_email` — the project's standing rule is that an
    outage must never look like a confirmation."""
    local = email.split("@", 1)[0].split("+", 1)[0].lower()
    if _is_role_address(email):
        return "role"
    # Normalised role check — this is the `_info` / `adminvfo` hole in the shipped list.
    squashed = re.sub(r"[^a-z]", "", local)
    if squashed in _ROLE_LOCAL_PARTS or any(
        squashed.startswith(r) or squashed.endswith(r) for r in _ROLE_LOCAL_PARTS
    ):
        return "role"
    if _blank(principal_name):
        return "unknown"
    name_tokens = {t for t in re.split(r"[^a-z]+", (principal_name or "").lower()) if len(t) > 2}
    if any(t in local for t in name_tokens):
        return "principal"
    return "unknown"


async def classify_email_attribution(
    email: str, entity_name: str, principal_name: str | None
) -> tuple[str, str]:
    """Decide whether `email` is the named principal's address, another person's, a role
    inbox, or undecidable. Returns (attribution, basis).

    Cached on (email, principal_name) through the existing tool cache, so re-running the
    audit over the same corpus costs nothing.

    On any LLM failure the deterministic fallback runs instead — it can only return
    'principal' when a name token is literally present, so an outage degrades toward
    firm_email, never toward a false principal attribution."""
    if not email or "@" not in email:
        return "unknown", "no_address"

    async def _ask() -> dict[str, Any]:
        model = get_model("cheapest")
        prompt = (
            f"Firm: {entity_name}\n"
            f"Named principal: {principal_name or '(none on file)'}\n"
            f"Email: {email}"
        )
        reply = await model.ainvoke(
            [SystemMessage(content=_ATTRIBUTION_SYSTEM), HumanMessage(content=prompt)]
        )
        text = (getattr(reply, "content", "") or "")
        if isinstance(text, list):  # some providers return content blocks
            text = " ".join(str(b.get("text", "")) for b in text if isinstance(b, dict))
        return {"answer": str(text).strip().lower()}

    try:
        payload = await cached_call(
            "email_attribution", _ask, email=email, principal_name=principal_name or ""
        )
        answer = (payload or {}).get("answer", "")
        word = re.sub(r"[^a-z_]", "", answer.split()[0]) if answer.split() else ""
        if word in _VALID_ATTRIBUTIONS:
            return word, "llm"
        logger.warning("email attribution: unparseable LLM reply %r for %s", answer, email)
    except Exception:  # noqa: BLE001 — an LLM outage falls back, it does not fail the row
        logger.warning("email attribution: LLM unavailable for %s", email, exc_info=True)

    return _deterministic_attribution(email, principal_name), "deterministic_fallback"


def field_for_attribution(attribution: str) -> str:
    """Map an attribution to the column the address may occupy.

    Only a positive 'principal' earns `principal_email`. 'other_person' lands in
    `firm_email` because the schema has no third column: it is a genuine reachable
    channel at the firm, and calling it the principal's address would be the specific
    lie this validator exists to prevent."""
    return "principal_email" if attribution == "principal" else "firm_email"


# ---------------------------------------------------------------------------
# T43.8 — principal-targeted site tier (no Snov required)
# ---------------------------------------------------------------------------

# `enrichment._find_email_on_site` takes the FIRST @domain address on the page, which is
# why it keeps returning switchboards: `info@` is usually printed above the team list.
# This tier harvests EVERY address on the firm's own pages and then asks which one is the
# principal's — the question the first-match scan never asks. It needs no Snov credit,
# which matters because Snov's name-targeted lookup is the only other route to a
# principal's address and it is the tier that keeps running dry.

_SITE_PATHS = (
    "/team", "/about", "/contact", "/about-us", "/leadership",
    "/our-team", "/people", "/who-we-are", "/contact-us", "/team.html",
)


_PAGE_TIMEOUT_S = 25.0


async def _harvest_site_emails(domain: str) -> list[tuple[str, str]]:
    """Every distinct on-domain address across the firm's common pages, with its URL.

    The pages are fetched CONCURRENTLY. Fetching them in sequence cost ~3.5 min/row
    (10 paths x ~20s of browser-rendered fetch), which measured out at ~22 hours for a
    440-row backfill — the fetches are independent and network-bound, so the whole set
    costs about as much as its slowest member. Each page also carries its own timeout, so
    one unresponsive path cannot dominate the row.
    """
    async def _one(path: str) -> tuple[str, list[str]]:
        url = f"https://{domain}{path}"
        try:
            # `fetch_raw_html` (httpx only), NOT `fetch_page_free_first`. Two reasons:
            #   1. Speed. `fetch_page_free_first` escalates to crawl4ai, which serialises
            #      on a shared browser (`_CRAWL_SEMAPHORE` + `_crawler_lock`), so gathering
            #      10 of them queues behind one renderer and blows the row timeout — this
            #      is what made the first run ~3.5 min/row.
            #   2. Recall. Raw HTML keeps `mailto:` hrefs, which trafilatura's text
            #      extraction strips out — and a contact page's only address is very often
            #      in exactly that href.
            html = await asyncio.wait_for(fetch_raw_html(url), timeout=_PAGE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — timeout or fetch error: this path yields nothing
            return url, []
        return url, [m.group(0).strip(".,;:") for m in _EMAIL_RE.finditer(html or "")]

    results = await asyncio.gather(*(_one(p) for p in _SITE_PATHS), return_exceptions=True)

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in results:
        if isinstance(item, BaseException):
            continue
        url, emails = item
        for email in emails:
            key = email.lower()
            if key in seen or not _email_matches_domain(email, domain):
                continue
            seen.add(key)
            found.append((email, url))
    return found


def _name_tokens(principal_name: str | None) -> tuple[str, str]:
    """(first, last) from a principal name, tolerating "SURNAME, First" and suffixes."""
    raw = (principal_name or "").strip()
    if "," in raw:  # "TAFT, THOMAS SR" — ADV renders surname first
        last_part, _, first_part = raw.partition(",")
        toks = [t for t in re.split(r"[^A-Za-z]+", f"{first_part} {last_part}") if t]
    else:
        toks = [t for t in re.split(r"[^A-Za-z]+", raw) if t]
    drop = {"jr", "sr", "ii", "iii", "iv", "mr", "ms", "mrs", "dr"}
    toks = [t for t in toks if t.lower() not in drop and len(t) > 1]
    if not toks:
        return "", ""
    return toks[0].lower(), toks[-1].lower()


def _matches_principal(local: str, first: str, last: str) -> bool:
    """Is this local part plausibly THIS person? Requires the surname, or the full first
    name on a single-token local — deliberately stricter than 'shares any token', which is
    what let `ali.zamani@` be accepted for principal Reza Zamani."""
    l = re.sub(r"[^a-z]", "", local.lower())
    if not l:
        return False
    if last and len(last) > 2 and last in l:
        return True
    return bool(first and len(first) > 2 and l == first)


def _infer_from_pattern(
    harvested: list[tuple[str, str]], first: str, last: str, domain: str
) -> tuple[str, str] | None:
    """Derive the firm's address convention from a colleague's address and apply it to the
    principal. Returns (email, evidence_url).

    This is an inference, not an observation, and is written with status
    `pattern_inferred` so it can never be mistaken for a confirmed address. It is grounded
    though: the shape comes from a real address on the firm's own domain, not from a
    guessed list of conventions.
    """
    if not (first and last):
        return None
    peers = [
        (e, u) for e, u in harvested
        if not _is_role_address(e) and not _matches_principal(e.split("@", 1)[0], first, last)
    ]
    if not peers:
        return None
    # Decide from ALL peer addresses at once, not the first one. A per-address regex reads
    # `shannon@` (7 chars) as initial+surname and produces `tcompton@` for Todd Compton,
    # when six peers named shannon/amy/mark/justin/nancy make `{first}@` obvious. The
    # convention is a property of the set.
    locals_ = [e.split("@", 1)[0] for e, _ in peers]
    dotted = [l for l in locals_ if "." in l]
    url = peers[0][1]
    if len(dotted) >= max(1, len(locals_) // 2):
        lead = dotted[0].split(".")[0]
        return (f"{first[0]}.{last}@{domain}" if len(lead) == 1
                else f"{first}.{last}@{domain}"), url
    # No dots: either every local is a bare given name, or initial+surname. If most peer
    # locals are short-ish single words, it is the given-name convention.
    singles = [re.sub(r"[^a-z]", "", l.lower()) for l in locals_]
    singles = [s for s in singles if s]
    if not singles:
        return None
    if sum(1 for s in singles if len(s) <= 8) >= max(1, len(singles) // 2):
        return f"{first}@{domain}", url
    return f"{first[0]}{last}@{domain}", url


async def _apply_attribution_and_build(
    item: "WorkItem", principal_name: str | None, email: str, url: str, domain: str,
    *, status: str, method: str, snov_claim: Any = None,
) -> "EmailResult":
    """Run an address through the same gates the main path uses and build the result:
    off-domain is dropped, the LLM picks the column, and a row with no principal named can
    only ever yield firm_email."""
    if not _email_matches_domain(email, domain):
        return EmailResult(field=None, email=None, reason="off_domain", source_url=url)
    attribution, basis = await classify_email_attribution(email, item.entity_name, principal_name)
    field = field_for_attribution(attribution)
    if not principal_name:
        field = "firm_email"
    return EmailResult(
        field=field, email=email, source_url=url, source_class="site_scrape",
        extraction_method=method, status=status,
        confidence="low", reason=f"attribution_{attribution}",
    )


async def find_principal_email_on_site(
    domain: str, principal_name: str | None,
    harvested: list[tuple[str, str]] | None = None,
) -> tuple[str, str, str, str] | None:
    """(email, status, source_url, extraction_method) for the principal, or None.

    Two tiers, strongest first:
      1. an address on the firm's own site whose local part is THIS principal -> observed.
      2. the firm's address convention, learned from a colleague's on-domain address and
         applied to the principal -> `pattern_inferred`.
    """
    first, last = _name_tokens(principal_name)
    if not (first or last):
        return None
    # Accept an already-fetched harvest so the caller can pay for the site read once and
    # serve every tier from it.
    if harvested is None:
        harvested = await _harvest_site_emails(domain)
    if not harvested:
        return None

    for email, url in harvested:
        if _matches_principal(email.split("@", 1)[0], first, last):
            return email, "single_source", url, "site_scrape_named"

    inferred = _infer_from_pattern(harvested, first, last, domain)
    if inferred:
        email, url = inferred
        return email, "pattern_inferred", url, "site_pattern_inference"
    return None


# ---------------------------------------------------------------------------
# T43.3 — backfill_principal_email (reuse the enrichment tier chain)
# ---------------------------------------------------------------------------


def _blank(s: str | None) -> bool:
    return not s or not s.strip()


async def backfill_principal_email(item: WorkItem) -> EmailResult:
    """Backfill a missing principal_email / firm_email for one shipped record.

    Reuses the existing app/enrichment.py tier chain — resolve_domain, then
    _find_email_via_snov / _find_email_on_site in the SAME order wave_1 uses
    (Snov first when a principal is named; site first when not). Writes NO new
    email-classification logic: the guard already exists, and a second copy would drift.

    Then applies three hard invariants as an INDEPENDENT second gate (enrichment.py
    already enforces them, but this gate is tested separately and must hold on its own):
      1. no principal_name  ->  field may ONLY be 'firm_email'.
      2. _is_role_address   ->  field is 'firm_email'. Always.
      3. _email_matches_domain False ->  DROP entirely (field=None, reason='off_domain').
    An invariant that fires overrrides whatever the tier chain returned, with a warning.
    """
    domain = await resolve_domain(item.entity_name)
    if not domain:
        return EmailResult(field=None, reason="no_domain")

    principal_name = None if _blank(item.principal_name) else item.principal_name

    # T43.8 tier 0 — the principal-targeted site scan, BEFORE Snov. It is free, it needs
    # no credit, and it answers the actual question ("which of these addresses is this
    # person's?") rather than "what is the first address on the page". Running it ahead of
    # Snov means a funded Snov credit is only spent on rows the firm's own site could not
    # answer, which is what makes the backfill survive Snov running dry.
    # Harvest the site ONCE and serve every tier from that one pass. `enrichment.
    # _find_email_on_site` used to be the fallback here, but it re-fetches the same pages
    # through the headless browser, which serialises on a single shared renderer
    # (`crawl.py:_CRAWL_SEMAPHORE` + `_crawler_lock`) at ~20s a page. Measured live: that
    # one call was pushing every row into the 120s row timeout, ~1 row per 2 minutes.
    # This harvest already covers a superset of its paths and reads raw HTML (so it also
    # sees `mailto:` hrefs the old text-extraction path dropped), so the browser round is
    # redundant work, not extra coverage.
    harvested = await _harvest_site_emails(domain)

    if principal_name and harvested:
        hit = await find_principal_email_on_site(domain, principal_name, harvested=harvested)
        if hit:
            email, status, url, method = hit
            attribution, _basis = await classify_email_attribution(
                email, item.entity_name, principal_name
            )
            # The classifier still gates it: an inferred address that reads as a role
            # inbox is not promoted just because we constructed it.
            if attribution == "principal":
                return EmailResult(
                    field="principal_email", email=email, source_url=url,
                    source_class="site_scrape", extraction_method=method,
                    status=status,
                    confidence="medium" if status == "single_source" else "low",
                    reason=f"attribution_principal_{method}",
                )

    # Snov's name-targeted lookup is the only remaining route to a *principal's* address
    # once the firm's own site has been read. It is cheap to attempt while out of credits
    # (an immediate 402), and it is the tier that pays off the moment credits return.
    # Called even with no principal named — Snov then runs a domain-wide search, whose
    # results can only ever become firm_email (invariant 1), but a firm channel is still
    # a channel and dropping the call would lose it.
    claim = await _find_email_via_snov(domain, principal_name)

    # Nothing from Snov: fall back to any address the single harvest already produced,
    # rather than fetching the site a second time.
    if (claim is None or claim.status == "could_not_verify") and harvested:
        email, url = harvested[0]
        return await _apply_attribution_and_build(
            item, principal_name, email, url, domain,
            status="single_source", method="site_scrape_harvest",
            snov_claim=claim,
        )

    if claim is None:
        return EmailResult(field=None, reason="not_found")

    email = claim.answer
    # A could_not_verify / error claim carries no usable address. Distinguish an OUTAGE
    # from an ABSENCE: `source_class == "tool_unavailable"` means the lookup never ran
    # (Snov returned 402 Payment Required when its credits ran out, 2026-08-16). Reporting
    # that as "not_found" would state that the firm publishes no address, which is a claim
    # about the world we did not make — the same "an outage must never look like an
    # absence" rule the enrichment layer follows.
    if not email or claim.status in ("could_not_verify", "removed_failed_validation", "superseded"):
        unavailable = (claim.source_class or "") == "tool_unavailable"
        return EmailResult(
            field=None,
            reason="tool_unavailable" if unavailable else "not_found",
            source_url=claim.source_url,
            source_class=claim.source_class,
            extraction_method=claim.extraction_method,
        )

    # --- Invariant 3: off-domain is dropped entirely (2026-08-13 incident rule). ---
    if not _email_matches_domain(email, domain):
        logger.warning(
            "corpus_validator: off-domain address dropped for %s — %s does not match %s",
            item.record_id, email, domain,
        )
        return EmailResult(field=None, email=None, reason="off_domain",
                           source_url=claim.source_url)

    # --- Invariant 2: attribution decides the column (T43.6). ---
    # The LLM classifier, NOT `_is_role_address`. The word list is what let 30 of 79
    # existing rows go wrong (audit, 2026-08-16): it misses `_info@`, `compliance@`,
    # `clientservices@`, and cannot tell `ali.zamani@` from principal `Reza Zamani`.
    # Backfilling with the weak guard would re-create the defect we just cleaned up.
    attribution, basis = await classify_email_attribution(
        email, item.entity_name, principal_name
    )
    field = field_for_attribution(attribution)

    # --- Invariant 1: no principal named -> firm_email, whatever anything else says. ---
    # A mechanical backstop kept deliberately after the classifier: an address cannot be
    # "the principal's" on a row that names no principal, and that must not depend on a
    # model's answer.
    if not principal_name:
        field = "firm_email"

    if field != claim.field_name:
        logger.info(
            "corpus_validator: %s relabelled %s -> %s for %s (attribution=%s via %s)",
            email, claim.field_name, field, item.record_id, attribution, basis,
        )
    result = _claim_to_email_result(claim, field)
    result.reason = f"attribution_{attribution}"
    return result


def _claim_to_email_result(claim, field: str) -> EmailResult:
    """Map an enrichment Claim into an EmailResult, overriding `field` when an
    invariant forced a relabel."""
    return EmailResult(
        field=field,  # type: ignore[arg-type]
        email=claim.answer,
        source_url=claim.source_url,
        source_class=claim.source_class,
        extraction_method=claim.extraction_method,
        status=claim.status,
        # Carry the tier chain's own confidence. Hardcoding "low" here would throw away
        # the distinction between a site-scraped address found on the firm's own contact
        # page and a Snov guess, and that distinction is the whole basis on which a
        # reviewer later trusts the cell.
        confidence=getattr(claim, "confidence", None) or "low",
    )


# ---------------------------------------------------------------------------
# T43.4 — record_verdict + targeted re-ingest (the durable projection path)
# ---------------------------------------------------------------------------


def record_verdict(item: WorkItem, result: EmailResult, *, dry_run: bool = False, db_path: str | None = None) -> None:
    """Write an email verdict through app/db.py:upsert_claim + write_field_status, then
    re-project the touched record_id through the normal ingest path.

    A result with field=None writes NO claim — a non-verdict must never masquerade as a
    settled one. Only the checkpoint records that work was attempted.

    `dry_run=True` writes nothing to either database (no claim, no field_status, no
    checkpoint, no re-project) — it is the safety rail for the CLI's --dry-run flag.
    """
    if dry_run:
        return

    record_id = item.record_id
    with sqlite_connection(db_path) as conn:
        if result.field is not None and result.email:
            # Never default a missing status to "confirmed". In this codebase "confirmed"
            # means corroborated, and defaulting an *unknown* status to the strongest one
            # is exactly backwards — it would mint a corroborated-looking cell out of an
            # absence of information. `single_source` is the honest floor: one source
            # said so, nothing cross-checked it.
            status = result.status or "single_source"
            claim = {
                "field_name": result.field,
                "answer": result.email,
                "status": status,
                "source_url": result.source_url,
                "source_class": result.source_class,
                "extraction_method": result.extraction_method,
                "confidence": result.confidence or "low",
                "produced_by": "corpus_validator",
                "wave": "1",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
            upsert_claim(conn, record_id, claim)
            write_field_status(
                conn, record_id, result.field, status,
                method=result.extraction_method,
                confirming_url=result.source_url,
                confirming_class=result.source_class,
            )
        # A row whose lookup never ran is NOT work attempted — do not checkpoint it at
        # all. Checkpointing it would increment `attempts`, and three runs during a Snov
        # outage would poison the row permanently (attempts >= MAX_ATTEMPTS), retiring it
        # over a billing lapse rather than a fact about the firm. It stays eligible so the
        # next run picks it up once the tool is back.
        if result.reason == "tool_unavailable":
            logger.info("corpus_validator: %s left unattempted (tool unavailable)", record_id)
            return
        # Only a principal_email is 'done'. A firm_email is 'partial': a real result worth
        # keeping, but not the thing this pass is looking for — and `_load_skipped_ids`
        # skips only 'done', so a partial row stays eligible for a later run when the
        # name-targeted tier (Snov) is funded again. Marking it 'done' would freeze the
        # firm's switchboard in place as the final answer for that lead.
        if result.field == "principal_email":
            status = "done"
        elif result.field == "firm_email":
            status = "partial"
        else:
            status = "failed"
        upsert_validator_checkpoint(conn, record_id, "email", status,
                                    last_error=None if result.field else result.reason)


def record_type_verdict(
    item: WorkItem, verdict: TypeVerdict, *, dry_run: bool = False, db_path: str | None = None
) -> bool:
    """Persist an SFO/MFO verdict as a G1.Q4 claim. Returns True if a claim was written.

    `build_records._type_final` reads this by **question_id**, not by field name, so the
    claim carries `question_id="G1.Q4"` and `field_name=None` — matching how every other
    G1.Q4 claim in the store is shaped.

    A `type_unconfirmed` outcome writes NO claim. It records a checkpoint only, so a
    non-verdict can never masquerade as a settled one: `_type_final` already returns
    `type_unconfirmed` in the absence of a claim, and writing one saying so would put an
    unearned answer in the log.
    """
    if dry_run:
        return False
    wrote = False
    with sqlite_connection(db_path) as conn:
        if verdict.verdict in ("SFO", "MFO"):
            upsert_claim(conn, item.record_id, {
                "question_id": "G1.Q4",
                "field_name": None,
                "answer": verdict.verdict,
                # `confirmed` only when the firm said it on its own site; a third-party
                # directory is a single source and cannot self-corroborate.
                "status": "confirmed" if "own_site" in verdict.basis else "single_source",
                "source_url": verdict.source_url,
                "source_class": "site_scrape" if "own_site" in verdict.basis else "serper_organic",
                "extraction_method": "llm_quote_verified",
                "confidence": verdict.confidence,
                "produced_by": "corpus_validator",
                "wave": "1",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            })
            write_field_status(conn, item.record_id, "G1.Q4",
                               "confirmed" if "own_site" in verdict.basis else "single_source",
                               method="llm_quote_verified", confirming_url=verdict.source_url)
            wrote = True
        upsert_validator_checkpoint(
            conn, item.record_id, "type",
            "done" if wrote else "failed",
            last_error=None if wrote else (verdict.basis or "type_unconfirmed"),
        )
    return wrote


_INGEST_DIR = Path(__file__).resolve().parent.parent / "micro_rag" / "ingest"


def _load_ingest_module():
    """Import micro_rag/ingest/ingest.py by path — the SAME dance app/rag_sync.py uses
    (script-style module, no package __init__.py, sibling imports via sys.path). Returns
    None when the module or its dependencies (psycopg2, torch) are unavailable — the
    normal offline-test state."""
    path = _INGEST_DIR / "ingest.py"
    if not path.exists():
        return None
    try:
        if str(_INGEST_DIR) not in sys.path:
            sys.path.insert(0, str(_INGEST_DIR))
        spec = importlib.util.spec_from_file_location("_micro_rag_ingest_for_validator", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — a missing optional dependency is not an error here
        logger.info("corpus_validator: micro_rag ingest module unavailable", exc_info=True)
        return None


def _record_for(conn: sqlite3.Connection, record_id: str, ingest_mod) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Build one micro-RAG records row + its provenance for a single record_id, using
    the SAME build_record mapping the batch job uses (byte-identical re-project). Skips
    records that are no longer confirmed-shipped so a re-rejected entity is not
    re-published — same guard as app/rag_sync.py:_record_for."""
    entity = get_entity(conn, record_id)
    if entity is None:
        return None, []
    row = conn.execute(
        "SELECT outcome FROM enrichment_runs WHERE entity_id = ? ORDER BY id DESC LIMIT 1",
        (record_id,),
    ).fetchone()
    outcome = row["outcome"] if row else None
    if not is_confirmed(outcome):
        return None, []
    hq_state = ingest_mod._hq_state(conn, record_id)
    claims = get_claims(conn, record_id)
    record = ingest_mod.build_record(record_id, entity["canonical_name"], outcome, claims, hq_state=hq_state)
    provenance = [c for c in claims if c.get("field_name")]
    return record, provenance


def reproject_records(record_ids: list[str], db_path: str | None = None) -> dict[str, Any]:
    """Re-project ONLY the touched record_ids into Supabase through the normal ingest
    path — the one place in this project that writes to Postgres.

    Does NOT re-embed chunks: contact fields (principal_email/principal_phone) are never
    embedded (micro_rag/ingest/schema.sql says so explicitly) and `entity_type` is a
    structural column, so neither change needs new embeddings. Re-embedding 331 records
    for nothing costs minutes and loads torch for nothing. `write_to_postgres(...,
    embed=False)` skips the chunks delete/embed/re-insert cycle and leaves existing
    chunks in place.

    prune=False is load-bearing: we carry only the touched ids, so a pruning write would
    delete every other record. Removal of a re-judged lead stays the batch job's work.

    Returns a summary dict; never raises (a Postgres outage is reported, not fatal)."""
    if not record_ids:
        return {"status": "ok", "reprojected": 0}
    pg_dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not pg_dsn:
        return {"status": "skipped", "reason": "no DATABASE_URL", "touched": len(record_ids)}
    ingest_mod = _load_ingest_module()
    if ingest_mod is None:
        return {"status": "skipped", "reason": "ingest module unavailable", "touched": len(record_ids)}

    records: list[dict[str, Any]] = []
    provenance_by_record: dict[str, list[dict[str, Any]]] = {}
    skipped: list[str] = []
    with sqlite_connection(db_path) as conn:
        for rid in record_ids:
            try:
                record, provenance = _record_for(conn, rid, ingest_mod)
            except Exception as exc:  # noqa: BLE001
                logger.warning("corpus_validator: could not build record for %s", rid, exc_info=True)
                skipped.append(rid)
                continue
            if record is None:
                skipped.append(rid)
                continue
            records.append(record)
            provenance_by_record[rid] = provenance

    if not records:
        return {"status": "ok", "reprojected": 0, "skipped": skipped}

    try:
        build_hash = ingest_mod.write_to_postgres(
            pg_dsn, records, provenance_by_record, prune=False, embed=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("corpus_validator: Postgres re-project failed", exc_info=True)
        return {"status": "error", "reprojected": 0, "touched": len(records),
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"status": "ok", "reprojected": len(records), "skipped": skipped,
            "build_hash": build_hash}


# ---------------------------------------------------------------------------
# T43.5 invariant checks against live Supabase (read-only)
# ---------------------------------------------------------------------------


def _role_local_parts_list() -> list[str]:
    """The role-address local parts from enrichment.py, as a list for the ANY(%s) bind."""
    return sorted(_ROLE_LOCAL_PARTS)


def check_invariants(pg_dsn: str | None = None) -> dict[str, Any]:
    """Run the two T43.3 invariant checks against live Supabase, read-only.

    A. No record has a role-address principal_email.
    B. No record with a NULL/blank principal_name has a non-null principal_email.

    Both must be 0. A non-zero value is reported loudly as pre-existing corruption the
    operator needs to know about — it is not caused by this run, and this function never
    raises on it."""
    dsn = pg_dsn or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        return {"status": "skipped", "reason": "no DATABASE_URL"}
    roles = _role_local_parts_list()
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM records WHERE principal_email IS NOT NULL
                  AND lower(split_part(split_part(principal_email,'@',1),'+',1)) = ANY(%s)
                """,
                (roles,),
            )
            a = cur.fetchone()[0]
            cur.execute(
                """
                SELECT count(*) FROM records
                WHERE (principal_name IS NULL OR btrim(principal_name) = '')
                  AND principal_email IS NOT NULL AND btrim(principal_email) <> ''
                """
            )
            b = cur.fetchone()[0]
    finally:
        conn.close()
    return {
        "status": "ok",
        "A_role_principal_email": a,
        "B_nameless_principal_email": b,
        "pass": a == 0 and b == 0,
    }


# ---------------------------------------------------------------------------
# T43.7 — audit existing principal_email values and demote misattributions
# ---------------------------------------------------------------------------


def _demote_to_firm_email(
    conn: sqlite3.Connection, record_id: str, email: str, attribution: str,
    source_url: str | None, source_class: str | None, extraction_method: str | None,
) -> None:
    """Demote a misattributed `principal_email` to `firm_email`, keeping the address.

    Two writes, because the projection reads last-write-wins per field:
      1. a `principal_email` claim with status `removed_failed_validation`, which
         `build_records` renders as a NULL cell (it is the one status that blanks a
         value while keeping the claim visible in the log), and
      2. a `firm_email` claim carrying the same address and its original provenance.
    The contact is not lost — it is relabelled honestly. The rejection is also written to
    `audit_rejected_values`, the existing home for a value that was removed and why."""
    now = datetime.now(timezone.utc).isoformat()
    common = {
        "source_url": source_url,
        "source_class": source_class,
        "extraction_method": extraction_method,
        "produced_by": "corpus_validator",
        "wave": "1",
        "retrieved_at": now,
    }
    upsert_claim(conn, record_id, {
        **common,
        "field_name": "principal_email",
        "answer": None,
        "status": "removed_failed_validation",
        "confidence": "low",
    })
    upsert_claim(conn, record_id, {
        **common,
        "field_name": "firm_email",
        "answer": email,
        "status": "single_source",
        "confidence": "low",
    })
    write_field_status(conn, record_id, "principal_email", "removed_failed_validation")
    write_field_status(conn, record_id, "firm_email", "single_source",
                       method=extraction_method, confirming_url=source_url,
                       confirming_class=source_class)
    try:
        write_audit_rejected_value(
            conn, record_id, "principal_email", email,
            f"email_attribution_{attribution}", evidence_url=source_url,
        )
    except Exception:  # noqa: BLE001 — the audit row is a record, not the mechanism
        logger.warning("could not write audit_rejected_values for %s", record_id, exc_info=True)


async def audit_existing_emails(
    limit: int | None = None, *, apply: bool = False, db_path: str | None = None,
) -> dict[str, Any]:
    """Re-check every existing `principal_email` in the corpus and demote the ones that
    are not actually the named principal's address.

    This exists because the attribution guard is evaluated at WRITE time and never
    re-evaluated. Observed live 2026-08-16: TAFT FAMILY OFFICE kept a `principal_email`
    written while a principal was named, after that principal_name claim was later
    contradicted — leaving an address attributed to nobody. A write-time-only guard
    cannot catch that class of drift; only a sweep can.

    `apply=False` (the default) classifies and reports, writing nothing."""
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not dsn:
        return {"status": "skipped", "reason": "no DATABASE_URL"}

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT record_id, entity_name, principal_name, principal_email
                FROM records WHERE principal_email LIKE '%@%' ORDER BY record_id
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if limit is not None:
        rows = rows[:limit]

    findings: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for record_id, entity_name, principal_name, email in rows:
        attribution, basis = await classify_email_attribution(email, entity_name, principal_name)
        counts[attribution] = counts.get(attribution, 0) + 1
        if attribution != "principal":
            findings.append({
                "record_id": record_id, "entity_name": entity_name,
                "principal_name": principal_name, "email": email,
                "attribution": attribution, "basis": basis,
            })

    demoted: list[str] = []
    if apply and findings:
        with sqlite_connection(db_path) as sconn:
            for f in findings:
                prior = next(
                    (c for c in get_claims(sconn, f["record_id"])
                     if c.get("field_name") == "principal_email" and c.get("answer")),
                    {},
                )
                _demote_to_firm_email(
                    sconn, f["record_id"], f["email"], f["attribution"],
                    prior.get("source_url"), prior.get("source_class"),
                    prior.get("extraction_method"),
                )
                demoted.append(f["record_id"])

    reproject = reproject_records(demoted, db_path=db_path) if demoted else None
    return {
        "status": "ok", "checked": len(rows), "counts": counts,
        "findings": findings, "demoted": demoted, "reproject": reproject,
        "applied": apply,
    }


# ---------------------------------------------------------------------------
# T43.2 — still a stub (round 3)
# ---------------------------------------------------------------------------


_TYPE_PATHS = ("", "/about", "/about-us", "/who-we-serve", "/our-firm", "/clients", "/services")

_TYPE_SYSTEM = """You decide whether a firm is a single-family office (SFO) or a
multi-family office (MFO), using ONLY the evidence given.

  SFO  - serves ONE family's wealth. Look for: "single family office", "a single family",
         "our family", "the <surname> family's capital", "we manage the assets of one family".
  MFO  - serves SEVERAL client families. Look for: "multi-family office", "client families",
         "families we serve", "our clients", "we serve N families", a client/prospect pitch.

CRITICAL RULES:
* The firm merely being NAMED "<something> Family Office" is NOT evidence either way.
  Almost every firm here is named that way. Ignore the name entirely.
* You must QUOTE the deciding sentence verbatim from the evidence. Copy it exactly,
  character for character. Do not paraphrase, summarise, or reconstruct it.
* If no passage decides it, answer type_unconfirmed. That is a normal, correct answer and
  is much better than a guess.

Reply as exactly three lines and nothing else:
VERDICT: SFO | MFO | type_unconfirmed
SOURCE: <the source url you quoted from, or NONE>
QUOTE: <the verbatim sentence, or NONE>"""


def _name_words(entity_name: str) -> set[str]:
    return {w for w in re.split(r"[^A-Za-z]+", entity_name.lower()) if len(w) > 2}


# A quote can be perfectly verbatim and still decide nothing. Observed live 2026-08-17:
# PERSIMMON CAPITAL MANAGEMENT was returned as MFO on the grounded quote "We are a family
# focused, privately owned, boutique wealth advisory firm" — which says nothing about one
# family versus many. Being real is not the same as being relevant, so the quote must also
# contain language that actually distinguishes the two.
_MFO_PHRASES = (
    "multi-family office", "multi family office", "multifamily office",
    "client families", "families we serve", "our clients", "client relationships",
    "families and individuals", "serve families",
)
_SFO_PHRASES = (
    "single-family office", "single family office", "a single family",
    "one family", "our family", "sole source of capital", "the family's capital",
)


def _quote_is_decisive(snippet: str, verdict: str) -> bool:
    """Does the quote contain language that actually separates SFO from MFO?"""
    s = re.sub(r"\s+", " ", snippet).lower()
    phrases = _MFO_PHRASES if verdict == "MFO" else _SFO_PHRASES
    return any(p in s for p in phrases)


def _extract_sentence(text: str, phrase: str) -> str | None:
    """The sentence in `text` containing `phrase`, so the stored quote is real text we
    located ourselves rather than anything a model produced."""
    flat = re.sub(r"\s+", " ", text)
    i = flat.lower().find(phrase)
    if i < 0:
        return None
    start = max(0, flat.rfind(".", 0, i) + 1)
    end = flat.find(".", i + len(phrase))
    end = len(flat) if end < 0 else end + 1
    return flat[start:end].strip()[:400] or None


def _mechanical_verdict(
    evidence: list[tuple[str, str]], domain: str | None
) -> tuple[str, str, str] | None:
    """Decide SFO/MFO by searching the evidence OURSELVES. Returns (verdict, quote, url).

    This is the answer to the biggest failure mode in the LLM path: it cited text that did
    not exist on 85 of 320 rows (2026-08-16). But a fabricated *citation* does not mean the
    evidence lacks the fact — the model routinely re-cased or paraphrased a phrase that is
    genuinely on the page. So rather than take its word or throw the row away, locate a
    decisive phrase directly and quote the sentence around it. Nothing here can fabricate:
    the phrase is found by substring search, and the quote is sliced out of the source.

    Own-site evidence counts double — a firm describing itself outranks a directory. A row
    where both sides appear and neither dominates stays unconfirmed rather than guessing.
    """
    scores = {"MFO": 0, "SFO": 0}
    best: dict[str, tuple[str, str]] = {}
    for url, text in evidence:
        flat = re.sub(r"\s+", " ", text).lower()
        weight = 2 if (domain and domain in url) else 1
        for verdict, phrases in (("MFO", _MFO_PHRASES), ("SFO", _SFO_PHRASES)):
            for phrase in phrases:
                if phrase in flat:
                    scores[verdict] += weight
                    if verdict not in best or weight == 2:
                        sentence = _extract_sentence(text, phrase)
                        if sentence:
                            best[verdict] = (sentence, url)
                    break
    winner = max(scores, key=lambda k: scores[k])
    loser = "SFO" if winner == "MFO" else "MFO"
    # Require a clear margin: an even split is genuine ambiguity, not a close call.
    if scores[winner] == 0 or scores[winner] <= scores[loser] or winner not in best:
        return None
    quote, url = best[winner]
    return winner, quote, url


def _snippet_is_just_the_name(snippet: str, entity_name: str) -> bool:
    """A 'quote' that is only the firm's own name (plus filler) decides nothing — the
    entity being called "X Family Office" is exactly the non-evidence the prompt forbids."""
    words = {w for w in re.split(r"[^A-Za-z]+", snippet.lower()) if len(w) > 2}
    filler = {"family", "office", "offices", "llc", "inc", "the", "and", "for", "wealth"}
    return not (words - _name_words(entity_name) - filler)


async def _gather_type_evidence(
    entity_name: str, domain: str | None
) -> list[tuple[str, str]]:
    """(url, text) evidence, the firm's OWN pages first so they outrank a directory."""
    evidence: list[tuple[str, str]] = []
    if domain:
        async def _one(path: str) -> tuple[str, str] | None:
            url = f"https://{domain}{path}"
            try:
                got = await asyncio.wait_for(free_fetch_raw(url), timeout=_PAGE_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                return None
            content = (got or {}).get("content") or ""
            return (url, content[:6000]) if content else None

        for item in await asyncio.gather(*(_one(p) for p in _TYPE_PATHS),
                                         return_exceptions=True):
            if isinstance(item, tuple):
                evidence.append(item)

    for query in (f'"{entity_name}" "multi-family office"',
                  f'"{entity_name}" "single family office"'):
        try:
            search = await serper_search_raw(query, max_results=4)
        except Exception:  # noqa: BLE001
            continue
        for r in search.get("results", []):
            text = " ".join(filter(None, [r.get("title"), r.get("content")]))
            if text:
                evidence.append((r.get("url") or query, text))
    return evidence


async def classify_entity_type(
    entity_name: str, domain: str | None = None, aliases: list[str] | None = None
) -> TypeVerdict:
    """T43.2 — evidence-gated SFO/MFO verdict. `type_unconfirmed` is a legitimate outcome
    (G1.Q4 is a SOFT gate, `on_unknown="ship_with_label"`), never forced away from.

    The quoted snippet is checked IN CODE against the source text it claims to come from.
    A composed quote forces `type_unconfirmed` — prompting a model to be accurate does not
    survive 331 rows; a substring assertion does.
    """
    basis: list[str] = []

    # 1. ADV registration as a PRIOR, never a verdict. A true SFO is exempt from SEC
    #    registration under Rule 202(a)(11)(G)-1, so a registration leans MFO — but an MFO
    #    under $100M may be state-registered and a family office may register voluntarily.
    #    Only an `exact` name match may be attributed to this entity (adv.py is explicit:
    #    a partial match's registration belongs to a different firm).
    try:
        adv = await adv_firm_search_raw(entity_name, max_results=5)
        exact = [r for r in adv.get("results", []) if r.get("name_match") == "exact"]
        if exact:
            reg = bool(exact[0].get("is_registered_investment_adviser"))
            basis.append(f"adv:registered={reg};branches={exact[0].get('branches_count')}")
        else:
            basis.append("adv:no_exact_match")  # itself mild evidence FOR an SFO
    except Exception:  # noqa: BLE001 — the prior is optional, the verdict is not
        basis.append("adv:unavailable")

    evidence = await _gather_type_evidence(entity_name, domain)
    if not evidence:
        return TypeVerdict(verdict="type_unconfirmed", basis=";".join(basis) + ";no_evidence")

    block = "\n\n".join(f"[{url}]\n{text}" for url, text in evidence)[:14000]
    prompt = f"Firm: {entity_name}\nADV signal: {'; '.join(basis)}\n\nEVIDENCE:\n{block}"
    try:
        reply = await get_model("cheapest").ainvoke(
            [SystemMessage(content=_TYPE_SYSTEM), HumanMessage(content=prompt)]
        )
        raw = getattr(reply, "content", "") or ""
        if isinstance(raw, list):
            raw = " ".join(str(b.get("text", "")) for b in raw if isinstance(b, dict))
    except Exception:  # noqa: BLE001 — an LLM outage is not a verdict
        logger.warning("classify_entity_type: LLM unavailable for %s", entity_name, exc_info=True)
        return TypeVerdict(verdict="type_unconfirmed", basis=";".join(basis) + ";llm_unavailable")

    def _field(tag: str) -> str:
        m = re.search(rf"^{tag}:\s*(.+)$", str(raw), re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    verdict = _field("VERDICT").upper()
    verdict = "SFO" if verdict.startswith("SFO") else "MFO" if verdict.startswith("MFO") else "type_unconfirmed"
    source, quote = _field("SOURCE"), _field("QUOTE")

    # --- the mechanical gate: the quote must literally appear in the evidence ---
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    def _fallback(reason: str) -> TypeVerdict:
        """The model's citation failed a gate. Two recoveries before giving up, because a
        bad citation is not proof the verdict is wrong — only that the *quote* is not
        usable as the evidence.

        1. Look for the fact ourselves in the evidence (`_mechanical_verdict`), which
           yields a real sentence we located rather than one a model produced.
        2. Failing that, keep the model's verdict as `single_source` at low confidence.
           It read the whole evidence block and its judgement is a source in its own
           right — just an unverifiable one, so it is recorded as exactly that and is
           filterable via `basis` / confidence. A wrong quote was never a good reason to
           discard a probably-right answer.
        """
        mech = _mechanical_verdict(evidence, domain)
        if mech is None:
            if verdict in ("SFO", "MFO"):
                return TypeVerdict(
                    verdict=verdict,  # type: ignore[arg-type]
                    source_url=(hit[0] if (hit := next(((u, t) for u, t in evidence), None)) else None),
                    snippet=None,
                    confidence="low",
                    basis=";".join(basis) + f";{reason};llm_unverified",
                )
            return TypeVerdict(verdict="type_unconfirmed", basis=";".join(basis) + ";" + reason)
        mverdict, mquote, murl = mech
        own = bool(domain and domain in murl)
        logger.info("classify_entity_type: %s — %s, recovered %s mechanically from %s",
                    entity_name, reason, mverdict, murl)
        return TypeVerdict(
            verdict=mverdict,  # type: ignore[arg-type]
            source_url=murl, snippet=mquote,
            # Deliberately a notch below the LLM path even on the firm's own site: this
            # verdict rests on a phrase match, not on a model reading the passage in
            # context, so it should never outrank evidence that was actually comprehended.
            confidence="medium" if own else "low",
            basis=";".join(basis) + f";{reason};recovered_mechanically"
                  + (";own_site" if own else ";third_party"),
            source_text=None,
        )

    # The model declining, or returning no quote, is also a case where the evidence may
    # still contain the fact plainly — send it through the same mechanical recovery.
    if verdict == "type_unconfirmed" or quote.upper() in ("", "NONE"):
        return _fallback("llm_declined")

    nq = _norm(quote)
    hit = next(((u, t) for u, t in evidence if nq and nq in _norm(t)), None)
    if hit is None:
        logger.warning("classify_entity_type: %s — quote not found in evidence (%r)",
                       entity_name, quote[:80])
        return _fallback("quote_not_grounded")
    if _snippet_is_just_the_name(quote, entity_name):
        return _fallback("quote_is_only_the_name")
    if not _quote_is_decisive(quote, verdict):
        return _fallback("quote_not_decisive")

    url = hit[0]
    own_site = bool(domain and domain in url)
    return TypeVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        source_url=url if source.upper() != "NONE" else url,
        snippet=quote,
        confidence="high" if own_site else "medium",
        basis=";".join(basis) + (";own_site" if own_site else ";third_party"),
        source_text=hit[1],
    )