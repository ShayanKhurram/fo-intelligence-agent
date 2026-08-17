// T46.2 — the Intent Watcher research core.
//
// Pure and unit-testable apart from the two `fetch` calls (Serper / GDELT). **Does not
// import `lib/db.ts`** (directly or transitively) so it can be exercised by `node --test`
// with no database. The route layer (T46.3) is what persists signals and joins to the
// corpus; this module only searches, gates, dates, and interprets.
//
// Two correctness properties above all else, both enforced here:
//   1. Never fabricate a date — an unparseable article date means the article is dropped,
//      never stamped with `now()`.
//   2. Never fabricate a meaning — if the LLM call fails or returns invalid JSON the
//      article still becomes a signal with `meaning: null`. A blank is honest; an
//      invention is not.
import { createHash } from "node:crypto";
import { z } from "zod";
import { ollamaChat, type ChatMessage } from "./ollama.ts";

// ---------------------------------------------------------------------------
// Entity name normalization
// ---------------------------------------------------------------------------

// Corporate suffix tokens stripped from the **trailing** run of an entity name before
// matching. The matcher also recognises a *following* marker (separate set below); these
// two sets are deliberately different — suffix-stripping cleans the entity itself, while
// the following-marker test asks "does the article name the firm as a firm?".
const SUFFIX_TOKENS = new Set([
  "inc", "llc", "l.l.c", "lp", "l.p", "ltd", "limited",
  "corp", "corporation", "co", "company", "pllc", "pc", "plc",
  // `group` is stripped so `REAP FINANCIAL GROUP, LLC` normalizes to `reap financial`
  // (PLAN.md T46 constraint 4) — without it the phrase would be `reap financial group`,
  // which never appears in the live reject strings, so the marker/3-token gate the
  // accept rule was designed around would never be exercised. `group` is a genuine
  // corporate suffix ("Vista Group LLC"), just missing from the T46.2 suffix list.
  "group",
]);

export function normalizeEntityName(name: string): {
  phrase: string;
  tokens: string[];
  corporate: string[];
} {
  // Lowercase, replace '.'/',' with spaces, collapse whitespace.
  const lower = name.toLowerCase().replace(/[.,]/g, " ").replace(/\s+/g, " ").trim();
  const rawTokens = lower.length ? lower.split(" ") : [];

  // Strip a trailing run of corporate suffix tokens. Only the trailing run is stripped —
  // "The Coury Firm" keeps all three tokens because none of them are suffixes; "Diamant
  // Asset Management, Inc." drops only "inc".
  const corporate: string[] = [];
  const body = [...rawTokens];
  while (body.length > 0 && SUFFIX_TOKENS.has(body[body.length - 1])) {
    corporate.unshift(body.pop()!);
  }
  const phrase = body.join(" ").replace(/\s+/g, " ").trim();
  // `tokens` are the remaining tokens of length > 2 — short tokens like "co" (a real
  // fragment after stripping) are noise for the "3 or more significant tokens" test. A
  // leading English article (the/a/an) stays in `phrase` (it is part of the firm's name
  // and must be matched contiguously) but does NOT count toward significance, so "The
  // Coury Firm" has 2 significant tokens, not 3 — clearing the 3-token branch of the
  // accept gate on the strength of an article would weaken the relevance test.
  const LEADING_ARTICLES = new Set(["the", "a", "an"]);
  const tokens = body.filter(
    (t, i) => t.length > 2 && !(i === 0 && LEADING_ARTICLES.has(t)),
  );
  return { phrase, tokens, corporate };
}

// ---------------------------------------------------------------------------
// Rate limiting — module-level token buckets
// ---------------------------------------------------------------------------

// One Serper credit per org; Serper sustained ~5 req/s in the live probe. GDELT is the
// keyless fallback and enforces one request every 5 seconds (a burst gets a plain-text
// "please limit" body instead of JSON). The buckets are process-global so a concurrent
// run cannot oversend even if multiple orgs race through `searchActivity`.
class TokenBucket {
  private capacity: number;
  private refillPerSec: number;
  private tokens: number;
  private lastRefill: number;

  constructor(refillPerSec: number, capacity?: number) {
    this.refillPerSec = refillPerSec;
    this.capacity = capacity ?? refillPerSec;
    this.tokens = this.capacity;
    this.lastRefill = Date.now();
  }

