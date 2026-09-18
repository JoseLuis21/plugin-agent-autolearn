import "server-only";

import type { Row } from "./db";

export const pool = {
  async execute(_sql: string, _params: unknown[]): Promise<Row[]> {
    return [];
  },
};
