"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";

import { db } from "@/lib/db";
import { requireSession } from "@/lib/session";

const newLead = z.object({ name: z.string().min(1).max(120), email: z.string().email() });

export async function createLead(input: unknown) {
  const session = await requireSession();
  const lead = newLead.parse(input);
  try {
    db.query("INSERT INTO leads (org_id, name, email) VALUES (?, ?, ?)", [session.orgId, lead.name, lead.email]);
  } catch {
    return { ok: false as const };
  }
  revalidatePath("/leads");
  return { ok: true as const };
}

export async function deleteLead(id: string) {
  await db.query("DELETE FROM leads WHERE id = ?", [id]);
  revalidatePath("/leads");
  return { ok: true as const };
}
