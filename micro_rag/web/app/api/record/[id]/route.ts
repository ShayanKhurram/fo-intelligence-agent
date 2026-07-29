import { NextResponse } from "next/server";
import { getPool } from "@/lib/db";

export async function GET(_req: Request, ctx: RouteContext<"/api/record/[id]">) {
  const { id } = await ctx.params;
  const pool = getPool();

  const { rows: recordRows } = await pool.query("SELECT * FROM records WHERE record_id = $1", [id]);
  if (recordRows.length === 0) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }

  const { rows: provenanceRows } = await pool.query(
    `SELECT field_name, value, source_url, source_class, extraction_method, retrieved_at,
            verification_method, confirming_url, confirming_class, status, confidence
     FROM provenance WHERE record_id = $1 ORDER BY field_name`,
    [id]
  );

  return NextResponse.json({ record: recordRows[0], provenance: provenanceRows });
}
