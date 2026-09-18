---
name: development-nextjs
description: MANDATORY LAW for the team's Next.js projects — architecture rules for EVERY component, folder, function, server action and DAL function: the _internal Data Access Layer, the client/server boundary, Zod on both edges, DTOs, module structure. Read it BEFORE writing or modifying code in a Next.js project (App Router, pnpm, AuthJS, shadcn). Not for other stacks.
---

# Next.js Modular Architecture Skill

This skill enforces the team's architecture for all Next.js code. It is the team's law:
the same rules `nextjs-architecture-reviewer` checks before a PR goes up.

If the project you are in documents its own architecture — its own copy of this skill, `AGENTS.md`,
`CLAUDE.md` — that one wins where they disagree.

## Core Guidelines

### 1. Technology Stack

- **Package Manager**: `pnpm` only.
- **Authentication**: AuthJS.
- **UI**: Shadcn UI + Tailwind CSS.
- **Validation**: Zod for schemas, forms, server actions, API responses, and DTO validation.
- **State Management**: Zustand when client-side state is needed.
- **Data Security Pattern**: Next.js Data Access Layer (DAL) using `_internal`.

---

## 2. Global Directory Structure

Global shared folders MUST be prefixed with `_`:

```txt
_components/
_enums/
_fonts/
_hooks/
_interfaces/
_internal/
_providers/
_utils/
```

### Folder responsibilities

- `_components/`: Shared reusable UI components.
- `_hooks/`: Shared client hooks.
- `_interfaces/`: Shared TypeScript interfaces/types that are safe to import from client or server.
- `_schemas/`: Shared Zod schemas only when truly global.
- `_utils/`: Generic utilities that are safe for client and server.
- `_internal/`: Server-only Data Access Layer, API clients, authorization helpers, mappers, and private business logic.

---

# 3. Mandatory Data Access Layer Convention

## 3.1 `_internal` is the official DAL

The `_internal` folder is the only allowed place for:

- External backend API calls.
- Direct access to `process.env`, except public `NEXT_PUBLIC_*` config when unavoidable.
- Authenticated fetch wrappers.
- Authorization checks.
- Backend `snake_case` to frontend `camelCase` mapping.
- DTO creation.
- Server-only business rules.
- Private backend response types.

Every file inside `_internal` that accesses private data MUST include:

```ts
import "server-only";
```

Example:

```ts
import "server-only";

import { auth } from "@/auth";

export async function getAccessTokenOrThrow() {
  const session = await auth();

  if (!session?.accessToken) {
    throw new Error("Unauthorized");
  }

  return session.accessToken;
}
```

## 3.2 Client Components MUST NOT import `_internal`

Never import `_internal` from:

- Client Components.
- Zustand stores.
- Browser hooks.
- Shared UI components using `'use client'`.

Bad:

```ts
"use client";

import { getUsers } from "../_internal/users.dal";
```

Good:

```tsx
// Server Component
import { getUsersDTO } from "./_internal/users.dal";
import { UsersTable } from "./_components/users-table";

export default async function Page() {
  const users = await getUsersDTO();

  return <UsersTable users={users} />;
}
```

---

# 4. Modular Domain Architecture

Modules live inside `app/`.

Each module folder MUST contain:

```txt
app/module-name/
  _actions/
  _components/
  _internal/
  _schemas/
  layout.tsx
  page.tsx
  error.tsx
```

## 4.1 Module folder responsibilities

### `_actions/`

Server Actions only.

Rules:

- Must include `'use server'`.
- Must validate all input with Zod.
- Must not contain raw fetch calls.
- Must not contain large business logic.
- Must not return raw backend responses.
- Must delegate data access and mutations to `_internal`.

**`'use server'` is what makes the file importable from a Client Component.** Without it, a file
that imports `server-only`, `next/headers` or a Node builtin fails the production build as soon as
any `'use client'` file imports it — directly or through another component:

