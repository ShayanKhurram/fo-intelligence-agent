"use client";

import { useState } from "react";
import type { QueryResponse } from "@/lib/types";
import { ProvenanceDrawer } from "./ProvenanceDrawer";

// Example queries — plan §7.2: "every one must return a real, good result on the live
// system. Test them last, after the data is frozen." These are placeholders until the
// eval pass (task: deploy + evaluate) verifies them against the live deployed system.
const EXAMPLE_QUERIES = [
  "Single-family offices with confirmed AUM over $500M",
  "Who are the named principals across these records?",
  "How many records are multi-family offices?",
  "Family offices with a recent dated activity signal",
];

export function SearchApp() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<QueryResponse | null>(null);
  const [openRecord, setOpenRecord] = useState<string | null>(null);

  async function runQuery(q: string) {
    setLoading(true);
    setResponse(null);
    try {
      const res = await fetch("/api/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q }),
      });
      const data = await res.json();
      setResponse(data);
    } catch {
      setResponse({ answer: "Couldn't complete that search. Try again.", records: [], error: true });
    } finally {
      setLoading(false);
    }
  }

  function renderAnswerWithTags(answer: string) {
    const parts = answer.split(/(\[[^\]:]+:[^\]]+\])/g);
    return parts.map((part, i) => {
      const m = part.match(/^\[([^\]:]+):([^\]]+)\]$/);
      if (m) {
        return (
          <button
            key={i}
            onClick={() => setOpenRecord(m[1])}
            className="mono text-xs text-[var(--live)] underline decoration-dotted"
            title="View evidence for this claim"
          >
            [{m[2]}]
          </button>
        );
      }
      return <span key={i}>{part}</span>;
    });
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-12">
      <header className="mb-8">
        <h1 className="mb-1 text-2xl font-semibold">FO Intelligence — micro-RAG</h1>
        <p className="text-[var(--ink-500)]">
          Private-wealth intelligence assembled from public obligation filings (13F, 990-PF, Form 5500, Form D).
          Ask about entity type, state, AUM, or investing mandate.
        </p>
      </header>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) runQuery(query);
        }}
        className="mb-4 flex gap-2"
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. SFOs in Texas over $200M doing growth equity"
          className="flex-1 rounded border border-[var(--rule)] bg-transparent px-4 py-2 focus:outline-2 focus:outline-[var(--live)]"
          aria-label="Search query"
        />
        <button
          type="submit"
          disabled={loading || !query.trim()}
          className="rounded bg-[var(--ink-900)] px-4 py-2 text-[var(--paper)] disabled:opacity-40"
        >
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      {!response && (
        <div className="flex flex-wrap gap-2">
          {EXAMPLE_QUERIES.map((q) => (
            <button
              key={q}
              onClick={() => {
                setQuery(q);
                runQuery(q);
              }}
              className="rounded-full border border-[var(--rule)] px-3 py-1 text-sm text-[var(--ink-500)] hover:text-[var(--ink-900)]"
            >
              {q}
            </button>
          ))}
        </div>
      )}

      {response && (
        <section aria-live="polite" className="mt-8">
          {response.relaxedFilters && response.relaxedFilters.length > 0 && (
            <div className="mb-4 flex flex-wrap gap-2">
              {response.relaxedFilters.map((f) => (
                <span key={f} className="mono rounded-full bg-[var(--partial)]/15 px-3 py-1 text-xs text-[var(--partial)]">
                  relaxed: {f}
                </span>
              ))}
            </div>
          )}

          <p className="mb-6 whitespace-pre-wrap leading-relaxed">{renderAnswerWithTags(response.answer)}</p>

          {response.records.length > 0 && (
            <ul className="space-y-2">
              {response.records.map((id) => (
                <li key={id}>
                  <button
                    onClick={() => setOpenRecord(id)}
                    className="mono w-full rounded border border-[var(--rule)] px-3 py-2 text-left text-sm hover:bg-[var(--ink-900)]/5"
                  >
                    {id}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {openRecord && <ProvenanceDrawer recordId={openRecord} onClose={() => setOpenRecord(null)} />}
    </div>
  );
}
