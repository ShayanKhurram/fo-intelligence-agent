# UI Implementation Plan — Dark Glass

Supersedes §7 of `micro_rag_plan.md`. Architecture, retrieval, and grounding gates are
unchanged; this replaces the presentation direction.

Reference shape: the answer-engine layout — query at top, streaming answer with inline
citation pills, sources rail on the right, floating input at the bottom. We keep that
skeleton because it is genuinely good and instantly legible to a non-technical user, and
we change what fills it, because our citations point at **records and fields with an
evidence chain**, not at web pages.

---

## 1 · The idea that keeps this from being generic dark mode

Dark + glass is a well-worn look. What stops it reading as a template here is that
**colour carries meaning instead of decoration.**

Every value in this dataset has a verification status. The interface renders that status
as visual weight: confirmed values sit at full contrast, unconfirmed ones are desaturated
and quieter, and things we couldn't verify are grey — the absence of colour standing in
for the absence of confidence. A thin record *looks* thin before you read a word of it.

That's the one real risk in this design: it makes some of our own data look weak. It's
also the honest thing, it's the thing being graded, and it turns epistemic rigour into
something the user feels in half a second.

The second grounding move: this dataset came out of **filings** — 13F, 990-PF, 5500,
Form D. So identifiers, dates, form numbers, and the entire evidence trail render in
monospace. It's not a retro affectation; those are machine records and they should look
like machine records.

---

## 2 · Tokens

```css
/* surfaces — cool slate, not neutral black */
--bg-base:        #0D1014;
--bg-raised:      #141A21;
--bg-glass:       rgba(26, 33, 42, 0.68);
--bg-glass-hi:    rgba(34, 43, 54, 0.78);
--scrim:          rgba(13, 16, 20, 0.55);   /* under text on glass */

--edge:           rgba(255, 255, 255, 0.08);
--edge-soft:      rgba(255, 255, 255, 0.05);
--edge-lit:       rgba(255, 255, 255, 0.14); /* top edge only, 1px */

/* type */
--text-hi:        #E9EDF2;
--text-mid:       #98A3B2;
--text-low:       #67717F;

/* functional verification palette — never used decoratively */
--confirmed:      #2FB88A;   /* jade */
--partial:        #C8912F;   /* brass */
--unknown:        #67717F;   /* grey = no confidence */
--live:           #5B9CFF;   /* dated activity signal */
--urgent:         #E8734A;   /* hot trigger — max 1 per card */

/* radii + elevation */
--r-sm: 8px;  --r-md: 12px;  --r-lg: 18px;  --r-pill: 999px;
--shadow-drawer: 0 24px 64px rgba(0,0,0,0.55);
```

**Type.** Display in a grotesk with actual character — Instrument Sans, General Sans, or
Bricolage Grotesque; pick one and commit. Body in Inter at 15px, line-height 1.65, measure
capped at 68ch. Utility in Geist Mono or JetBrains Mono for record IDs, form numbers,
dates, and the full evidence chain.

**Dark-mode weight correction:** type reads heavier on dark backgrounds. Run body at 400
where you'd use 450 on light, and headings at 550 rather than 600. Ignoring this is the
most common reason dark UIs feel muddy.

---

## 3 · Glass rules

Frosted surfaces are the reason this feels alive rather than flat, and also the fastest
way to make it slow and unreadable. Constraints:

- **Maximum three blurred surfaces on screen at once.** Here: the records rail, the input
  bar, and the drawer when open. Nothing else gets blur.
- `backdrop-filter: blur(20px) saturate(140%)` plus a semi-opaque fill. **Never put text
  directly on blur alone** — always a fill or scrim behind it, or contrast collapses over
  varying content.
- One 1px top edge at `--edge-lit`, sides and bottom at `--edge`. This single detail is
  what reads as "glass" rather than "translucent rectangle."
- **No blur on scrolling content.** Blurred surfaces are fixed or absolute; the list
  scrolls *behind* them.
- Provide a solid fallback via `@supports not (backdrop-filter: blur(1px))`.

---

