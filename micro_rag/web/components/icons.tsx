// Small inline icon set — no external icon library (ui_plan.md's implementation notes
// call for zero external CDN dependencies; this keeps the same discipline for icons).
export function MailIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" className={className} aria-hidden="true">
      <rect x="1.5" y="3.5" width="13" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
      <path d="M2 4.5L8 9L14 4.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function PhoneIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" className={className} aria-hidden="true">
      <path
        d="M3.5 2h2l1 3-1.5 1.2a8 8 0 0 0 4.8 4.8L11 9.5l3 1v2a1.5 1.5 0 0 1-1.6 1.5C6.9 13.6 2.4 9.1 2 3.6A1.5 1.5 0 0 1 3.5 2Z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function ChevronIcon({ className, open }: { className?: string; open?: boolean }) {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      fill="none"
      className={className}
      style={{ transform: open ? "rotate(180deg)" : undefined, transition: "transform 180ms ease-out" }}
      aria-hidden="true"
    >
      <path d="M2.5 4.5L6 8L9.5 4.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function CloseIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 14 14" width="14" height="14" fill="none" className={className} aria-hidden="true">
      <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function ShareIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" className={className} aria-hidden="true">
      <circle cx="12.5" cy="3.5" r="1.8" stroke="currentColor" strokeWidth="1.2" />
      <circle cx="3.5" cy="8" r="1.8" stroke="currentColor" strokeWidth="1.2" />
      <circle cx="12.5" cy="12.5" r="1.8" stroke="currentColor" strokeWidth="1.2" />
      <path d="M5 7L11 4.3M5 9L11 11.7" stroke="currentColor" strokeWidth="1.1" />
    </svg>
  );
}

export function SendIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" className={className} aria-hidden="true">
      <path d="M8 12.5V3.5M8 3.5L4 7.5M8 3.5L12 7.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function FilterIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" className={className} aria-hidden="true">
      <path d="M2 3.5h12M4.5 8h7M7 12.5h2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

// ---- T47 additions: the shell's navigation and the composer's stop control ----

export function MenuIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="17" height="17" fill="none" className={className} aria-hidden="true">
      <path d="M2 4h12M2 8h12M2 12h12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function PlusIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" className={className} aria-hidden="true">
      <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function AskIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" className={className} aria-hidden="true">
      <circle cx="7" cy="7" r="4.4" stroke="currentColor" strokeWidth="1.3" />
      <path d="M10.4 10.4L14 14" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

export function WatchIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" className={className} aria-hidden="true">
      <path d="M1.5 8s2.4-4.2 6.5-4.2S14.5 8 14.5 8s-2.4 4.2-6.5 4.2S1.5 8 1.5 8Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <circle cx="8" cy="8" r="1.8" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

export function LogIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" fill="none" className={className} aria-hidden="true">
      <rect x="2.5" y="2" width="11" height="12" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
      <path d="M5 5.5h6M5 8h6M5 10.5h3.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

export function StopIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" className={className} aria-hidden="true">
      <rect x="4" y="4" width="8" height="8" rx="1.4" />
    </svg>
  );
}
