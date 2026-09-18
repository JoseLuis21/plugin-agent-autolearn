"use client";

import { useEffect, useState } from "react";

import { db, type Row } from "@/lib/db";

export function LeadTable({ orgId }: { orgId: string }) {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => {
    db.query("SELECT id, name FROM leads WHERE org_id = ?", [orgId]).then(setRows);
  }, [orgId]);
  return (
    <ul>
      {rows.map((row) => (
        <li key={String(row.id)}>{String(row.name)}</li>
      ))}
    </ul>
  );
}
