"use client";

import type { RecordRow } from "@/lib/types";
import { formatUsd, formatDate, statusTier } from "@/lib/format";
import { MailIcon, PhoneIcon } from "./icons";

function TypeBadge({ entityType }: { entityType: string }) {
  const label = entityType === "SFO" || entityType === "MFO" ? entityType : "TYPE UNCONFIRMED";
  const tone = entityType === "SFO" || entityType === "MFO" ? "text-[var(--text-mid)]" : "text-[var(--unknown)]";
  return <span className={`mono text-[10px] uppercase tracking-wide ${tone}`}>{label}</span>;
}

function ContactIcon({ Icon, status, value, label }: { Icon: typeof MailIcon; status: string; value: string | null; label: string }) {
  const tier = value ? statusTier(status) : "unknown";
  const title = !value ? `No ${label.toLowerCase()} on file` : tier === "confirmed" ? `${label}: ${value} (confirmed)` : `${label}: ${value} (not confirmed)`;
  return (
    <span
      className={value ? `status-${tier}` : "text-[var(--text-low)] opacity-50"}
      title={title}
      aria-label={title}
    >
      <Icon />
    </span>
  );
}

export function RecordCard({
  record,
  variant,
  onOpen,
  focused,
}: {
  record: RecordRow;
  variant: "rail" | "main";
  onOpen: (recordId: string) => void;
  focused?: boolean;
}) {
  const aum = formatUsd(record.aum_usd);
  const signalDate = formatDate(record.most_recent_signal_date);
  const isMain = variant === "main";

  return (
    <button
      id={`record-card-${record.record_id}`}
      onClick={() => onOpen(record.record_id)}
      className={`glass block w-full text-left transition-colors hover:bg-[var(--bg-glass-hi)] ${
        isMain ? "p-5" : "p-3"
      } ${focused ? "ring-2 ring-[var(--live)]" : ""}`}
    >
      <div className="mb-1.5 flex items-start justify-between gap-2">
        <h3 className={isMain ? "display text-lg" : "display text-sm"}>{record.entity_name}</h3>
        <span
          className={`mt-1 inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
            record.urgency_tier ? "bg-[var(--urgent)]" : "bg-[var(--unknown)]"
          }`}
          title={record.urgency_tier ? `Urgency: ${record.urgency_tier}` : "No urgency signal"}
          aria-hidden="true"
        />
      </div>
      <TypeBadge entityType={record.entity_type} />

      {record.principal_name && (
        <p className="mt-2 text-sm text-[var(--text-hi)]">
          {record.principal_name}
          {record.principal_title && <span className="text-[var(--text-mid)]"> · {record.principal_title}</span>}
        </p>
      )}

      {aum && (
        <p className="mono mt-1 text-xs text-[var(--text-mid)]">
          {aum}
          {record.aum_basis && <span className="text-[var(--text-low)]"> ({record.aum_basis})</span>}
        </p>
      )}

      {signalDate && (
        <p className="status-live mono mt-1.5 text-xs">
          activity {signalDate}
        </p>
      )}

      {isMain && record.mandates.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {record.mandates.slice(0, 6).map((m) => (
            <span key={m} className="mono rounded-[var(--r-pill)] border border-[var(--edge)] px-2 py-0.5 text-[10px] text-[var(--text-mid)]">
              {m}
            </span>
          ))}
        </div>
      )}

      {isMain && record.outreach_hook && (
        <p className="mt-3 border-l-2 border-[var(--live)]/40 pl-3 text-sm leading-relaxed text-[var(--text-hi)] italic">
          &ldquo;{record.outreach_hook}&rdquo;
        </p>
      )}

      <div className="mt-3 flex items-center gap-3">
        <ContactIcon Icon={MailIcon} status={record.principal_email_status} value={record.principal_email} label="Email" />
        <ContactIcon Icon={PhoneIcon} status={record.principal_phone_status} value={record.principal_phone} label="Phone" />
      </div>
    </button>
  );
}

export function RecordCardSkeleton({ variant }: { variant: "rail" | "main" }) {
  const isMain = variant === "main";
  return (
    <div className={`skeleton ${isMain ? "h-40" : "h-28"}`} aria-hidden="true" />
  );
}