```
Error: You're importing a module that depends on "server-only" into a React Client
Component module. 'server-only' cannot be imported from a Client Component module.
```

This is transitive: a client component importing a plain helper that imports the action file
breaks the same way. Two consequences when a Client Component needs server work:

- The action file carries `'use server'` at the top and **exports only async functions**. Types,
  constants and helpers go somewhere else — exporting them from a `'use server'` file is itself a
  build error.
- If the client only needs *data* and not an operation, do not import anything: let the Server
  Component read it from the DAL and pass the DTO down as props.

Example:

```ts
"use server";

import "server-only";

import { revalidatePath } from "next/cache";
import { createUserSchema } from "../_schemas/create-user.schema";
import { createUser } from "../_internal/users.dal";

export async function createUserAction(input: unknown) {
  const payload = createUserSchema.parse(input);

  await createUser(payload);

  revalidatePath("/users");

  return {
    success: true,
  };
}
```

### `_internal/`

Module-specific DAL.

Rules:

- Must include `import 'server-only'`.
- Performs auth and authorization checks.
- Calls backend APIs.
- Maps backend responses to safe DTOs.
- Returns only the fields needed by UI.
- Never returns secrets, tokens, raw backend records, or unnecessary fields.

Example structure:

```txt
_internal/
  users.dal.ts
  users.mapper.ts
  users.api-types.ts
  users.permissions.ts
```

### `_schemas/`

Zod schemas for:

- Forms.
- Server Action input.
- Search params.
- Backend API responses.
- DTOs when needed.

### `_components/`

Private module UI components.

Rules:

- Components receive safe DTOs only.
- Client components must receive minimal props.
- Client components must not receive raw backend models.

---

# 5. External Go API Integration

The Go backend is accessed only through the DAL.

## 5.1 Base URL

```ts
const API_URL = process.env.NEXT_PUBLIC_API_URL;
```

## 5.2 Mandatory headers

Every authenticated backend request MUST include:

```ts
headers: {
  Authorization: `Bearer ${session.accessToken}`,
  'Content-Type': 'application/json',
}
```

## 5.3 Central fetch wrapper

Create a server-only backend fetch helper.

Example:

```ts
import "server-only";

import { auth } from "@/auth";

type ApiFetchOptions = Omit<RequestInit, "headers"> & {
  headers?: HeadersInit;
};

export async function apiFetch(path: string, options: ApiFetchOptions = {}) {
  const session = await auth();

  if (!session?.accessToken) {
    throw new Error("Unauthorized");
  }

  const baseUrl = process.env.NEXT_PUBLIC_API_URL;

  if (!baseUrl) {
    throw new Error("Missing NEXT_PUBLIC_API_URL");
  }

  return fetch(`${baseUrl}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
}
```

## 5.4 Backend response validation

Every backend response MUST be validated with Zod before being used.

Example:

```ts
import "server-only";

import { z } from "zod";
import { apiFetch } from "@/app/_internal/api-fetch";

const BackendUserSchema = z.object({
  id: z.string(),
  first_name: z.string(),
  last_name: z.string(),
  email: z.string().email(),
  created_at: z.string(),
});

const UsersResponseSchema = z.array(BackendUserSchema);

export async function getUsersDTO() {
  const response = await apiFetch("/users");

  if (!response.ok) {
    throw new Error("Failed to fetch users");
  }

  const raw = await response.json();
  const users = UsersResponseSchema.parse(raw);

  return users.map((user) => ({
    id: user.id,
    firstName: user.first_name,
    lastName: user.last_name,
    email: user.email,
  }));
}
```

---

# 6. DTO Rules

## 6.1 Never expose raw backend models

Do not pass raw backend responses directly to components.

Bad:

```tsx
const user = await getRawUser();

return <UserProfile user={user} />;
```

Good:

```tsx
const user = await getUserProfileDTO();

