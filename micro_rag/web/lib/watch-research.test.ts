// T46.2 — watch-research.ts acceptance. Run with `npm test` (node --test, zero deps).
// No network: every test exercises the pure functions only.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  normalizeEntityName,
  parseArticleDate,
  acceptArticle,
  dedupeKey,
  selectArticles,
  interpretBatch,
  isExhaustion,
  searchActivity,
  type InterpretItem,
  type Article,
  type Interpretation,
} from "./watch-research.ts";

// ---------------------------------------------------------------------------
// normalizeEntityName — the four real names the accept rule was designed against.
// ---------------------------------------------------------------------------

test("normalizeEntityName: THE COURY FIRM keeps all tokens in phrase but only 2 are significant", () => {
  const n = normalizeEntityName("THE COURY FIRM");
  assert.equal(n.phrase, "the coury firm");
  assert.deepEqual(n.corporate, []);
  // The leading article 'the' stays in `phrase` (it is part of the name and must be
  // matched contiguously) but does NOT count toward significance — D4.
  assert.deepEqual(n.tokens, ["coury", "firm"]);
});

test("normalizeEntityName: DIAMANT ASSET MANAGEMENT, INC. strips only 'inc'", () => {
  const n = normalizeEntityName("DIAMANT ASSET MANAGEMENT, INC.");
  assert.equal(n.phrase, "diamant asset management");
  assert.deepEqual(n.corporate, ["inc"]);
  assert.deepEqual(n.tokens, ["diamant", "asset", "management"]);
});

test("normalizeEntityName: REAP FINANCIAL GROUP, LLC strips 'llc' then 'group' → 'reap financial' (2 tokens)", () => {
  // PLAN.md T46 constraint 4: this entity normalizes to `reap financial`, which is what
  // makes the two live reject strings exercise the marker/3-token gate rather than
  // failing earlier on a missing phrase.
  const n = normalizeEntityName("REAP FINANCIAL GROUP, LLC");
  assert.equal(n.phrase, "reap financial");
  assert.deepEqual(n.corporate, ["group", "llc"]);
  assert.deepEqual(n.tokens, ["reap", "financial"]);
});

test("normalizeEntityName: TURTLE CREEK WEALTH ADVISORS, LLC strips only 'llc'", () => {
  const n = normalizeEntityName("TURTLE CREEK WEALTH ADVISORS, LLC");
  assert.equal(n.phrase, "turtle creek wealth advisors");
  assert.deepEqual(n.corporate, ["llc"]);
  assert.deepEqual(n.tokens, ["turtle", "creek", "wealth", "advisors"]);
});

// ---------------------------------------------------------------------------
// parseArticleDate — never fabricates a date; unparseable → null.
// ---------------------------------------------------------------------------

test("parseArticleDate: '23 hours ago' is now minus 23h", () => {
  const now = new Date("2026-08-17T12:00:00Z");
  const d = parseArticleDate("23 hours ago", now);
  assert.ok(d, "expected a date");
  const expected = new Date("2026-08-16T13:00:00Z");
  assert.equal(d!.toISOString(), expected.toISOString());
});

test("parseArticleDate: '3 weeks ago' is now minus 21 days", () => {
  const now = new Date("2026-08-17T12:00:00Z");
  const d = parseArticleDate("3 weeks ago", now);
  assert.ok(d);
  const expected = new Date("2026-07-27T12:00:00Z");
  assert.equal(d!.toISOString(), expected.toISOString());
});

test("parseArticleDate: 'Jun 10, 2026' parses to 2026-06-10", () => {
  const d = parseArticleDate("Jun 10, 2026", new Date());
  assert.ok(d);
  assert.equal(d!.toISOString(), "2026-06-10T00:00:00.000Z");
});

test("parseArticleDate: GDELT '20260620T120000Z' parses to 2026-06-20T12:00:00Z", () => {
  const d = parseArticleDate("20260620T120000Z", new Date());
  assert.ok(d);
  assert.equal(d!.toISOString(), "2026-06-20T12:00:00.000Z");
});

