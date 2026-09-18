---
name: safe-refactor
description: How the team unifies duplicated code without shipping a regression — diffing the copies before merging them, treating defaults and sanitizers as behavior, finding every caller including the ones outside the diff, and keeping structural changes separate from behavioral ones. Read it BEFORE extracting a shared helper, merging duplicate implementations, changing a default value, making a validator stricter, renaming or deleting an exported function, or changing what a function returns.
---

# Safe refactoring for the team

How to unify code without breaking what worked. The mirror of this skill is the
`regression-reviewer` in `pre-pr-review`, which asks one question of every diff: *what used to
work that stops working now?*

This is the team's most productive source of regressions, for a simple reason: consolidating
duplicated code is the right instinct, it is done often, and it is done at the end of a PR when
attention is lowest. A commit literally named *"UNIFY CODE"* in this codebase shipped three
separate behavior changes, none of them intended.

---

## The law

**A refactor changes structure and not behavior. If behavior has to change, that is a different
commit, with its own reason written down.**

Every rule below is a way of noticing that behavior changed while you thought you were only
moving code.

---

## The copies are never identical — diff them first

You found four copies of `formatDate` and you are about to keep the best one. Before you do:

```bash
# Read all of them side by side. Never assume the one you know is representative.
git show HEAD:path/a.tsx | sed -n '/function formatDate/,/^}/p'
git show HEAD:path/b.tsx | sed -n '/function formatDate/,/^}/p'
```

In this repo, three copies of `formatDate` had a guard for a missing date and the fourth did not
— so `/press` published `NaN NaNth, NaN` for months. Whichever copy you canonize, you inherit its
gaps and you drop every fix the others accumulated privately.

The procedure that works:

1. **List every copy.** Grep the symbol across the whole repo, not the diff.
2. **Diff them against each other.** The differences *are* the requirements — each one is a fix,
   a guard, or a caller-specific need someone added for a reason.
3. **The unified version is the union of the guards**, not the newest copy.
4. **Each call site keeps its previous output.** If one of them now renders something different,
   that is the behavior change, and it needs a sentence in the PR.

The same applies to markup and styles. Three renderers that produce different Tailwind classes
are not three copies of one renderer — they are one renderer and three themes. Unify the logic,
parameterize the difference.

---

## A default is behavior

Changing a fallback looks like configuration and lands like a feature change, because it only
fires where nobody set the value — that is, in the environments nobody checks.

```ts
// Before: a missing variable meant "always fresh".
// After:  a missing variable means "cache for an hour".
- return parsed || 0;
+ return parsed || 3600;
```

Every environment that forgot the variable just changed behavior, in 38 call sites, with nothing
in the logs. Note also that `|| 0` and `?? 0` are not the same rule: with `||`, an explicit `0` —
a deliberate "never cache" in these repos — falls through to the fallback.

Before changing any default, answer: **which environments rely on the old one, and how would I
know?** If the answer is "I cannot tell", the default stays and the explicit value gets set.

---

## Making a validator stricter is a behavior change

A sanitizer, an allowlist or a type guard that gets tighter *removes* functionality. The code
looks safer and the diff looks like hardening, so it passes review easily — and content
disappears in silence, with no error and no log.

Two live examples from one commit:

```tsx
// Before: raw HTML from the CMS code block, iframes included.
parse(codeHtml)
// After:  DOMPurify with no config — whose default allowlist has no <iframe>.
parse(sanitizeHtml(codeHtml))
```

The `code` block is exactly the editor's escape hatch for pasting embeds: YouTube, Wistia, maps,
forms. Every one of them silently became an empty `<div>`.

```ts
// Before: the component rendered whatever it was given.
// After:  new URL(url) with no base — so "/uploads/clip.mp4" and "//player.vimeo.com/x"
//         are now rejected, where they used to play.
```

**So when you tighten a check, enumerate what the old one accepted and the new one does not**,
and confirm nothing in production relies on it. That is a query against the real content, not a
guess:

```
filters[content][$containsi]=<iframe
```

If legitimate cases exist, widen the new check deliberately — `ADD_TAGS`, an anchored host
allowlist — rather than reverting to no check. If none exist, say so in the PR so the next person
knows it was checked and not overlooked.

---

## Find every caller, including the ones outside the diff

The reviewer's most common regression finding is a signature that changed and a call site that
did not — and the missed call site is almost always in a file the PR never opened.

Before changing a signature, a return type, or a name:

```bash
rg -n "\bgetHelps\b" --type ts        # every reference, whole repo
rg -n "posterFor|thumbnailUrl"        # helpers have more callers than you remember
```

Three cases that need extra care:

- **A return type that gained a meaning.** A function that returned `Post[]` and now returns
  `Post[] | null` compiles at every call site that does `.length` — and throws at runtime on the
  new branch. TypeScript only helps if the callers are typed; `any` in between hides it.
- **A deleted export.** Grep for callers *before* deleting, and delete in the same commit. A
  function left with zero callers is dead weight the next person has to reason about.
- **A default parameter or an argument order.** These change every caller at once and none of
  them visibly.

In Go: changing a port interface changes every adapter that implements it and every service that
consumes it. The compiler catches the implementations; it does not catch a mock in a test that
silently keeps the old shape, nor a `container` wiring the wrong concrete type.

---

## What a shared helper has to preserve

When N inline copies become one import, the risk moves from "they drift" to "one change hits
everything". Both are real; the second is more dangerous because it is invisible at the call
sites.

- **Extract with identical behavior first, improve second.** Two commits, not one. The first is
  reviewable by reading; the second has a stated reason.
- **The helper does not gain responsibilities in the extraction.** Adding a log, a guard, or a
  default while extracting means no reviewer can tell which change caused a difference.
- **Copies do not get "kept in sync".** They get deleted. A helper that leaves three copies
  behind has added a fourth implementation.
- **The extraction gets the test the copies never had** — see `testing`. Extracting is the
  one moment the logic is importable and nobody is under time pressure.

---

## Shared identifiers must keep sharing

When two pieces of code have to agree on a derived value — an anchor `id` and the `#fragment`
that points at it, a cache key and its invalidation, a slug written on one page and read on
another — they must call **the same function on the same input**.

The failure mode is subtle: both sides "use slugify", so it looks correct, while one passes the
raw text and the other passes rendered nodes. A heading with bold text then produces `#how` on
one side and `how-work` on the other, and every index link lands nowhere.

```ts
// One source, one input shape, both sides.
export function headingText(children) { … }        // raw blocks → string
slugify(headingText(block.children))               // the id
slugify(headingText(textObject?.children))         // the link
```

If you cannot pass the same input to both sides, the shared thing is not the function — it is the
*value*. Compute it once and pass it down.

---

## Before you call the refactor done

- Every copy of the thing you unified is **deleted**, and `rg` proves it.
- Every call site renders or returns what it did before — the ones outside the diff included.
- No default, fallback, allowlist or guard changed meaning. If one did, it is in the PR text.
- Signatures that changed have all their callers updated, tests and mocks included.
- The unified helper has a test that fails without it.
- The build and the type check run clean **together**, at the end, not per file.

And the one question the reviewer will ask that is worth asking yourself first: **if this refactor
is wrong, how does anyone find out?** If the answer is "a user notices missing content", add the
test or the log that answers it better.