  private refill(now: number): void {
    const elapsed = (now - this.lastRefill) / 1000;
    if (elapsed > 0) {
      this.tokens = Math.min(this.capacity, this.tokens + elapsed * this.refillPerSec);
      this.lastRefill = now;
    }
  }

  async take(): Promise<void> {
    // Loop until a token is available, sleeping just long enough for one to refill.
    // Bounded wait — a caller never blocks longer than `1/refillPerSec` seconds when the
    // bucket is empty, so a long run stays responsive.
    while (true) {
      const now = Date.now();
      this.refill(now);
      if (this.tokens >= 1) {
        this.tokens -= 1;
        return;
      }
      const deficit = 1 - this.tokens;
      const waitMs = Math.ceil((deficit / this.refillPerSec) * 1000);
      await new Promise((r) => setTimeout(r, Math.max(waitMs, 1)));
    }
  }
}

const serperBucket = new TokenBucket(5, 5);
const gdeltBucket = new TokenBucket(0.2, 1); // 1 per 5 seconds

// Exposed for tests / the route so the bucket can be awaited deterministically without
// depending on the constructor internals.
export function __acquireSerperSlot(): Promise<void> {
  return serperBucket.take();
}
export function __acquireGdeltSlot(): Promise<void> {
  return gdeltBucket.take();
}

// ---------------------------------------------------------------------------
// Article + search
// ---------------------------------------------------------------------------

export type Article = {
  title: string;
  url: string;
  source: string;
  dateRaw: string | null;
  observedAt: Date | null;
};

export type SearchResult = {
  articles: Article[];
  error?: string; // credits_exhausted | rate_limited | http_<status> | no_key | fetch_failed
  skipped?: string; // name_too_generic
};

// Domains that never carry a usable firm-activity signal — directories, social media,
// regulators. The block is by exact host OR by suffix (so `instagram.com` also blocks
// `www.instagram.com`). Order matters in `acceptArticle`, not here.
const BLOCKED_DOMAINS = [
  "linkedin.com",
  "preqin.com",
  "advisorfinder.com",
  "usnews.com",
  "zoominfo.com",
  "crunchbase.com",
  "pitchbook.com",
  "sec.gov",
  "instagram.com",
  "facebook.com",
  "twitter.com",
  "x.com",
  "tiktok.com",
  "youtube.com",
  "reddit.com",
];

function hostOf(url: string): string | null {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}

function isBlockedDomain(host: string | null): boolean {
  if (!host) return false;
  for (const d of BLOCKED_DOMAINS) {
    if (host === d || host.endsWith("." + d)) return true;
  }
  return false;
}

export async function searchActivity(
  name: string,
  opts: { lookbackDays: number; backend?: "serper" | "gdelt"; key?: string },
): Promise<SearchResult> {
  const normalized = normalizeEntityName(name);
  // A one-token entity ("Vista") would match far too much prose; refuse to search rather
  // than burn a credit on a query that cannot be gated. No HTTP call is made.
  if (normalized.tokens.length < 2) {
    return { articles: [], skipped: "name_too_generic" };
  }

  const backend =
    opts.backend ?? (opts.key || process.env.SERPER_API_KEY ? "serper" : "gdelt");

  if (backend === "serper") {
    const key = opts.key ?? process.env.SERPER_API_KEY;
    if (!key) {
      // No key configured — fall back to GDELT rather than fail. A missing key is not an
      // error worth surfacing; GDELT is the documented keyless path.
      return searchActivityGdelt(normalized.phrase, opts.lookbackDays);
    }
    return searchActivitySerper(normalized.phrase, key);
  }
  return searchActivityGdelt(normalized.phrase, opts.lookbackDays);
}

