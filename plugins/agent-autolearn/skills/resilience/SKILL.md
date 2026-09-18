---
name: resilience
description: How the team's code survives missing data, slow dependencies and hostile input — the difference between "empty" and "failed", timeouts on every outbound call, UTC dates, validated environment variables, and fail-closed defaults, in this team's Next.js apps and Go services. Read it BEFORE reading a field off a CMS or API response, writing a catch block, adding a fetch, parsing an env var, formatting a date, or deciding what a page does when its data source is down.
---

# Resilience for the team

How to write code that degrades instead of breaking. The mirror of this skill is the
`edge-case-reviewer` in `pre-pr-review`, which hunts exactly what follows.

The findings in this category are rarely dramatic. They are a page that redirects when it should
have failed, a date one day early, a form that hangs with no message. They reach production
because the happy path was the only path anyone ran.

---

## "Empty" and "failed" are different answers

This is the most expensive confusion in the team's code, and it is one `if` away from being fixed.

A CMS or API call has three outcomes, not two: **it worked and returned data**, **it worked and
there is nothing**, **it did not work**. Collapsing the last two throws away the only information
that tells you what to do.

```ts
// WRONG — Strapi is down, the catch returns null, and the article URL redirects to the index.
const posts = await getPost(postSlug);
if (!Array.isArray(posts) || posts.length === 0) redirect("/blog");

// RIGHT — a real outage fails; only a genuinely absent post redirects.
if (posts === null) throw new Error("CMS unavailable");
if (posts.length === 0) redirect("/blog");
```

Why it matters beyond correctness: a redirect tells Googlebot the article URL *is* the index and
consolidates them. A 5xx tells it to come back later. The reader gets thrown to a list page with
no explanation instead of an error they can retry.

**So the contract has to preserve the difference.** A fetch helper that ends in `?? null` for both
cases has already destroyed it:

```ts
// WRONG — "no rows" and "request blew up" both arrive as null.
return res?.data?.[0] ?? null;

// RIGHT — the caller can still tell them apart.
return res.data;          // [] when there is nothing
// and the catch returns null, which now means exactly one thing
```

In Go the same rule is the difference between `sql.ErrNoRows` and every other error: map the
first to an empty result the caller can render, wrap the rest and let them travel.

---

## Every field can be missing

Content editors leave fields blank. That is normal input, not an error, and it must never produce
a 500.

- Guard on the value you are about to use, not on its parent. `item?.thumbnail?.url && <Image …>`
  — because the image component throws on an `undefined` `src` even when `item.thumbnail` exists.
- A missing description renders an empty block; it does not crash the route. This has taken a page
  down here.
- A missing relation is an empty array, not `undefined.map`.
- Do not let a helper assume depth: reading `children[0].text` works until an editor adds bold,
  and then it silently returns `""`.

The rule that generalizes: **treat every optional field as absent at least once in your head
before you render it.** If the empty state looks broken, design the empty state — that is part of
the feature, not a defensive extra.

---

## Every outbound call gets a timeout

A `fetch` with no timeout does not fail. It hangs, holds the request, and eventually dies
somewhere you are not looking — a load balancer idle timeout, a Lambda limit, a user closing the
tab.

```ts
// WRONG — the signup backend hangs and the Server Action hangs with it.
await fetch(URL_JOIN, requestOptions);

// RIGHT
await fetch(URL_JOIN, { ...requestOptions, signal: AbortSignal.timeout(15000) });
```

And the timeout is only half of it — **the caller has to say something**. A `catch` that
re-enables a submit button without setting an error message produces the worst outcome available:
the user sees nothing happen, submits again, and the second attempt answers *"the email is
already registered"*.

```ts
} catch (e) {
  setErrorsSend("We could not reach the server. Please try again.");
  regenerateToken();
}
```

In Go: every outbound call takes a `context` with a deadline, and the deadline comes from the
caller, not from a constant buried in the adapter.

---

## Dates are UTC until the moment you print them

`new Date("2026-02-03")` is parsed as UTC. `date.getDate()` reads it back in the **server's**
local time. Those two lines in the same function are a one-day error waiting for a timezone.

