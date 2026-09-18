---
name: testing
description: How the team writes tests that are worth keeping — what to cover, where tests live, and why testability is a design constraint, in this team's Next.js apps and Go services. Read it BEFORE fixing a bug, adding a branch, extracting a helper, or touching a test file; and whenever you are about to write a function whose logic you cannot import from a test. Covers the failing-test-first rule for bug fixes, test helpers that hide the bug they should catch, both-branch coverage, and the test gate that can run zero tests and report green.
---

# Testing for the team

How to write tests that catch the bug. The mirror of this skill is the `test-reviewer` in
`pre-pr-review`, which audits exactly what follows — what you skip here comes back there.

**This skill is not about coverage numbers.** Nobody here is chasing a percentage. It is about
the three ways a test suite lies: it does not exist for the branch that broke, it exists but
would pass without the fix, or it exists and never runs at all. All three have shipped in this
team's repos.

---

## The one rule for bug fixes

**A bug fixed without a test that fails before the fix is not fixed — it is fixed until the next
refactor.**

This is the single most common finding the review raises, and the reasoning is short: the fix
proves the bug is gone today; only the test keeps it gone. Write the test first, watch it fail,
then apply the fix. If the test passes before you change the code, it is testing the wrong thing.

```ts
// The diff added: if (error?.digest) throw error;
// Without this test, deleting that line again is silent.
test("a Next control-flow error is rethrown, not swallowed", () => {
  const err = Object.assign(new Error("bailout"), { digest: "DYNAMIC_SERVER_USAGE" });
  assert.throws(() => handleStrapiError(err), (thrown) => thrown === err);
});

test("any other error is logged and becomes null", () => {
  assert.equal(handleStrapiError(new Error("boom")), null);
});
```

Verify the test is real: comment the fix out and confirm the test goes red. A test you never saw
fail is a test you are guessing about.

---

## Testability is a design constraint, not a test problem

When logic cannot be imported by the test runner, the answer is **move the logic**, never "this
one is hard to test".

In a Next.js repo, a module that carries `"use server"`, a `@/` alias, or an SDK import
(`@sendgrid/mail`, a database client) cannot be loaded by `node --test`. Any branching logic
inside it is therefore untestable where it sits.

```ts
// WRONG — five branches that decide what the public sees, inside actions.ts ("use server").
// node --test cannot import this file. The logic is untestable where it lives.
const misconfigured = codes.some((c) =>
  ["missing-input-secret", "invalid-input-secret", "bad-request"].includes(c));

// RIGHT — the decision moves to a pure module; the action keeps only I/O.
// app/_utils/turnstile.ts
export function classifyTurnstile(ok: boolean, body: any): "ok" | "invalid" | "unavailable" { … }
```

The split is always the same: **I/O stays, decisions move.** `fetch`, timeouts and `try/catch`
belong in the action or the adapter; the branching that turns a response into an outcome belongs
in a pure function with a name. In Go the same rule falls out of the architecture — decisions
live in `internal/core/services` and `domain`, and those are the packages with tests.

A good signal: if you are about to write a comment explaining which branch runs when, that branch
wants to be a pure function with a test per case.

---

## A test helper must not normalize away the bug

The most dangerous test is one that constructs its input differently from production. It passes
forever, on a value the code never receives.

```ts
// WRONG — every date gets a midday time, which is exactly what hides the timezone bug.
const at = (iso: string) => `${iso}T12:00:00`;
assert.equal(formatDate(at("2026-02-03")), "3rd Feb, 2026");  // green in every timezone

// RIGHT — the shapes the CMS actually sends.
assert.equal(formatDate("2026-02-03"), "3rd Feb, 2026");
assert.equal(formatDate("2026-02-03T02:00:00.000Z"), "3rd Feb, 2026");
```

That helper kept a real defect green for months: `formatDate` read `getDate()` in local time, so
`"2026-02-03"` rendered as *2nd Feb* anywhere west of UTC. The suite was passing on a value no
caller ever passes.

**Rule: build test inputs from the real source.** Copy an actual payload out of the CMS, the
queue, or the request log. If your test needs a helper to make the input acceptable, the helper
is a bug report.

Where the environment decides the outcome, pin it and test both: `TZ=UTC` and
`TZ=America/New_York` must agree, and CI should run at least one suite under a non-UTC `TZ`.