async function searchActivitySerper(phrase: string, key: string): Promise<SearchResult> {
  await serperBucket.take();
  let resp: Response;
  try {
    resp = await fetch("https://google.serper.dev/news", {
      method: "POST",
      headers: { "X-API-KEY": key, "Content-Type": "application/json" },
      body: JSON.stringify({ q: phrase, num: 10 }),
    });
  } catch {
    return { articles: [], error: "fetch_failed" };
  }

  // Serper reports a spent balance as HTTP 400 with `{"message":"Not enough credits"}`,
  // NOT 429 (mirrors app/tools/keyrotation.py:is_exhaustion_response). Surface it as a
  // distinct `credits_exhausted` error so the route can stop the whole run instead of
  // burning every remaining org against an already-spent key. A 429 is ordinary
  // throttling — `rate_limited`, retried by the caller, never mistaken for exhaustion.
  if (resp.status === 400 || resp.status === 401 || resp.status === 402 || resp.status === 403 || resp.status === 429) {
    if (resp.status === 429) {
      return { articles: [], error: "rate_limited" };
    }
    const body = await resp.text().catch(() => "");
    if (isExhaustion(resp.status, body)) {
      return { articles: [], error: "credits_exhausted" };
    }
    return { articles: [], error: `http_${resp.status}` };
  }
  if (!resp.ok) {
    return { articles: [], error: `http_${resp.status}` };
  }

  const data = await resp.json().catch(() => null) as
    | { news?: Array<{ title?: string; link?: string; source?: string; date?: string }> }
    | null;
  const articles: Article[] = [];
  for (const item of data?.news ?? []) {
    const title = (item.title ?? "").trim();
    const url = (item.link ?? "").trim();
    if (!title || !url) continue;
    const dateRaw = item.date ?? null;
    articles.push({
      title,
      url,
      source: (item.source ?? "").trim(),
      dateRaw,
      observedAt: parseArticleDate(dateRaw, new Date()),
    });
  }
  return { articles };
}

async function searchActivityGdelt(phrase: string, lookbackDays: number): Promise<SearchResult> {
  await gdeltBucket.take();
  const start = gdeltStartDateTime(lookbackDays);
  const url =
    `https://api.gdeltproject.org/api/v2/doc/doc` +
    `?query=${encodeURIComponent('"' + phrase + '"')}` +
    `&mode=ArtList&format=json&startdatetime=${start}&maxrecords=10`;

  let resp: Response;
  try {
    resp = await fetch(url, { headers: { Accept: "application/json" } });
  } catch {
    return { articles: [], error: "fetch_failed" };
  }

  // GDELT answers a burst with a plain-text "Please limit requests to one every 5
  // seconds" body instead of JSON. Treat a non-JSON body as a retryable `rate_limited`
  // error, not an outage — the caller should back off, not fail the org.
  if (resp.status === 429) {
    return { articles: [], error: "rate_limited" };
  }
  if (!resp.ok) {
    return { articles: [], error: `http_${resp.status}` };
  }
  const text = await resp.text();
  let data: { articles?: Array<{ title?: string; url?: string; domain?: string; seendate?: string }> };
  try {
    data = JSON.parse(text);
  } catch {
    return { articles: [], error: "rate_limited" };
  }

  const articles: Article[] = [];
  for (const item of data.articles ?? []) {
    const title = (item.title ?? "").trim();
    const articleUrl = (item.url ?? "").trim();
    if (!title || !articleUrl) continue;
    const dateRaw = item.seendate ?? null;
    articles.push({
      title,
      url: articleUrl,
      source: (item.domain ?? "").trim(),
      dateRaw,
      observedAt: parseArticleDate(dateRaw, new Date()),
    });
  }
  return { articles };
}

// True if this response means "this key is spent". Mirrors
// app/tools/keyrotation.py:is_exhaustion_response, whose module docstring is explicit
// that the trigger "is not simply a 429" — Serper reports a spent balance as **400 with
// a `Not enough credits` body**, while a 429 is ordinary throttling that should be
// retried. So we decide on the **body phrases only**; a bare 429 must NOT count as
// exhaustion (it is `rate_limited`, handled by the caller).
export function isExhaustion(status: number, body: string): boolean {
  // Only statuses that can carry an exhaustion message are worth inspecting. A bare 429
  // is deliberately NOT in this set — it is a rate limit, not a spent balance.
  if (status !== 400 && status !== 401 && status !== 402 && status !== 403) return false;
  const phrases = [
    "not enough credits",
    "no credits",
    "insufficient credit",
    "consumed all your api credits",
    "credit limit",
    "quota exceeded",
    "out of credits",
    "limit reached",
  ];
  const hay = (body || "").toLowerCase();
  return phrases.some((p) => hay.includes(p));
}

