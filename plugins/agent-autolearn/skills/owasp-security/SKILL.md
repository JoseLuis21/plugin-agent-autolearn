---
name: owasp-security
description: Secure coding for the team's stacks — how to write code that survives the OWASP Top 10 and API Security Top 10 in this team's Go/Fiber backends and Next.js apps. Read it before touching authentication, authorization, SQL, user input, tokens, secrets, file paths or outbound URLs; before adding an endpoint, Server Action or route handler; and when asked to make something secure. Covers injection, broken access control, auth failures, crypto and SSRF with the helpers this team already has.
---

# OWASP for the team

How to write code that does not become a finding. The mirror of this skill is
`pre-pr-review`, which audits the same categories before the PR goes up — what you skip here
comes back there, with your name on it.

**This skill does not repeat the architecture laws.** `development-nextjs` and
`development-golang` already require Zod on both edges, the `_internal` DAL, auth inside the
DAL, and repository placeholders. Follow those and most of OWASP is already handled. What follows
is the security-specific part: the attacks, and the helpers this team already has for them.

**Use what exists.** Almost every primitive below is already written and tested in the repos.
Rolling your own is how a subtle bug gets in.

---

## The five that always matter

These are the categories the pre-PR review walks on **every** diff. Get them right and the rest is
hardening.

---

## A03 / API8 — Injection

### SQL: the value is always a placeholder

The rule has one sentence: **column names and fixed fragments may be concatenated; a value never
is.**

```go
// WRONG — fmt.Sprintf into a query. This is the finding that blocks PRs.
query := fmt.Sprintf("SELECT * FROM leads WHERE email = '%s'", email)

// RIGHT — the value travels as an argument
query := "SELECT * FROM leads WHERE email = ?"
rows, err := repo.conn.DB.QueryContext(ctx, query, filters.Email.String())
```

Dynamic queries are built the way the lead repository already does it: the fragment comes from
code, the value from `args`.

```go
args := []any{}
query := "SELECT ... FROM leads L WHERE 1=1"

if filters.InvestorID != nil {
	query += " AND investor_id = ?"              // fragment: literal, from code
	args = append(args, filters.InvestorID.Int64()) // value: always an argument
}
```

**`ORDER BY` and `LIMIT` cannot take placeholders**, which is exactly where injection sneaks back
in. Resolve them through a closed `switch` over typed constants — never from a user string:

```go
// RIGHT — the user picks an enum, not a column name
switch order.Field {
case ports_lead_persistence.OrderByInvestorID:
	query += " ORDER BY investor_id"
default:
	query += " ORDER BY id"
}
```

If you ever need `LIKE`, the wildcards go in the **argument**, not in the query string:
`"... LIKE ?"` with `"%"+term+"%"`.

### Command and path injection

- No `os/exec` with anything derived from a request. If it is unavoidable, pass arguments as a
  slice (never a shell string) and allowlist the binary.
- A file path built from user input gets `filepath.Clean` and a prefix check against the intended
  base directory. `../../etc/passwd` is still a live attack in 2026.

### Next.js

- Injection on the front is mostly **XSS**: React escapes by default, so the vector is
  `dangerouslySetInnerHTML`. If you truly need it, sanitize server-side and say why in a comment.
- A route handler that forwards `searchParams` into a backend query without parsing is the same SQL
  injection, one hop away. Parse with Zod in the handler (the architecture skill requires it anyway).

---

## A01 / API1 / API3 / API5 — Broken access control

**The most common and most expensive hole.** Authenticated is not authorized.

### The IDOR test

Before writing the query, answer: *if an attacker swaps this id for another one, what stops them?*
If the answer is "nothing", the filter is missing.

```go
// WRONG — any authenticated investor reads any lead
lead, err := svc.leadRepository.GetByFilters(ctx, &ports_lead_persistence.LeadFilters{ID: &id})

// RIGHT — ownership is part of the query, not an afterthought
lead, err := svc.leadRepository.GetByFilters(ctx, &ports_lead_persistence.LeadFilters{
	ID:         &id,
	InvestorID: &session.InvestorID,
})
```

Scope by owner **in the filter**, not with an `if` after the read: a check you can forget is a
check that will be forgotten, and the row already left the database.

### Where the check lives

- **Go**: inside the DAL/service, never only in the route group. Every use case that reads or
  mutates private data re-verifies. `private` in `routes/private.routes.go` proves *authenticated*,
  nothing more.
- **Next.js**: inside `_internal`. A Server Action is a public HTTP endpoint — a `redirect('/login')`
  on the page protects nothing, because the action is reachable without ever rendering that page.

```ts
// app/leads/_internal/leads.dal.ts
import "server-only";

export async function deleteLead(leadId: string) {
  const session = await auth();
  if (!session?.user) throw new Error("Unauthorized");
  if (!session.user.permissions.includes("leads.delete")) throw new Error("Forbidden");
  // ...
}
```

### Adding an endpoint

