export type QueryResponse = {
  answer: string;
  records: string[];
  count?: number;
  relaxedFilters?: string[];
  strippedFraction?: number;
  declined?: boolean;
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
