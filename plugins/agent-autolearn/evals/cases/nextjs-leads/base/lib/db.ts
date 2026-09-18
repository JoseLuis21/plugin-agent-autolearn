import "server-only";

export type Row = Record<string, unknown>;

// Thin wrapper over the driver. Values always travel as parameters.
export const db = {
  async query(sql: string, params: unknown[] = []): Promise<Row[]> {
    const { pool } = await import("./pool");
    return pool.execute(sql, params);
  },
};