test("parseArticleDate: 'garbage' returns null (never now)", () => {
  const now = new Date();
  const d = parseArticleDate("garbage", now);
  assert.equal(d, null);
});

test("parseArticleDate: null/blank input returns null", () => {
  assert.equal(parseArticleDate(null, new Date()), null);
  assert.equal(parseArticleDate("   ", new Date()), null);
});

// ---------------------------------------------------------------------------
// acceptArticle — the five real cases, in the fixed reject order.
// ---------------------------------------------------------------------------

const NOW = new Date("2026-08-17T12:00:00Z");
const LOOKBACK = 90;

// A non-blocked URL with a neutral slug, so undated/stale/blocked_domain all pass and the
// name test is what decides.
const ARTICLE_URL = "https://www.businesswire.com/news/home/2026081500500/en/story.html";

function article(title: string, url: string, observedAt: Date | null) {
  return { title, url, observedAt };
}

test("acceptArticle: REAP vs 'reap financial rewards' → reject name_not_matched", () => {
  const n = normalizeEntityName("REAP FINANCIAL GROUP, LLC");
  // phrase 'reap financial' IS present, but the next token 'rewards' is not a marker and
  // there are only 2 significant tokens → reject.
  const r = acceptArticle(
    article(
      "Strong corporate social impact programs reap financial rewards: ACCP",
      ARTICLE_URL,
      NOW,
    ),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, false);
  assert.equal((r as { ok: false; reason: string }).reason, "name_not_matched");
});

test("acceptArticle: REAP vs 'Reap Financial Windfall' → reject name_not_matched", () => {
  const n = normalizeEntityName("REAP FINANCIAL GROUP, LLC");
  const r = acceptArticle(
    article(
      "Mideast Accord: Iran Set to Reap Financial Windfall in US Peace Deal",
      ARTICLE_URL,
      NOW,
    ),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, false);
  assert.equal((r as { ok: false; reason: string }).reason, "name_not_matched");
});

test("acceptArticle: TURTLE CREEK WEALTH ADVISORS named in headline → accept", () => {
  const n = normalizeEntityName("TURTLE CREEK WEALTH ADVISORS, LLC");
  const r = acceptArticle(
    article(
      "McDonald's Corporation $MCD Shares Purchased by Turtle Creek Wealth Advisors LLC",
      ARTICLE_URL,
      NOW,
    ),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, true);
});

test("acceptArticle: DIAMANT vs a Jupiter Asset Management headline → reject name_not_matched", () => {
  const n = normalizeEntityName("DIAMANT ASSET MANAGEMENT, INC.");
  const r = acceptArticle(
    article(
      "Fund Update: New $87.9M $PEP stock position opened by JUPITER ASSET MANAGEMENT",
      ARTICLE_URL,
      NOW,
    ),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, false);
  assert.equal((r as { ok: false; reason: string }).reason, "name_not_matched");
});

test("acceptArticle: HUFFMAN FAMILY OFFICE on instagram.com → reject blocked_domain (before the name test)", () => {
  const n = normalizeEntityName("HUFFMAN FAMILY OFFICE");
  const r = acceptArticle(
    article(
      "HUFFMAN FAMILY OFFICE posts about their latest investments",
      "https://instagram.com/p/CxYz123/",
      NOW,
    ),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, false);
  // The name would actually match (3 tokens), so confirming blocked_domain proves the
  // domain check runs BEFORE the name test.
  assert.equal((r as { ok: false; reason: string }).reason, "blocked_domain");
});

test("acceptArticle: undated article → reject undated (before stale/blocked/name)", () => {
  const n = normalizeEntityName("TURTLE CREEK WEALTH ADVISORS, LLC");
  const r = acceptArticle(
    article("Turtle Creek Wealth Advisors LLC makes a move", ARTICLE_URL, null),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, false);
  assert.equal((r as { ok: false; reason: string }).reason, "undated");
});