Compare it against its siblings in the same group **before** writing it. If every other endpoint in
`investors` filters by the session investor and yours does not, that is the finding — and it will be
a BLOCKER, because it exposes another customer's data.

Never widen a public route list, a middleware matcher or a CORS origin as a side effect of an
unrelated change.

---

## A07 / API2 — Authentication failures

The team already has the pieces. Use them:

| Need | Use | Not |
|---|---|---|
| Hash a password | `utils.HashPassword` / `utils.CheckPassword` (bcrypt) | MD5, SHA1, SHA256 bare |
| Random token | `utils.GenerateRefreshToken`, `utils.SecureRandomString` (`crypto/rand`) | `math/rand`, timestamps, uuid as a secret |
| Access token | `utils.GenerateAccessToken` (HS256, `exp` set) | a token without expiry |
| Store an auth token | `utils.HashTokenAuth` before persisting | the raw token in the database |

Rules that are easy to break:

- **Validate the signing method on parse.** The middleware already does
  `jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Name})`. Removing it re-opens the `alg: none`
  class of attack. Never parse a token without it.
- **Compare secrets in constant time**: `crypto/subtle.ConstantTimeCompare`, not `==`, for tokens,
  signatures and webhook HMACs.
- **Login is rate limited** (`LoginRateLimit`). A new authentication path — 2FA, magic link, OTP —
  needs its own limit, or it is a brute-force oracle.
- **Same error for wrong user and wrong password.** "Email not found" is an account enumeration API.
- Logout and password change **revoke refresh tokens**. A token that survives a password change
  means the victim cannot lock the attacker out.
- Cookies carrying sessions: `HttpOnly`, `Secure`, `SameSite`. Always.

---

## A02 — Cryptographic failures

- **No secret in the diff.** Not in code, not in tests, not in fixtures, not in a comment, not in
  `.env.example` with a real value. A secret that reached a commit is burned: rotate it, do not
  delete the line and hope.
- Secrets are read in `internal/config` (Go) or `_internal` (Next.js) — nowhere else. The
  architecture skills already require this; the security reason is that a secret read anywhere can
  be logged or bundled anywhere.
- **Nothing without `NEXT_PUBLIC_` may reach a client component.** Anything that does is published
  to every visitor with devtools.
- Randomness for anything security-bearing comes from `crypto/rand` (Go) or `crypto.randomUUID` /
  `crypto.getRandomValues` (web). `math/rand` and `Math.random()` are predictable.
- Do not invent encryption. If something needs to be encrypted at rest, raise it before writing it.

---

## A10 / API7 — SSRF

Any outbound request whose URL is influenced by user input is SSRF until proven otherwise. From
inside the VPC, `http://169.254.169.254/` is cloud credentials.

- Never build an outbound URL from a request field. If a redirect or callback URL is genuinely
  needed, match it against an **allowlist of hosts** kept in config.
- Same for redirects: a `?next=` parameter goes through an allowlist, or it is an open redirect
  handing your users to a phishing page that looks like your login.
- Webhooks you receive are unauthenticated until you verify the signature. Verify it with a
  constant-time compare, on the raw body, before parsing.

---

## The rest — when you touch their surface

**A04 Insecure design.** A new money flow — credits, payments, disputes, refunds — needs its abuse
case thought through before it is written: can it run twice, can it go negative, can someone else's
id be passed in? Idempotency keys on anything that charges or sends.

**A05 Misconfiguration.** Security headers, CORS and CSP are a deliberate decision, not a default.
CORS with `*` plus credentials is not a thing browsers allow, and reaching for it means the design is
wrong. IAM policies with `*` in Action or Resource do not ship.

**A06 Vulnerable components.** A new dependency: check the name against typosquatting, pin the
version, and say in the PR why it earns its place. Prefer the standard library.

**A08 Integrity failures.** Do not deserialize untrusted data into objects. Validate third-party
responses with Zod (Next.js) or a typed decode (Go) — the architecture skills already require it, and
the security reason is that the other side can be compromised.

**A09 Logging failures.** Log the security events — failed login, permission denied, token refused —
with enough context to investigate. Never log a token, password, full card number, or a complete
request body. `slog` structured, the logger the repo already uses.

**API4 Resource consumption.** A list endpoint has a maximum page size enforced server-side; a client
that asks for `limit=1000000` gets the cap, not the rows. Uploads have a size limit and an extension
allowlist.

**API6 Business flow abuse.** If a flow is valuable when automated — registration, lead submission,
purchase — it needs a rate limit or a challenge.

---

## Before you open the PR

Five questions. If you cannot answer one, you are not done:

1. Every value in every query I wrote travels as `?` — and every `ORDER BY` came from a `switch`.
2. Every new read or write is scoped to the owner **inside the filter**, and I compared it against
   its sibling endpoints.
3. No secret, token or password appears in the diff, tests and fixtures included.
4. Nothing without `NEXT_PUBLIC_` crossed to a client component; no raw token reached the browser.
5. No outbound URL is built from user input without an allowlist.

Then run `/agent-autolearn:pre-pr-review` and let the security reviewer disagree with you.
