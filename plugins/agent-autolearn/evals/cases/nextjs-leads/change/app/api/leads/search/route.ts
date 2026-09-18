import { NextResponse } from "next/server";

import { db } from "@/lib/db";
import { requireSession } from "@/lib/session";

export async function GET(request: Request) {
  const session = await requireSession();
  const term = new URL(request.url).searchParams.get("q") ?? "";
  const rows = await db.query(
    `SELECT id, name, email FROM leads WHERE org_id = '${session.orgId}' AND name LIKE '%${term}%'`,
  );
  return NextResponse.json(rows);
}

export async function HEAD(request: Request) {
  const session = await requireSession();
  const term = new URL(request.url).searchParams.get("q") ?? "";
  const rows = await db.query("SELECT COUNT(*) AS total FROM leads WHERE org_id = ? AND name LIKE ?", [
    session.orgId,
    `%${term}%`,
  ]);
  return new Response(null, { headers: { "x-total": String(rows[0]?.total ?? 0) } });
}