test("acceptArticle: article older than lookback → reject stale", () => {
  const n = normalizeEntityName("TURTLE CREEK WEALTH ADVISORS, LLC");
  const old = new Date(NOW.getTime() - (LOOKBACK + 5) * 24 * 60 * 60 * 1000);
  const r = acceptArticle(
    article("Turtle Creek Wealth Advisors LLC makes a move", ARTICLE_URL, old),
    n,
    { lookbackDays: LOOKBACK, now: NOW },
  );
  assert.equal(r.ok, false);
  assert.equal((r as { ok: false; reason: string }).reason, "stale");
});

// ---------------------------------------------------------------------------
// dedupeKey — stable across query string, fragment, and trailing slash.
// ---------------------------------------------------------------------------

test("dedupeKey: identical for /foo/, /foo?utm_source=x, /foo#frag, and www host variant", () => {
  const a = dedupeKey("https://example.com/foo/");
  const b = dedupeKey("https://example.com/foo?utm_source=x");
  const c = dedupeKey("https://example.com/foo#section");
  const d = dedupeKey("https://www.example.com/foo");
  assert.equal(a, b);
  assert.equal(a, c);
  assert.equal(a, d);
  assert.ok(a.length === 40, "sha1 hex is 40 chars");
});

test("dedupeKey: different paths produce different keys", () => {
  const a = dedupeKey("https://example.com/foo");
  const b = dedupeKey("https://example.com/bar");
  assert.notEqual(a, b);
});

test("dedupeKey: is deterministic (same input → same key across calls)", () => {
  const a = dedupeKey("https://example.com/foo?utm_source=x&ref=tw");
  const b = dedupeKey("https://example.com/foo/?utm_source=x&ref=tw");
  assert.equal(a, b);
});

// ---------------------------------------------------------------------------
// D1 — interpretBatch must not drop model interpretations when an org has >6 articles.
// The defaults slots and the prompt must use the SAME recency-sorted, 6-item list.
// ---------------------------------------------------------------------------

function makeArticle(title: string, url: string, daysAgo: number): Article {
  const observedAt = new Date(NOW.getTime() - daysAgo * 24 * 60 * 60 * 1000);
  return { title, url, source: "test", dateRaw: null, observedAt };
}

test("selectArticles: returns the 6 freshest in recency order, even when they sit at the END of the input array", () => {
  // 10 articles; the freshest (daysAgo 0..5) are placed at indices 4..9 — the END of
  // the array. The oldest (daysAgo 9) is at index 0. A naive `slice(0, 6)` would take
  // the oldest six; the recency sort must take the freshest six instead.
  const articles: Article[] = [
    makeArticle("old0", "https://x.com/a0", 9),
    makeArticle("old1", "https://x.com/a1", 8),
    makeArticle("old2", "https://x.com/a2", 7),
    makeArticle("old3", "https://x.com/a3", 6),
    makeArticle("fresh5", "https://x.com/a5", 5),
    makeArticle("fresh4", "https://x.com/a4", 4),
    makeArticle("fresh3", "https://x.com/a3b", 3),
    makeArticle("fresh2", "https://x.com/a2b", 2),
    makeArticle("fresh1", "https://x.com/a1b", 1),
    makeArticle("fresh0", "https://x.com/a0b", 0),
  ];
  const item: InterpretItem = { recordId: "rec", entityName: "X", articles };
  const selected = selectArticles(item);
  assert.equal(selected.length, 6);
  // Freshest first.
  assert.deepEqual(
    selected.map((a) => a.title),
    ["fresh0", "fresh1", "fresh2", "fresh3", "fresh4", "fresh5"],
  );
});

