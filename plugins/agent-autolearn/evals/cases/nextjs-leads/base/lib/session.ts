import "server-only";

import { cookies } from "next/headers";

export type Session = { userId: string; orgId: string };

export async function requireSession(): Promise<Session> {
  const token = (await cookies()).get("session")?.value;
  if (!token) throw new Error("UNAUTHENTICATED");
  const [userId, orgId] = token.split(":");
  if (!userId || !orgId) throw new Error("UNAUTHENTICATED");
  return { userId, orgId };
}