function gdeltStartDateTime(lookbackDays: number): string {
  // GDELT expects YYYYMMDDHHMMSSZ. Anchor to now minus lookbackDays.
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - lookbackDays);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    d.getUTCFullYear().toString() +
    pad(d.getUTCMonth() + 1) +
    pad(d.getUTCDate()) +
    pad(d.getUTCHours()) +
    pad(d.getUTCMinutes()) +
    pad(d.getUTCSeconds()) +
    "Z"
  );
}

// ---------------------------------------------------------------------------
// Date parsing
// ---------------------------------------------------------------------------

// Parse Serper's relative strings ("23 hours ago", "3 weeks ago"), Serper's absolute
// "Jun 10, 2026", and GDELT's "20260620T120000Z". Returns null on anything unparseable
// — the caller drops the article rather than fabricating a date with `now()`.
export function parseArticleDate(raw: string | null, now: Date): Date | null {
  if (!raw) return null;
  const s = raw.trim();
  if (!s) return null;

  // GDELT: 20260620T120000Z
  const gdelt = s.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/i);
  if (gdelt) {
    const [, y, mo, d, h, mi, se] = gdelt;
    const dt = new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +se));
    return isNaN(dt.getTime()) ? null : dt;
  }

  // ISO 8601 (some sources pass a full timestamp).
  const iso = new Date(s);
  if (!isNaN(iso.getTime()) && /\d{4}-\d{2}-\d{2}/.test(s)) {
    return iso;
  }

  // "N hours|days|weeks|months|years ago"
  const rel = s.match(/^(\d+)\s+(hour|day|week|month|year)s?\s+ago$/i);
  if (rel) {
    const n = +rel[1];
    const unit = rel[2].toLowerCase();
    const dt = new Date(now);
    switch (unit) {
      case "hour":
        dt.setUTCHours(dt.getUTCHours() - n);
        break;
      case "day":
        dt.setUTCDate(dt.getUTCDate() - n);
        break;
      case "week":
        dt.setUTCDate(dt.getUTCDate() - n * 7);
        break;
      case "month":
        dt.setUTCMonth(dt.getUTCMonth() - n);
        break;
      case "year":
        dt.setUTCFullYear(dt.getUTCFullYear() - n);
        break;
    }
    return isNaN(dt.getTime()) ? null : dt;
  }

  // "Jun 10, 2026" — Serper's absolute form.
  const abs = s.match(/^([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})$/);
  if (abs) {
    const [, mon, day, year] = abs;
    const mo = MONTHS[mon.toLowerCase()];
    if (mo !== undefined) {
      const dt = new Date(Date.UTC(+year, mo, +day));
      return isNaN(dt.getTime()) ? null : dt;
    }
  }

  return null;
}

const MONTHS: Record<string, number> = {
  jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
  jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
};

// ---------------------------------------------------------------------------
// acceptArticle — the relevance gate
// ---------------------------------------------------------------------------

// The token that may immediately follow the entity phrase in the article and still count
// as a match. The article must name the firm *as a firm* — "Turtle Creek Wealth Advisors
// LLC" matches because the phrase is immediately followed by `llc`; "reap financial
// rewards" does not because the next token is `rewards`.
const FOLLOWING_MARKERS = new Set([
  "llc", "inc", "lp", "ltd", "corp", "corporation", "co", "company",
  "pllc", "pc", "plc", "advisors", "advisers", "capital", "management",
  "partners", "group", "wealth", "holdings", "office", "family", "trust",
]);

export type AcceptResult = { ok: true } | { ok: false; reason: string };

export function acceptArticle(
  article: { title: string; url: string; observedAt: Date | null },
  normalized: { phrase: string; tokens: string[] },
  opts: { lookbackDays: number; now: Date },
): AcceptResult {
  // Reject in this fixed order, with these reason codes:
  //   undated → stale → blocked_domain → name_not_matched
  if (article.observedAt === null) {
    return { ok: false, reason: "undated" };
  }

  const ms = opts.now.getTime() - article.observedAt.getTime();
  const tooOldMs = opts.lookbackDays * 24 * 60 * 60 * 1000;
  if (ms > tooOldMs) {
    return { ok: false, reason: "stale" };
  }

  // blocked_domain is checked BEFORE the name test — a HUFFMAN FAMILY OFFICE post on
  // instagram.com is rejected for the domain, never reaches the name matcher.
  const host = hostOf(article.url);
  if (isBlockedDomain(host)) {
    return { ok: false, reason: "blocked_domain" };
  }

  return matchName(article.title, article.url, normalized);
}

