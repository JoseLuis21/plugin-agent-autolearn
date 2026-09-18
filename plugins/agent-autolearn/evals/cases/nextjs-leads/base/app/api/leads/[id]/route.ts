import { NextResponse } from "next/server";

import { db } from "@/lib/db";
import { requireSession } from "@/lib/session";

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const session = await requireSession();
  const { id } = await params;
  const rows = await db.query("SELECT id, name, email FROM leads WHERE id = ? AND org_id = ?", [id, session.orgId]);
  if (rows.length === 0) return NextResponse.json({ error: "not_found" }, { status: 404 });
  return NextResponse.json(rows[0]);
}
