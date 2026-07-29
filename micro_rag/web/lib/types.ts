export type QueryResponse = {
  answer: string;
  records: string[];
  count?: number;
  relaxedFilters?: string[];
  strippedFraction?: number;
  declined?: boolean;
  error?: boolean;
};

// ui_plan.md §9 — the SSE contract. The client renders each event type independently;
// this union is the source of truth both the route (producer) and SearchApp (consumer)
// import, so the two ends can't drift out of sync on shape.
import type { ParsedFilters } from "./query-understanding";

export type FilterChip = { key: string; label: string };

export type ClaimVerifiedPayload = {
  sentence: string;
  recordId: string;
  field: string;
  value: string;
  status: string;
};

export type StageId = "understanding" | "filtering" | "matching" | "checking";
export type StageStatus = "active" | "done";

export type QueryStreamEvent =
  | { type: "stage"; id: StageId; label: string; status: StageStatus; detail?: string }
  // parsedFilters is the authoritative structured state — chips are a lossy display
  // projection of it (e.g. aum_min's raw number vs its "$500M" label). The client keeps
  // parsedFilters as its own state so a LATER chip removal (deleting one key and
  // resubmitting) starts from what the server actually parsed, not an empty object.
  | { type: "filters"; filters: FilterChip[]; parsedFilters: ParsedFilters }
  | { type: "records"; records: RecordRow[]; candidateCount: number }
  | { type: "token"; text: string; kind: "claim" | "neutral" }
  | { type: "claim_verified"; claim: ClaimVerifiedPayload }
  | {
      type: "done";
      records: string[];
      relaxedFilters: string[];
      strippedFraction?: number;
      declined?: boolean;
      discarded?: boolean;
      count?: number;
      finalAnswerFallback?: string; // used only for declined/aggregate/discarded paths that never streamed sentence tokens
      error?: boolean;
    };

export type RecordRow = {
  record_id: string;
  entity_name: string;
  entity_type: string;
  hq_state: string | null;
  hq_country: string | null;
  aum_usd: number | null;
  aum_basis: string | null;
  aum_as_of: string | null;
  mandates: string[];
  fit_tags: string[];
  check_size_min: number | null;
  check_size_max: number | null;
  principal_name: string | null;
  principal_title: string | null;
  principal_email: string | null;
  principal_email_status: string;
  principal_phone: string | null;
  principal_phone_status: string;
  most_recent_signal_date: string | null;
  urgency_tier: string | null;
  record_confidence: string;
  outcome: string;
  outreach_hook: string | null; // provenance-only field, not a records column — see lib/records.ts
};

export type ProvenanceRow = {
  field_name: string;
  value: string | null;
  source_url: string | null;
  source_class: string | null;
  extraction_method: string | null;
  retrieved_at: string | null;
  verification_method: string | null;
  confirming_url: string | null;
  confirming_class: string | null;
  status: string;
  confidence: string | null;
};

export function statusClass(status: string): string {
  if (status === "verified") return "status-confirmed";
  if (["single_source", "confirmed", "format_only", "pattern_inferred"].includes(status)) return "status-partial";
  return "status-unknown";
}

export function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    verified: "confirmed",
    single_source: "confirmed (single source)",
    confirmed: "confirmed",
    format_only: "format checked",
    pattern_inferred: "pattern inferred, not verified",
    could_not_verify: "not confirmed",
    removed_failed_validation: "not confirmed",
    contradicted: "not confirmed",
  };
  return labels[status] ?? status;
}