```ts
// WRONG — "3rd Feb" in UTC, "2nd Feb" in America/New_York.
date.getDate(); date.getFullYear(); date.toLocaleString("en-US", { month: "short" });

// RIGHT
date.getUTCDate(); date.getUTCFullYear();
date.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
```

This is live in the team's code today and invisible only because the Alpine images boot in UTC.
The day a task definition sets `TZ`, every published date moves a day. **Do not rely on the
container's timezone being UTC** — say UTC in the code.

---

## Environment variables are input, and `parseInt` is not validation

`parseInt("1h")` is `1`. Not an error — `1`. A cache window meant to be an hour becomes one
second, and the upstream service takes ~3600× the traffic with nothing in the logs.

```ts
// WRONG
const value = parseInt(process.env.REVALIDATE_FETCH_TIME);

// RIGHT — reject what is not a whole non-negative number, and say so out loud.
if (raw === undefined || raw.trim() === "") {
  console.error("REVALIDATE_FETCH_TIME is not set, falling back to 3600s");
  return 3600;
}
const parsed = Number(raw);
return Number.isInteger(parsed) && parsed >= 0 ? parsed : 3600;
```

Three rules around configuration:

- **Resolve it once, in one module, and import the constant.** Reading `process.env` at each call
  site is how two call sites end up disagreeing.
- **An explicit value always wins, including `0`.** A fallback that fires on `0` is a bug: `0`
  means "never cache" and it is a deliberate choice in several of these repos.
- **Changing a fallback is changing behavior.** Moving a default from `0` to `3600` silently
  converts every environment that forgot the variable from always-fresh to cached-for-an-hour.
  See `safe-refactor`.

---

## Fail closed, and say which side failed

When a security check cannot run, the answer is **deny**. When it denies, the message has to
distinguish *your* fault from *the user's*.

A Turnstile verification with an empty secret returns HTTP 200 with `invalid-input-secret`. Read
naively, that is "the visitor's token is bad", and every single submission is rejected with
*"Invalid Turnstile token"* while the real problem is a missing environment variable. Nobody
reports it as an outage, because it looks like the users are wrong.

```ts
"missing-input-secret" | "invalid-input-secret" | "bad-request"  → "unavailable" → 503, log loudly
"invalid-input-response"                                          → "invalid"     → 400, tell the user
network error / timeout                                           → "unavailable" → 503
```

Same shape for any gate: a validator that throws, a key that is absent, a dependency that times
out — deny the request, return the status that says *we* failed, and log it where someone is
watching. Never let a failing check fall through to allow.

---

## Boundaries worth one thought each

Before a function is done, run it mentally on:

- **Empty, one, many** for every collection — and the one past the limit, for anything paginated
  or sliced.
- **The value that is present but the wrong type.** A form field is not a string because the form
  sends one; a JSON array can contain objects. `typeof x === "string"` before you slice it.
- **Zero and negative** for anything numeric, especially when it becomes a limit, an offset or a
  duration.
- **Double submit.** A button the user can press twice while the first request is in flight needs
  to be disabled by state, not by hope.
- **The unicode cases** when you truncate: a `slice` in the middle of a surrogate pair produces a
  broken character, and control characters inside a value survive a naive check.
- **Concurrent writes** in Go: a map written from two goroutines panics; the race detector finds
  it, `go test -race` is not optional for anything with a goroutine.

---

## The catch block is a decision, not a formality

Three things every `catch` in this team's code has to answer:

1. **Is this error mine to handle?** In Next.js, an error carrying `.digest` is the framework's
   own control flow — a static-render bailout or a `redirect()`. Swallowing it breaks the build
   or turns a redirect into a blank page. Rethrow it first, always.
2. **What does the caller get?** A `null` that means "failed" is fine if callers treat it as
   failure. A `null` that means both "failed" and "empty" is the first section of this document.
3. **Who finds out?** `console.error` in a catch is intentional error handling and belongs there.
   A catch that logs nothing and returns a default is how an outage stays invisible for a month.

```ts
.catch((error) => {
  if (error?.digest) throw error;                    // 1 — not mine
  console.error("Strapi request failed:", error);    // 3 — someone finds out
  return null;                                       // 2 — one meaning
});
```

When that block appears more than twice, it becomes a named helper — not a copy. Thirty-seven
copies of it exist in one repo today, and the review's point stands: one of them will lose a line
and nothing will fail.
