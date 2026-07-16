---
title: Code-Aware PR Review
description: A cleaner, code-aware interface for reviewing your GitHub pull requests
thumbnail: inspiration-code-aware-pr-review.svg
---

# Code-Aware PR Review

This file is the manifest for the **Code-Aware PR Review** inspiration (slug:
`code-aware-pr-review`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A self-hosted web app for reviewing your own GitHub pull requests with real code
context, because GitHub's own diff UI shows you changed lines but almost none of
the surrounding code they touch. It solves that: when you open a PR it downloads
the entire repository at the PR's head commit and renders each changed file as a
full-file diff in a Monaco editor, so you read a change in the context of the
whole file, not a keyhole hunk. On top of that it adds code intelligence -- open
any file in the repo, find every usage of a symbol across the whole tree, and get
hover / go-to-definition for Python and JavaScript/TypeScript (with an opt-in
"rich types" mode that resolves third-party and inferred types too). When it is
running the user opens a single tab: a list of their open pull requests (ones
they authored plus ones they were asked to review) annotated with CI, review, and
merge-conflict status, and clicking any PR drops them into the code-aware review
view. From there they can act on the PR without leaving the app -- post comments,
submit a line-comment review (approve / request changes / comment), edit the
title and description, close/reopen or merge, resolve and reopen review-comment
threads, flip a PR between draft and ready-for-review, and even hand a question
to a headless agent that investigates inside the checked-out code.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `libs/pr_review`

`libs/pr_review` is the entire app -- a Python workspace package with both the
backend and the frontend:

- `src/pr_review/runner.py` is the Flask app and all HTTP routes. Its
  `pr-review` console-script entry point (`pr_review.runner:main`) starts the
  server on `127.0.0.1`, port `8082` (overridable via the `PR_REVIEW_PORT`
  env var).
- `src/pr_review/github.py` is the GitHub layer: PR listing, status enrichment,
  the write-back helpers, the repo-source-tree tarball cache, and ripgrep
  find-usages. Every network call flows through one injectable `latchkey curl`
  seam (`github._curl`) so no token ever lives in the process and the tests run
  without touching the network. Both REST and GraphQL ride that one seam:
  `gh_graphql` backs the GitHub-GraphQL-only actions -- resolving/reopening
  review-comment threads and toggling a PR between draft and ready-for-review --
  and each review comment is annotated with its thread's resolved state.
- `src/pr_review/pyintel.py` (Jedi) and `src/pr_review/jsintel.py`
  (tree-sitter) provide the zero-setup Python and JS/TS hover /
  go-to-definition. `src/pr_review/prepare.py` plus `src/pr_review/tsintel.py`
  and `src/pr_review/assets/tsintel_server.mjs` implement the opt-in "rich
  types" upgrade (a persistent TypeScript language service and a Jedi
  environment backed by the repo's installed dependencies).
- `src/pr_review/ask.py` and `src/pr_review/agent_stream.py` implement the "ask
  an agent" investigator and the shared plumbing for spawning a headless
  `claude -p` agent inside a cached checkout and streaming its progress.
- `src/pr_review/assets/` is the frontend (`index.html`, `app.js`, `app.css`);
  Monaco loads from a CDN and every fetch is relative, so the app works behind
  the system_interface proxy.

At runtime a single supervisord program named `pr-review` (added to
`supervisord.conf` for this snapshot) runs the service. Its command first calls
`python3 scripts/forward_port.py --url http://localhost:8082 --name pr-review`,
which registers the app with the system_interface reverse proxy so it is reachable
by name at `/service/pr-review/`, and then runs `uv run pr-review` to start the
Flask server on port 8082. The workspace wiring in `pyproject.toml` (the
`pr-review` entry in `[project].dependencies`, the `libs/pr_review` workspace
member, and the `pr-review = { workspace = true }` source) is what makes
`uv run pr-review` resolve. On-disk state lives under `runtime/pr-review/`:
downloaded repo trees are cached under `runtime/pr-review/repos/` and finished
rich-types preps under `runtime/pr-review/prep/`.

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

- requires_permission: github-api / github-read (user-approved; the adopting
  agent initiates this via a latchkey permission request during setup). Read
  access to the user's GitHub. Needed for everything on the read path in
  `github.py`: resolving the viewer (`GET /user`), listing authored and
  review-requested PRs, enriching each PR's CI / review / merge status, fetching
  PR metadata, changed files and the full conversation, and downloading the repo
  source tarball at the PR head commit.
- requires_permission: github-api / github-write-pulls (user-approved; the
  adopting agent initiates this via a latchkey permission request during setup).
  Write access to pull requests. Needed by the `github.py` write helpers that
  submit line-comment reviews (`POST .../pulls/{n}/reviews`), edit the PR title
  and body and change its open/closed state (`PATCH .../pulls/{n}`), and merge a
  PR (`PUT .../pulls/{n}/merge`).
- requires_permission: github-api / github-write-issues (user-approved; the
  adopting agent initiates this via a latchkey permission request during setup).
  Write access to issues. Needed for posting and deleting the general
  (conversation) comments on a PR (`POST`/`DELETE .../issues/{n}/comments`),
  which GitHub exposes under the issues API even for pull requests.
- requires_permission: github-graphql-api / any (user-approved; the adopting
  agent initiates this via a latchkey permission request during setup). Access
  to GitHub's GraphQL endpoint (`POST /graphql`). Needed by `github.py`'s
  `gh_graphql` for the GraphQL-only actions: reading review-thread resolution
  state, resolving/reopening threads (`resolveReviewThread` /
  `unresolveReviewThread`), and toggling draft state
  (`markPullRequestReadyForReview` / `convertPullRequestToDraft`). NOTE: the
  `github-graphql-api` scope exists only in latchkey `>= 2.20.2`; an adopter on
  an older latchkey must upgrade, or these specific features will be denied
  while the REST features keep working.

The "rich types" and "ask an agent" features additionally spawn a headless
`claude -p` (the Claude Code CLI) inside the container to install a repo's
dependencies and to investigate code. This is a runtime dependency, not an
external credential: the CLI is present by default in a mind's container and the
code uses the keyless `claude -p` path (it sets no API key), so there is no
`requires_secret` line for it -- just be aware these two features shell out to
`claude` on `PATH` and consume Claude usage.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the `use-inspiration` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the minds app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

- Scoped to a single authenticated viewer. The PR list is built from whoever
  `latchkey`'s GitHub credentials resolve to (`get_viewer` -> `GET /user`), and
  there is no notion of multiple users, teams, or org-wide review queues. An
  adopter who wants a shared/team view would need to generalize the list query
  (e.g. drive it off explicit repo/org filters rather than the single viewer)
  and rethink write-back attribution.
- Rich types depends on the cached tree's package managers resolving cleanly.
  The opt-in prepare agent runs the repo's real install (npm / pnpm / uv / pip /
  poetry) inside the checkout; repos with private registries, unusual build
  steps, or install scripts that need extra secrets may fail to prepare, in
  which case the app silently falls back to the zero-setup engines (tree-sitter
  / bare Jedi). An adopter targeting such repos may want to preinstall or
  customize the prepare step. Note the deliberate `typescript@5` pin (newer
  TypeScript dropped the classic language-service API the tool relies on).
- The internal port `8082` is hardcoded in the supervisord wiring (the
  `forward_port.py --url http://localhost:8082` call), and though the server
  itself reads `PR_REVIEW_PORT`, the two must agree. An adopter who needs a
  different port has to change both places.
- The GraphQL features (thread resolution, draft toggle) depend on a latchkey
  new enough to expose the `github-graphql-api` scope (`>= 2.20.2`). On an older
  latchkey those actions are denied by the gateway; the app does not yet detect
  this and disable the controls, so an adopter stuck on an old latchkey would see
  those buttons error rather than hide. The REST features are unaffected.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
