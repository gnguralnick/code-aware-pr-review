# pr-review

A code-aware web interface for reviewing your GitHub pull requests. It fetches
the full repository at the PR's head commit and renders changes as full-file
diffs with hover, go-to-definition, and cross-repo find-usages -- so you review a
change in the context of the whole codebase, not a keyhole of surrounding lines.
Served at `/service/pr-review/`.

![The code-aware diff view: a full-file side-by-side diff with the changed-files list, PR status, and per-file review actions](docs/images/diff-view.png)

## Why

GitHub's own review UI shows you the changed lines and a few lines around them.
For anything non-trivial that isn't enough: you end up opening the repo in
another tab to see the function being changed, who calls it, or what a type
actually is. `pr-review` closes that gap. When you open a PR it downloads the
entire repository at the head commit, so every changed file is shown in full,
any file in the repo is one click away, and code intelligence works across the
whole tree -- all without leaving the review.

## What it does

### A dashboard of the PRs that need you

Lists the authenticated viewer's open pull requests -- both the ones you authored
and the ones you were asked to review -- with at-a-glance status: CI verdict,
review decision, merge-conflict state, and a diffstat. Filter to the ones that
need attention, group by repository or view a flat list, sort, and search by
title or repo; your filter, grouping, and sort choices persist between visits.

![The PR dashboard: open pull requests grouped by repository with CI, review, and draft status signals](docs/images/pr-list.png)

### A code-aware diff view

Opening a PR fetches the full repo source at the head commit (via the GitHub
tarball, cached on disk) and renders each changed file as a full-file diff in a
Monaco editor -- the same editor core as VS Code. From there you can:

- **Open any file in the repo**, not just the changed ones, to trace context.
- **Find usages** of a symbol across the entire tree (ripgrep), definitions
  floated to the top.
- **Hover and go-to-definition** for Python (Jedi) and JavaScript / TypeScript
  (tree-sitter) -- covering `.js/.jsx/.ts/.tsx` and their `.mjs/.cjs/.mts/.cts`
  variants -- resolving the repo's own code with zero setup.

### Rich types (opt-in, per repo)

The zero-setup engines resolve the repo's own symbols but not third-party
members -- tree-sitter is only a parser, and Jedi has no dependencies to resolve
against. The **Enable rich types** pill in a PR's header opts a repo into real
type resolution for both languages at once: a single headless agent installs the
repo's JS/TS dependencies (plus a pinned TypeScript language server) and its
Python dependencies (into an isolated virtualenv). After that, hover and
go-to-definition resolve member and inferred types, including from libraries.

A finished setup is keyed by a fingerprint of the repo's dependency files (not
the commit), shared across PRs and pushes, and re-used automatically the next
time the dependencies match -- so it only re-runs when dependencies actually
change.

### Review and act, without leaving the app

Post general comments, submit line-comment reviews (comment / approve / request
changes), edit the PR title and description, and close, reopen, or merge
(merge / squash / rebase) -- from the detail page or a right-click menu on the
dashboard, each behind a confirm step.

It also does the two things GitHub only exposes through its GraphQL API:
**resolve or reopen review-comment threads**, and **mark a draft ready for
review** (or convert a ready PR back to a draft). Each line comment shows whether
its thread is resolved, with a one-click Resolve / Unresolve control in both the
conversation and the in-diff view.

![A review-comment thread showing its resolved state and a one-click Unresolve control](docs/images/thread-resolution.png)

## How GitHub access works

Every GitHub call -- REST and GraphQL alike -- goes through `latchkey`, so your
stored credentials are injected transparently and no token ever lives in this
process. The transport is a single seam (`github._curl`); each network function
takes an injectable `curl` parameter that defaults to it, which is how the whole
test suite runs without touching the network. REST requests use `gh_json` /
`gh_request`; the GraphQL-only operations (resolve/reopen a review thread, mark
ready / convert to draft, read thread state) go through `gh_graphql`, which POSTs
to the `/graphql` endpoint over that same seam.

> The GraphQL features require a `latchkey` new enough to expose the
> `github-graphql-api` scope (2.20.2 or later). On older `latchkey` those actions
> are denied by the gateway while every REST feature keeps working.

## Layout

- `src/pr_review/runner.py` -- the Flask app and routes.
- `src/pr_review/github.py` -- GitHub access (REST via `gh_json`/`gh_request`,
  GraphQL via `gh_graphql`), status enrichment, the repo-tree cache, and ripgrep
  find-usages.
- `src/pr_review/pyintel.py` -- Jedi-backed hover and go-to-definition (Python),
  pinned to the repo's own source roots, and run against the prepared virtualenv
  when rich types are enabled.
- `src/pr_review/jsintel.py` -- tree-sitter-backed hover and go-to-definition
  (JavaScript / TypeScript): declaration signatures + doc comments, resolved
  locally and across relative imports.
- `src/pr_review/prepare.py` -- the opt-in "rich types" state machine: launches
  the headless install agent, tracks state under `.pr-review-prep/`, and shares
  finished setups across checkouts via a dependency-fingerprint-keyed store.
- `src/pr_review/tsintel.py` + `assets/tsintel_server.mjs` -- rich hover /
  go-to-definition via a persistent TypeScript language service (falls back to
  jsintel on any error).
- `src/pr_review/assets/` -- the frontend (`index.html`, `app.js`, `app.css`);
  Monaco loads from a CDN and all fetches are relative, so the app works behind a
  path-prefix proxy.
- `src/pr_review/testing.py` -- test helpers (`FakeCurl` and friends).

## Testing

```
cd libs/pr_review && uv run pytest
```

Tests never make real network calls, real `latchkey` calls, or real writes: the
`curl` transport is injected as a `FakeCurl`, and repo-tree-backed routes are
served from a pre-seeded on-disk cache. On-disk behavior (the cache, ripgrep,
Jedi, tree-sitter, the path-traversal guards) runs for real against trees built
in a temporary directory. The rich-types paths are seam-injected too, so the
prepare agent and the TypeScript language-service process are never spawned for
real in the suite.
