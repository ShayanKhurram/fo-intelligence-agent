import { NextResponse } from "next/server";
import { getPool } from "@/lib/db";

// Cheap, but proves the deployed system is serving the same file that was submitted
// (plan §9): returns the dataset build hash + record count.
export async function GET() {
  try {
    const pool = getPool();
    const { rows } = await pool.query(
      `SELECT build_hash, record_count, built_at FROM dataset_meta ORDER BY built_at DESC LIMIT 1`
    );
    // record_count reflects the LIVE total across all sources (pipeline + any CSV import),
    // not just the last ingest's dataset_meta row — the two ingest paths write independent
    // dataset_meta rows, so the latest one alone under-counts a co-resident dataset.
    const { rows: countRows } = await pool.query(`SELECT COUNT(*)::int AS n FROM records`);
    const record_count = countRows[0]?.n ?? 0;
    const bySource = await pool.query(
      `SELECT COALESCE(source,'pipeline') AS source, COUNT(*)::int AS n FROM records GROUP BY 1`
    );
    const counts_by_source = Object.fromEntries(bySource.rows.map((r) => [r.source, r.n]));
    if (rows.length === 0) {
      return NextResponse.json({ status: "ok", build_hash: null, record_count, counts_by_source });
    }
    return NextResponse.json({ status: "ok", ...rows[0], record_count, counts_by_source });
  } catch (e) {
    return NextResponse.json({ status: "db_unavailable", error: String(e) }, { status: 503 });
  }
}
