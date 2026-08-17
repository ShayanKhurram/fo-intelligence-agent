import { Pool, types } from "pg";

// node-postgres parses a `DATE` column into a JS `Date` at LOCAL midnight, not UTC. In a
// positive-offset timezone (this host is GMT+0500) `Date#toISOString().slice(0,10)` then
// shifts the calendar date BACK a day — so a stored 2026-08-17 read back as 2026-08-16.
// That silently corrupted the Intent Watcher board's research-signal dates (and left
// `records.most_recent_signal_date` unparseable by plan-rank's ISO regex, scoring recency
// 0 for every dated record). Return `DATE` as the raw "YYYY-MM-DD" string instead; every
// consumer already expects a string (`toIsoDate` does `new Date("2026-08-17")` → UTC midnight
// → correct, and plan-rank's `toDayNumber` matches `/^\d{4}-\d{2}-\d{2}/`).
types.setTypeParser(1082, (val: string) => val);

// One pooled connection, server-side only (this module is never imported by a
// client component) — the UI never holds a DB credential, per micro_rag_plan.md §2's
// "never touches DB" separation, now enforced by Next.js's server/client boundary
// instead of a separate deployed service.
let pool: Pool | null = null;

export function getPool(): Pool {
  if (!pool) {
    const connectionString = process.env.DATABASE_URL || process.env.POSTGRES_URL;
    if (!connectionString) {
      throw new Error("DATABASE_URL/POSTGRES_URL not set — provision Postgres via the Vercel Marketplace (Neon) first");
    }
    // SSL only for managed/remote Postgres (Neon et al. require it). A local Postgres
    // (localhost / 127.0.0.1, e.g. the Docker pgvector used for end-to-end testing)
    // doesn't speak SSL, so forcing it there fails the handshake. Opt out via an explicit
    // sslmode=disable in the DSN, or automatically for a localhost host.
    const isLocal = /(^|@)(localhost|127\.0\.0\.1)[:/]/.test(connectionString)
      || /sslmode=disable/.test(connectionString);
    pool = new Pool({
      connectionString,
      max: 5,
      ssl: isLocal ? false : { rejectUnauthorized: false },
    });
  }
  return pool;
}