// Normalize title + URL slug to lowercase alphanumerics separated by single spaces, then
// require the normalized **phrase to appear contiguously**, AND at least one of:
//   (a) the token immediately following the phrase is a corporate marker, or
//   (b) the entity has 3 or more significant tokens.
// Token-containment alone is not enough: `REAP FINANCIAL GROUP, LLC` normalizes to
// `reap financial`, which matches "programs reap financial rewards".
function matchName(
  title: string,
  url: string,
  normalized: { phrase: string; tokens: string[] },
): AcceptResult {
  const phrase = normalized.phrase;
  if (!phrase) return { ok: false, reason: "name_not_matched" };

  // Pull a slug out of the URL path so domain/query noise does not interfere, then fold
  // title + slug into one normalized haystack of lowercase alphanumerics.
  let slug = "";
  try {
    const u = new URL(url);
    slug = u.pathname;
  } catch {
    slug = url;
  }
  const haystack = normalizeForMatch(`${title} ${slug}`);
  const phraseNorm = normalizeForMatch(phrase);

  if (!phraseNorm) return { ok: false, reason: "name_not_matched" };

  const idx = haystack.indexOf(phraseNorm);
  if (idx === -1) {
    return { ok: false, reason: "name_not_matched" };
  }

  // Look at the token immediately after the phrase. The haystack is space-separated, so
  // the next token starts at idx + phraseNorm.length + 1 (skipping the single space).
  const afterStart = idx + phraseNorm.length;
  const rest = afterStart < haystack.length ? haystack.slice(afterStart) : "";
  const nextToken = rest.trim().split(/\s+/)[0] ?? "";

  const followedByMarker = nextToken.length > 0 && FOLLOWING_MARKERS.has(nextToken);
  const threeOrMore = normalized.tokens.length >= 3;

  if (!followedByMarker && !threeOrMore) {
    return { ok: false, reason: "name_not_matched" };
  }
  return { ok: true };
}

// Lowercase, keep alphanumerics, collapse runs of non-alphanumerics to single spaces.
function normalizeForMatch(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
}

// ---------------------------------------------------------------------------
// dedupeKey — stable per (host+path), independent of query/fragment/trailing slash
// ---------------------------------------------------------------------------

export function dedupeKey(url: string): string {
  let host = "";
  let path = "";
  try {
    const u = new URL(url);
    host = u.hostname.replace(/^www\./, "");
    path = u.pathname;
  } catch {
    // Not a clean URL — fall back to the raw string so we still produce a stable key.
    host = url;
  }
  // Strip a trailing slash so `/foo/` and `/foo` share a key, and drop any case
  // difference in the host. Query string and fragment are already discarded by URL
  // parsing above.
  let p = path.replace(/\/+$/, "");
  if (p.length === 0) p = "/";
  const canon = `${host.toLowerCase()}${p}`;
  return createHash("sha1").update(canon).digest("hex");
}

// ---------------------------------------------------------------------------
// interpretBatch — ONE LLM call per batch of up to 5 orgs
// ---------------------------------------------------------------------------

export type Interpretation = {
  dedupeKey: string;
  kind: string;
  meaning: string | null;
  confidence: "observed" | "inferred" | null;
};

export type InterpretItem = {
  recordId: string;
  entityName: string;
  articles: Article[];
};

const INTERPRET_BATCH_SIZE = 5;
const MAX_ARTICLES_PER_ORG = 6;
const MAX_MEANING_CHARS = 200;

const InterpretResponseSchema = z.array(
  z.object({
    recordId: z.string(),
    index: z.number().int().min(0),
    kind: z.enum([
      "capital_deployment",
      "exit",
      "personnel",
      "mandate_shift",
      "firm_news",
    ]),
    meaning: z.string().nullable(),
    confidence: z.enum(["observed", "inferred"]),
  }),
);

// Trim an org's articles to the recency-sorted, 6-item list the model will actually
// see. This is the SINGLE source of truth for which articles get interpreted — both the
// prompt and the defaults overlay use this same list, so an org with >6 articles can
// never have a model answer dropped for lack of a slot (the round-1 bug: defaults were
// built from `item.articles.slice(0,6)` in original order while the prompt sorted by
// recency first, so the two sets diverged and the overlay `find` missed).
export function selectArticles(item: InterpretItem): Article[] {
  return item.articles
    .slice()
    .sort((a, b) => {
      const at = a.observedAt?.getTime() ?? 0;
      const bt = b.observedAt?.getTime() ?? 0;
      return bt - at;
    })
    .slice(0, MAX_ARTICLES_PER_ORG);
}