---

## Cover the branch that returns, not only the branch that rejects

A validator has two jobs: reject the bad value **and** return the good one intact. Testing only
the first half leaves the second half free to rot.

```ts
// These five cases all expect "#", so they only pin the reject branch.
assert.equal(safeHref("java\nscript:alert(1)"), "#");
// Change `return clean` to `return url` and all five stay green,
// while accepted hrefs go back to the HTML with control characters in them.

// The missing half:
assert.equal(safeHref("/pri\u0000cing"), "/pricing");
assert.equal(safeHref("  https://example.com  "), "https://example.com");
```

Same shape everywhere: a normalizer must be asserted on its normalized output, a filter on what
survives it, a mapper on the fields it keeps. "It did not throw" is not an assertion.

---

## A gate that runs zero tests reports green

`node --test` exits 0 when its glob matches nothing. `go test ./...` passes a package with no
test files. Both mean the same thing: **the absence of tests is indistinguishable from success**,
unless something counts.

This has happened here. A commit renamed `__tests__/` to `tests/` without touching the glob in
`package.json`; the counter and the runner disagreed about where tests live, and for several
commits the Docker build gate passed having executed nothing.

The fix that stuck is the pattern to copy: **one script owns the location and hands the runner
the exact files it found**, and it fails when a test file exists somewhere it would not run.

```js
const ROOT = "app";
const TEST_DIR = "tests";      // defined once, here
const MIN_TEST_FILES = 10;
const MIN_TEST_CASES = 59;     // raised with every PR that adds cases
```

Three consequences for anything you write:

- **The minimum counts are part of the change.** Add test cases, raise `MIN_TEST_CASES` in the
  same PR. A gate whose floor never moves stops being a floor.
- **A test file outside the expected folder is an error, not an omission.** Silent skipping is
  the failure mode; make it loud.
- **The documentation is derived from the script, never written beside it.** If `AGENTS.md` and
  the gate disagree about where tests live, the next person to follow the docs breaks the image
  build. That divergence has been a finding twice.

---

## What is worth testing

Cover, in this order:

1. **Anything with security or parsing logic** — schemes and hosts (`safeHref`, `youtube.ts`),
   escaping before an email body or a query, token classification, payload normalization.
2. **Every branch of a decision that reaches a user** — a 503 and a 400 mean different things to
   the visitor; the function that chooses between them gets one case per outcome.
3. **Every bug the diff fixes** — see the first rule.
4. **Boundaries of anything that counts or slices** — empty, one, many, and one past the limit.

Do **not** file coverage for: getters, trivial mappers, one-line wrappers, types and constants,
generated code, migrations, configuration, or presentational components with no logic. Asking for
those is how a suite becomes something people delete.

---

## Tests that look fine and are not

The review flags these by name. Recognize them while writing:

- **Tautological** — the expectation is computed with the same logic as the code under test.
- **Over-mocked** — mocks exactly the thing it claims to test; would pass against an empty
  implementation.
- **Implementation-coupled** — asserts internal calls rather than observable behavior, so any
  honest refactor breaks it.
- **Fragile** — depends on `Date.now()` unfrozen, on map/set iteration order, on sleeps, on real
  network, or on state another test left behind.
- **One happy path** for a function with four branches.
- **A giant snapshot** accepted without reading it, which makes any future change pass with `-u`.
- **A name that says nothing** — `test1`, `it("works")`. The name states the guaranteed behavior:
  `"a heading in bold produces the same id as its menu link"`.

---

## Per stack

**Next.js repos.** Node's built-in runner with TypeScript type stripping — no Jest, no Vitest, no
new dependency. Tests live where that repo's gate script says, and import the module under test
with an explicit `.ts` extension. Pure helpers are the target; a component test that needs a DOM
is usually a sign the logic belongs in a helper.

**Go services.** Table-driven tests in the same package, following whatever the repo already uses
— do not introduce `testify` into a repo of plain table tests. Domain and services are where the
cases go; adapters get tested through their port interface. Any exported function in
`internal/core` with branching and no test is a finding.

Both: **follow the repo you are in.** Read the nearest existing test before writing a new one and
match its location, naming and helpers. A correct test in the wrong convention still costs the
reviewer an argument.
