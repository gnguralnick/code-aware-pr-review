# pr-review

A code-aware web interface for reviewing your GitHub pull requests, served at
`/service/pr-review/` (port 8082).

## What it does

1. **PR list.** Lists the authenticated viewer's open PRs (authored +
   review-requested) with status signals -- CI verdict, review decision,
   merge-conflict state, and diffstat -- enriched lazily per row. A left status
   strip (with a legend and per-row tooltip) summarizes each PR at a glance, and
   a toolbar offers repo grouping (or a flat list), sorting, and a title/repo
   search; the filter / grouping / sort choices persist in `localStorage`.
2. **Code-aware diff view.** On opening a PR it fetches the full repo source at
   the PR head commit (GitHub tarball) and caches it under
   `runtime/pr-review/repos/`, then renders changed files as full-file diffs in a
   Monaco editor. You can open any file in the repo, find-usages across the whole
   tree (ripgrep), and get code-aware hover / go-to-definition for Python (Jedi)
   and for JavaScript / TypeScript (tree-sitter -- `.js/.jsx/.ts/.tsx` and their
   `.mjs/.cjs/.mts/.cts` variants).
3. **Rich types (opt-in, per repo).** The zero-setup engines resolve the repo's
   own code but not third-party members -- tree-sitter is only a parser (e.g.
   `session.fromPartition` from `require('electron')` is opaque), and Jedi, while
   type-aware, has no dependencies installed to resolve against. The "Types:
   basic / Enable rich" pill in the detail header opts a repo into real type
   resolution for **both languages in one action**: it launches a single headless
   agent (`claude -p`) inside the cached tree that installs whatever is present --
   the repo's JS/TS dependencies (npm / pnpm / ...) plus a pinned TypeScript 5.x
   language server, and/or the repo's Python dependencies into an isolated venv at
   `.pr-review-prep/venv` (uv / pip / poetry). After that, JS/TS hover /
   go-to-definition come from the TypeScript language service (member + inferred
   types, library `.d.ts`), and Python hover / go-to-definition run Jedi against
   the prepared venv (third-party + inferred types); both fall back to their
   zero-setup engine on any error. State lives under `.pr-review-prep/` in the
   tree; "clear" removes it and the installed `node_modules` for that checkout
   (the shared store is left intact). A finished prep is shared across PRs and
   pushes: it is keyed by a fingerprint of the repo's dependency files
   (`package.json` + lockfiles, `pyproject.toml` / `uv.lock` / `requirements*.txt`
   / ...) rather than the commit SHA and published to a store under
   `runtime/pr-review/prep/` (bounded by an LRU cap per repo), so any later
   checkout whose dependencies match reuses it -- the JS artifacts by symlink, the
   Python venv referenced in place -- instead of reinstalling. When the
   dependencies match an existing prep exactly, rich types **auto-enable**
   silently (no agent) -- the pill flips to "rich" on its own. Only a genuine
   install (new or changed dependencies) launches the agent, which runs the
   packages' install scripts and spends Claude usage; that remains strictly manual
   behind the Enable action, and it is seeded from the repo's nearest prior prep
   so it updates incrementally rather than from scratch. Note: `npm install
   typescript` now resolves to TypeScript 7.x, whose npm package lacks the classic
   language service API -- the agent pins `typescript@5` for this reason.
4. **Write-back.** Post general comments, submit line-comment reviews
   (comment / approve / request-changes), edit the PR title/description, resolve
   or reopen review-comment threads, mark a draft ready for review (or convert a
   ready PR back to a draft), and close / reopen or merge a PR (merge / squash /
   rebase) -- from the detail page or a home-page right-click menu, behind a
   confirm step. Thread resolution and the draft toggle go through GitHub's
   GraphQL API (the operations GitHub exposes only there); everything else is
   REST. A line comment in the conversation and the in-diff view shows whether
   its thread is resolved, with a one-click Resolve / Unresolve control.

## How GitHub access works

Every GitHub call -- REST and GraphQL alike -- goes through `latchkey curl`, so
the user's stored credentials are injected transparently and no token ever lives
in this process. The transport is a single seam (`github._curl`); each network
function takes an injectable `curl` parameter that defaults to it, which is how
the tests run without touching the network. REST requests use `gh_json` /
`gh_request`; the GraphQL-only operations (resolve/unresolve a review thread,
mark ready / convert to draft, read thread state) go through `gh_graphql`, which
POSTs to the `/graphql` endpoint over the same seam.

The CI verdict deliberately ignores GitHub's legacy combined-status endpoint when
it reports zero statuses: that endpoint defaults to `pending` with no statuses,
which would otherwise wrongly override a clean check-runs result.

## Layout

- `src/pr_review/runner.py` -- the Flask app and routes.
- `src/pr_review/github.py` -- GitHub access, status enrichment, the repo-tree
  cache, and ripgrep find-usages.
- `src/pr_review/pyintel.py` -- Jedi-backed hover and go-to-definition (Python):
  pins resolution to the repo's own source roots, and when the repo has been
  prepared, runs Jedi against the prepared venv so third-party types resolve too.
- `src/pr_review/jsintel.py` -- tree-sitter-backed hover and go-to-definition
  (JavaScript / TypeScript): declaration signatures + doc comments, and
  definitions resolved locally and across relative imports in the cached tree.
- `src/pr_review/prepare.py` -- the opt-in "rich types" state machine: launches
  the headless install/setup agent, tracks state under `.pr-review-prep/`, and
  shares finished preps across checkouts via a dependency-fingerprint-keyed store
  under `runtime/pr-review/prep/` (reuse by symlink, auto-enable, seeded installs).
- `src/pr_review/tsintel.py` -- rich hover / go-to-definition via a persistent
  TypeScript language service, used for prepared repos (falls back to jsintel).
- `src/pr_review/assets/tsintel_server.mjs` -- the Node language-service helper
  that `tsintel.py` drives (line-delimited JSON protocol).
- `src/pr_review/assets/` -- the frontend (`index.html`, `app.js`, `app.css`);
  Monaco loads from a CDN and all fetches are relative so the app works behind
  the system_interface proxy.
- `src/pr_review/testing.py` -- test helpers (`FakeCurl` and friends).

## Testing

```
cd libs/pr_review && uv run pytest
```

Tests never make real network calls, real `latchkey` calls, or real writes: the
`curl` transport is injected as a `FakeCurl`, and repo-tree-backed routes are
served from a pre-seeded on-disk cache. On-disk behavior (the cache, ripgrep,
Jedi, tree-sitter, the path-traversal guards) runs for real against trees built
in `tmp_path`. The rich-types paths are seam-injected too -- the prepare agent
launcher and the `tsintel` language-service process are never spawned for real in
the suite (the `claude -p` agent + Node language service are exercised by hand /
in the release check).