return <UserProfile user={user} />;
```

## 6.2 DTOs must be minimal

Only return fields the UI needs.

Bad:

```ts
return {
  id: user.id,
  email: user.email,
  passwordHash: user.password_hash,
  accessToken: user.access_token,
  role: user.role,
  createdAt: user.created_at,
};
```

Good:

```ts
return {
  id: user.id,
  email: user.email,
  role: user.role,
};
```

## 6.3 Always map `snake_case` to `camelCase`

Backend:

```ts
created_at;
first_name;
last_name;
```

Frontend DTO:

```ts
createdAt;
firstName;
lastName;
```

---

# 7. Authentication and Authorization

## 7.1 Auth checks belong inside the DAL

Every DAL function that reads or mutates private data MUST verify authentication.

Example:

```ts
const session = await auth();

if (!session?.user || !session.accessToken) {
  throw new Error("Unauthorized");
}
```

## 7.2 Authorization must be resource-specific

Never rely only on page-level auth.

Bad:

```ts
export default async function Page() {
  const session = await auth()

  if (!session?.user) {
    redirect('/login')
  }

  return <AdminPanel />
}
```

Good:

```ts
export async function deleteUser(userId: string) {
  const session = await auth();

  if (!session?.user) {
    throw new Error("Unauthorized");
  }

  if (!session.user.permissions.includes("users.delete")) {
    throw new Error("Forbidden");
  }

  await apiFetch(`/users/${userId}`, {
    method: "DELETE",
  });
}
```

Server Actions are reachable as server endpoints, so every action and mutation path must re-check authentication and authorization through the DAL.

---

# 8. Server Actions Rules

Server Actions are thin controllers.

They may:

- Validate input with Zod.
- Call DAL functions.
- Return safe action states.
- Revalidate paths/tags.
- Redirect when needed.

They must not:

- Call the Go API directly.
- Access `process.env` directly.
- Return raw backend objects.
- Trust client input.
- Contain authorization logic duplicated from DAL.
- Expose secrets or private fields.

Example:

```ts
"use server";

import "server-only";

import { updateUserSchema } from "../_schemas/update-user.schema";
import { updateUser } from "../_internal/users.dal";

export async function updateUserAction(input: unknown) {
  const payload = updateUserSchema.parse(input);

  await updateUser(payload);

  return {
    success: true,
  };
}
```

---

# 9. Server Components Rules

Server Components may call DAL functions directly.

Example:

```tsx
import { getUsersDTO } from "./_internal/users.dal";
import { UsersTable } from "./_components/users-table";

export default async function UsersPage() {
  const users = await getUsersDTO();

  return <UsersTable users={users} />;
}
```

Rules:

- Server Components may import `_internal`.
- Server Components must pass only safe DTOs to Client Components.
- Server Components must not pass session, access token, backend raw records, or private data to Client Components.

---

# 10. Client Components Rules

Client Components must be explicit:

```tsx
"use client";
```

Client Components may:

- Render UI.
- Manage local UI state.
- Use Zustand stores.
- Call Server Actions.
- Receive safe DTOs.

Client Components must not:

- Import `_internal`.
- Access `process.env` private variables.
- Call the Go API directly with access tokens.
- Receive raw backend records.
- Receive session tokens.

---

# 11. Zustand Persistence

Use Zustand for client-side state management when needed.

## 11.1 Persist middleware

Stores requiring browser persistence MUST use `persist`.

Example:

```ts
"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";

type Store = {
  count: number;
  inc: () => void;
};

export const useCounterStore = create<Store>()(
  persist(
    (set) => ({
      count: 1,
      inc: () => set((state) => ({ count: state.count + 1 })),
    }),
    {
      name: "counter-store",
    },
  ),
);
```

## 11.2 SSR hydration safety

Components using persisted stores must avoid hydration mismatch.

Use a hydration hook:

```ts
"use client";