test("interpretBatch: every article the prompt would see has a matching slot in the returned map (D1)", async () => {
  // Same 10-article org, freshest at the end. interpretBatch degrades to defaults when
  // the LLM call fails (no OLLAMA_API_KEY here), so the returned slots are exactly the
  // defaults — which must be built from the SAME selectArticles list the prompt uses.
  // Under the round-1 bug the defaults came from `item.articles.slice(0,6)` (original
  // order = the OLDEST six), while the prompt saw the FRESHEST six, so four of the
  // prompt's articles had no slot. This test fails against that code.
  const articles: Article[] = [
    makeArticle("old0", "https://x.com/a0", 9),
    makeArticle("old1", "https://x.com/a1", 8),
    makeArticle("old2", "https://x.com/a2", 7),
    makeArticle("old3", "https://x.com/a3", 6),
    makeArticle("fresh5", "https://x.com/a5", 5),
    makeArticle("fresh4", "https://x.com/a4", 4),
    makeArticle("fresh3", "https://x.com/a3b", 3),
    makeArticle("fresh2", "https://x.com/a2b", 2),
    makeArticle("fresh1", "https://x.com/a1b", 1),
    makeArticle("fresh0", "https://x.com/a0b", 0),
  ];
  const item: InterpretItem = { recordId: "rec_d1", entityName: "X", articles };

  // No network in tests: stub fetch so ollamaChat fails fast instead of retrying against
  // the real Ollama Cloud endpoint for ~7s (it would eventually throw and degrade to
  // defaults anyway, but a unit test must not depend on the network or burn that time).
  const origFetch = globalThis.fetch;
  globalThis.fetch = (async () => {
    throw new Error("no network in tests");
  }) as typeof fetch;
  let map: Map<string, Interpretation[]>;
  try {
    map = await interpretBatch([item]);
  } finally {
    globalThis.fetch = origFetch;
  }
  const slots = map.get("rec_d1");
  assert.ok(slots, "expected a defaults entry for rec_d1");

  // Every article the prompt would see (selectArticles) must have a matching slot.
  const selectedKeys = new Set(selectArticles(item).map((a) => dedupeKey(a.url)));
  const slotKeys = new Set(slots!.map((s) => s.dedupeKey));
  for (const k of selectedKeys) {
    assert.ok(slotKeys.has(k), `prompt article ${k} has no slot in returned map`);
  }
  // And the slot set must equal the selected set exactly (no stale oldest-article slots).
  assert.equal(slots!.length, selectedKeys.size);
});

// ---------------------------------------------------------------------------
// D3 — a transient Serper 429 must NOT abort the whole run as credits exhausted.
// ---------------------------------------------------------------------------

test("isExhaustion: (400, 'Not enough credits') → true (spent balance reported as 400)", () => {
  assert.equal(isExhaustion(400, '{"message":"Not enough credits"}'), true);
});

test("isExhaustion: (429, 'slow down') → false (a 429 is ordinary throttling, not a spent balance)", () => {
  // A bare 429 must NOT count as exhaustion — the caller maps it to `rate_limited` so
  // the run backs off and continues instead of dying.
  assert.equal(isExhaustion(429, "slow down"), false);
});

test("searchActivity (serper): a 400 'Not enough credits' surfaces as credits_exhausted", async () => {
  const origFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response('{"message":"Not enough credits"}', { status: 400 })) as typeof fetch;
  try {
    const r = await searchActivity("Some Firm LLC", {
      lookbackDays: 90,
      backend: "serper",
      key: "fake-key",
    });
    assert.equal(r.error, "credits_exhausted");
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("searchActivity (serper): a 429 'slow down' surfaces as rate_limited, not credits_exhausted", async () => {
  const origFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response("slow down", { status: 429 })) as typeof fetch;
  try {
    const r = await searchActivity("Some Firm LLC", {
      lookbackDays: 90,
      backend: "serper",
      key: "fake-key",
    });
    assert.equal(r.error, "rate_limited");
  } finally {
    globalThis.fetch = origFetch;
  }
});