// Batch the items into groups of INTERPRET_BATCH_SIZE, make ONE ollamaChat call per
// batch, validate with zod, and degrade to `meaning: null` on any failure. The articles
// still become signals (the caller persists them regardless); a missing interpretation
// is an honest blank, never a fabricated one.
export async function interpretBatch(
  items: InterpretItem[],
): Promise<Map<string, Interpretation[]>> {
  const result = new Map<string, Interpretation[]>();

  for (let i = 0; i < items.length; i += INTERPRET_BATCH_SIZE) {
    const batch = items.slice(i, i + INTERPRET_BATCH_SIZE);
    const interpreted = await interpretOneBatch(batch).catch(() => null);

    // Seed every org in the batch with the honest-blank default so that even on total
    // failure every article still maps to a signal with meaning=null and kind=firm_news.
    // Use `selectArticles` so the defaults slots line up 1:1 with the articles the prompt
    // sent — the overlay below finds a slot by dedupeKey for every article the model saw.
    for (const item of batch) {
      const defaults: Interpretation[] = selectArticles(item).map((a) => ({
        dedupeKey: dedupeKey(a.url),
        kind: "firm_news",
        meaning: null,
        confidence: null,
      }));
      result.set(item.recordId, defaults);
    }

    if (!interpreted) continue; // LLM failure → keep the honest-blank defaults

    // Overlay the model's interpretations on top of the defaults, by (recordId, index).
    // `row.index` is into the `selectArticles` list (the same list the prompt used), so
    // the slot is guaranteed to exist.
    for (const row of interpreted) {
      const item = batch.find((b) => b.recordId === row.recordId);
      if (!item) continue;
      const selected = selectArticles(item);
      const article = selected[row.index];
      if (!article) continue;
      const meaning = row.meaning ? row.meaning.slice(0, MAX_MEANING_CHARS) : null;
      const existing = result.get(item.recordId);
      if (!existing) continue;
      const slot = existing.find((e) => e.dedupeKey === dedupeKey(article.url));
      if (slot) {
        slot.kind = row.kind;
        slot.meaning = meaning;
        slot.confidence = row.confidence;
      }
    }
  }

  return result;
}

async function interpretOneBatch(
  batch: InterpretItem[],
): Promise<z.infer<typeof InterpretResponseSchema>> {
  // Trim each org to at most 6 articles, most recent first, via the SAME `selectArticles`
  // the defaults use, so the prompt and the overlay slots can never drift.
  const payload = batch.map((item) => ({
    recordId: item.recordId,
    entityName: item.entityName,
    articles: selectArticles(item).map((a, i) => ({
      index: i,
      title: a.title,
      url: a.url,
      date: a.observedAt ? a.observedAt.toISOString().slice(0, 10) : null,
    })),
  }));

  const messages: ChatMessage[] = [
    {
      role: "system",
      content:
        "You interpret short news headlines about investment firms. For each article " +
        "return a JSON object with recordId, index, kind, meaning, confidence. " +
        "kind is one of: capital_deployment, exit, personnel, mandate_shift, firm_news. " +
        "confidence is 'observed' when the headline itself states the event, 'inferred' " +
        "when the meaning is a read on it. meaning is one short sentence (max 200 chars) " +
        "or null. CRITICAL: never assert anything the headline does not support. If you " +
        "are not sure, return meaning: null. Return ONLY a JSON array, no prose.",
    },
    {
      role: "user",
      content:
        "Interpret these articles. Respond with a JSON array only.\n\n" +
        JSON.stringify(payload),
    },
  ];

  const raw = await ollamaChat(messages, "cheapest");
  const stripped = stripJsonFence(raw);
  const parsed = JSON.parse(stripped); // throws on unparseable → caller degrades
  return InterpretResponseSchema.parse(parsed); // throws on schema-invalid → degrade
}

function stripJsonFence(s: string): string {
  const trimmed = s.trim();
  // Tolerate a ```json ... ``` wrapper some instruction models add despite the prompt.
  const fence = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  if (fence) return fence[1].trim();
  return trimmed;
}