## 4 · Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  tabs:  Answer · Records · Evidence            ·· ·   [ Share ]  │
├───────────────────────────────────────┬──────────────────────────┤
│                                       │  ╭ glass ──────────────╮ │
│              [ query chip, right ]    │  │ RECORDS          ▾  │ │
│                                       │  │ ┌─────────────────┐ │ │
│  ◇ stage strip (glass, collapses)     │  │ │ ● Cascade Point │ │ │
│                                       │  │ │   SFO · $310M   │ │ │
│  Answer streams here, with inline     │  │ │   J. Reyes, CIO │ │ │
│  claim pills appearing as each        │  │ └─────────────────┘ │ │
│  sentence clears the entailment       │  │ ┌─────────────────┐ │ │
│  gate. ⟨Cascade Point · AUM⟩          │  │ │ ● Marlowe Hldgs │ │ │
│                                       │  │ └─────────────────┘ │ │
│  ── record cards ──                   │  ╰─────────────────────╯ │
│                                       │                          │
├───────────────────────────────────────┴──────────────────────────┤
│   ╭ glass ─────────────────────────────────────────────────────╮ │
│   │  Ask about a family office…              [filters] [ ↑ ]   │ │
│   ╰────────────────────────────────────────────────────────────╯ │
└──────────────────────────────────────────────────────────────────┘
```

Main column ~62%, rail ~38%, single column below 900px with the rail becoming a
bottom sheet.

Tabs map to our product, not to a search engine: **Answer** (generated response + record
cards), **Records** (the full retrieved set as a table), **Evidence** (every claim in the
answer with its full provenance chain, flat).

That third tab is worth building. It's the audit view, it takes an hour, and it directly
demonstrates the thing being graded.

---

## 5 · Loading choreography

This is where "not dull" gets earned, and it's also an opportunity most people waste on a
spinner.

**Show the real pipeline.** The stages below are the actual retrieval steps, so the
loading state doubles as a demonstration of structured-plus-semantic retrieval — a graded
requirement — rather than being theatre.

| Stage | Label | What renders | Typical |
|---|---|---|---|
| 1 | Reading your question | parsed filter chips animate in one by one | 200–500ms |
| 2 | Filtering 50 records | a live count ticking down: 50 → 23 → 8 | 100–300ms |
| 3 | Matching mandate and activity | skeleton cards shimmer into the rail | 300–800ms |
| 4 | Checking evidence | answer begins streaming | — |

**Behaviour.** The stage strip is a glass panel that appears immediately on submit.
Completed stages collapse to a small jade check and shrink upward; the active stage carries
a slow 1.4s pulse on its leading dot. When streaming begins, the whole strip collapses to a
single summary line — "4 records · 12 fields" — which stays clickable to re-expand.

**Skeletons, not spinners.** Rail cards render as glass skeletons at the right dimensions
with a diagonal shimmer sweep (2.2s, 12% white, `ease-in-out`). The layout never jumps when
real content lands.

**The detail worth getting right:** claim pills do **not** appear as text streams. A
sentence streams plain, and its pill scales in (140ms, `ease-out`, 0.92 → 1) only once that
sentence has cleared the entailment gate. The user watches claims get certified in real
time. That is the grounding control made visible, and it costs nothing extra because the
gate already runs per-sentence.

**Never** show a bare spinner, a progress bar with fake percentages, or a stage label that
doesn't correspond to work actually happening.

---

## 6 · Components

**Claim pill** — inline, `--r-pill`, mono at 11px, height 20px. Contains a status dot,
truncated record name, and field. Border in the status colour at 30% alpha, fill at 8%.
Hover raises fill to 14% and shows a tooltip with the value and its date. Click opens the
evidence drawer. Confirmed pills carry colour; unknown pills are grey and slightly
transparent — visibly weaker, on purpose.

**Record card (rail)** — glass, `--r-md`. Leading status dot for urgency tier. Entity name
in display face, type badge (`SFO` / `MFO` / `TYPE UNCONFIRMED`) in mono uppercase at 10px.
Then principal name and title, AUM with its basis in mono, and the why-now line in
`--live`. Contact icons at the bottom, tinted by verification status — a grey mail icon
means we have an address we couldn't confirm, and that reads instantly.

**Record card (main column)** — wider, same information, plus mandate tags and the
outreach hook rendered as a pull-quote. This is the thing a fund manager actually reads
before dialling, so it gets the most typographic care of anything in the app.

**Evidence drawer** — the signature. Slides from the right at 420px, glass at
`--bg-glass-hi`, `--shadow-drawer`, 240ms `cubic-bezier(0.32, 0.72, 0, 1)`. Contents
render as a filing record in mono:

```
CLAIM      Manages ≈ $310M
FIELD      aum_usd · basis: 13F floor (public equities only)
SOURCE     SEC FORM 13F-HR · CIK 0001234567 · filed 2026-05-14
CONFIRM    press · reuters.com/… · retrieved 2026-07-02
CLASS      edgar_13f → news
STATUS     ● confirmed
```

The `CLASS` line showing origin → confirming class is the product's whole thesis in one
row: confirmed by something other than what found it.

**Filter chips** — parsed from the query, removable, mono labels. Removing one re-runs the
search. This is how a non-technical user loosens a query that returned nothing without
rewriting it.

**Input bar** — fixed, glass, `--r-lg`. Placeholder is specific: "Ask about a family
office…". Left slot for a filter toggle, right for submit. Focus ring in `--live` at 40%.

**Empty / declined states** — designed, not fallbacks. Centred, generous space, one line
explaining what happened, and two clickable example queries that are known to work. Never
an apology, never a stack trace.

---

## 7 · Motion

- Entrances 180–240ms `ease-out`; exits 140ms `ease-in`
- Stage transitions 300ms; drawer 240ms `cubic-bezier(0.32,0.72,0,1)`
- Shimmer 2.2s linear infinite
- Streaming cursor: 2px jade bar, 1s blink
- `prefers-reduced-motion` → shimmer becomes a static 8% fill, stages snap, drawer
  cross-fades, cursor stops blinking

Nothing animates on scroll. Nothing bounces.

---

## 8 · Accessibility

Dark plus glass is where contrast quietly dies. Non-negotiables:

- Body text on any glass surface ≥ 4.5:1 **measured over the busiest content that can sit
  behind it**, not over the flat fill. Use the scrim to guarantee it.
- Status is never colour alone: confirmed/partial/unknown each carry a distinct glyph
  (● / ◐ / ○) alongside the colour.
- Focus rings visible on glass — 2px `--live` with a 1px dark inner ring so they read on
  both light and dark content behind.
- Streaming answer wrapped in `aria-live="polite"`; stage strip in `aria-live="polite"`
  too, but only announcing stage changes, not tick counts.
- Drawer traps focus, closes on Escape, returns focus to the pill that opened it.
- Full keyboard path: `/` focuses input, `↑↓` moves the rail, `Enter` opens a record,
  `Esc` closes the drawer.

---

## 9 · Implementation notes

- Next.js App Router, Tailwind with the tokens above as CSS variables in
  `@theme`/`:root`, `shadcn/ui` for the drawer and tooltip primitives only.
- Streaming via SSE from `/query`. The response is a **typed event stream**, not raw text:
  `stage`, `filters`, `records`, `token`, `claim_verified`, `done`. The client renders
  each event type independently, which is what makes the pill-certification effect possible
  without hacks.
- Contrast-check the glass surfaces in CI if you can — a script that renders the three
  panels over worst-case backgrounds and asserts ratios is 30 minutes and prevents the
  most likely regression.
- Test on a low-end laptop. `backdrop-filter` is GPU-cheap on Apple silicon and expensive
  almost everywhere else; if the drawer stutters, drop blur radius to 12px before dropping
  the effect.

---

## 10 · Build order

| Step | Deliverable |
|---|---|
| 1 | Tokens, type scale, glass utility classes, contrast harness |
| 2 | Static shell: tabs, main column, rail, input bar — no data |
| 3 | SSE event stream from the API with all six event types |
| 4 | Stage strip with real stage events + collapse behaviour |
| 5 | Skeletons + shimmer in the rail |
| 6 | Answer streaming + claim pills gated on `claim_verified` |
| 7 | Record cards, both variants |
| 8 | **Evidence drawer** — give this real time, it's the signature |
| 9 | Evidence tab (flat audit view) |
| 10 | Empty, declined, partial, and error states |
| 11 | Accessibility pass; reduced-motion; keyboard paths |
| 12 | Example queries written **last**, verified against the frozen live system |

Step 12 stays last for the same reason as before: every visible word is an audited claim,
and an example query that returns nothing on the live URL is a claim the data doesn't
support.