import { useEffect, useState } from "react";

export function useHasHydrated() {
  const [hasHydrated, setHasHydrated] = useState(false);

  useEffect(() => {
    setHasHydrated(true);
  }, []);

  return hasHydrated;
}
```

Usage:

```tsx
"use client";

import { useHasHydrated } from "@/app/_hooks/use-has-hydrated";
import { useCounterStore } from "../_stores/counter.store";

export function Counter() {
  const hasHydrated = useHasHydrated();
  const { count, inc } = useCounterStore();

  if (!hasHydrated) {
    return null;
  }

  return (
    <div>
      <span>{count}</span>
      <button onClick={inc}>one up</button>
    </div>
  );
}
```

---

# 12. Error Handling

## 12.1 DAL errors

DAL functions should throw safe, generic errors.

Bad:

```ts
throw new Error(`Backend failed with token ${session.accessToken}`);
```

Good:

```ts
throw new Error("Failed to fetch users");
```

## 12.2 UI errors

Each module must include:

```txt
error.tsx
```

Use it to render safe user-facing errors.

---

# 13. Caching Rules

Use caching carefully.

Rules:

- Do not cache private user-specific data globally.
- Do not cache responses that depend on session unless using a private/request-safe strategy.
- Prefer request-level memoization using React `cache()` for repeated server-side reads.
- Revalidate paths/tags after mutations.
- Never cache access tokens.

Example:

```ts
import "server-only";

import { cache } from "react";
import { auth } from "@/auth";

export const getCurrentSession = cache(async () => {
  const session = await auth();

  if (!session?.user || !session.accessToken) {
    throw new Error("Unauthorized");
  }

  return session;
});
```

---

# 14. Security Rules

Mandatory:

- Treat all client input as untrusted.
- Validate `formData`, JSON bodies, URL params, route params, and `searchParams` with Zod.
- Re-check auth inside DAL mutations.
- Return only safe DTOs.
- Never expose access tokens to Client Components.
- Never expose raw backend responses to Client Components.
- Never import server-only code into Client Components.
- Never access secrets outside `_internal`.
- Prefer `import 'server-only'` for every DAL file.
- For expensive mutations, consider rate limiting.

---

# 15. Recommended Module Example

```txt
app/users/
  _actions/
    create-user.action.ts
    update-user.action.ts
    delete-user.action.ts

  _components/
    users-table.tsx
    user-form.tsx

  _internal/
    users.dal.ts
    users.mapper.ts
    users.api-types.ts
    users.permissions.ts

  _schemas/
    create-user.schema.ts
    update-user.schema.ts
    user-response.schema.ts

  layout.tsx
  page.tsx
  error.tsx
```

---

# 16. Architecture Decision Rules

When creating new code:

1. If code accesses backend data, put it in `_internal`.
2. If code mutates data from UI, create a Server Action in `_actions` that calls `_internal`.
3. If code validates input or response shape, use Zod in `_schemas`.
4. If code renders UI shared only by the module, put it in module `_components`.
5. If code renders UI shared across modules, put it in global `_components`.
6. If code uses browser APIs, it must be a Client Component or client hook.
7. If code needs localStorage persistence, use Zustand with `persist`.
8. If data crosses from server to client, it must be a safe DTO.
9. If backend uses `snake_case`, frontend receives `camelCase`.
10. If unsure whether a function is server-only, place it in `_internal` and add `import 'server-only'`.

---

# 17. Absolute Prohibitions

Do not:

- Use npm, yarn, or bun.
- Call backend APIs directly from Client Components.
- Import `_internal` into Client Components.
- Pass raw backend records to Client Components.
- Return raw backend responses from Server Actions.
- Skip Zod validation for client input.
- Skip Zod validation for backend responses.
- Access private environment variables outside `_internal`.
- Duplicate authorization logic across UI components.
- Trust page-level auth as sufficient protection for mutations.
