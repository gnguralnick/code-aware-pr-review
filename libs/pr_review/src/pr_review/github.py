"""GitHub access for the PR-review service.

All GitHub calls go through ``latchkey curl`` so the user's stored credentials
are injected transparently -- there is never a token in this process. The
service fetches the authenticated viewer's open PRs (authored + review-requested)
with the status signals shown in the list, and lazily fetches+caches each PR's
full source tree (at the PR head commit) on disk so the diff view can render
files in full context and let the user open any file in the repo.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from loguru import logger

API = "https://api.github.com"
# All persistent state lives under DATA_DIR; the env override lets a throwaway
# instance run against a copied store on a spare port without touching the live
# one (see the update-service skill's data-isolation flow).
DATA_DIR = Path(os.environ.get("PR_REVIEW_DATA_DIR", "runtime/pr-review"))
REPO_CACHE = DATA_DIR / "repos"

# The transport seam: a callable that runs ``latchkey curl`` with the given
# argument list and returns stdout bytes. Every network function takes one as an
# injectable parameter defaulting to the real ``_curl`` -- production callers use
# the default, while tests pass a fake so no real GitHub call or write ever runs.
CurlFn = Callable[[list[str]], bytes]


class GitHubError(RuntimeError):
    """A GitHub call through latchkey failed."""


def _curl(args: list[str]) -> bytes:
    result = subprocess.run(
        ["latchkey", "curl", "-s", *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitHubError(f"latchkey curl failed ({result.returncode}): {result.stderr.decode(errors='replace')[:500]}")
    return result.stdout


def gh_json(path: str, curl: CurlFn = _curl) -> dict | list:
    """GET a GitHub REST endpoint and parse JSON. ``path`` is relative to the API root."""
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
    raw = curl([url])
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"non-JSON response from {url}: {raw[:300]!r}") from exc


_STATUS_MARKER = "\n__HTTP_STATUS__"


def gh_request(method: str, path: str, payload: dict | None = None, curl: CurlFn = _curl) -> dict:
    """Make a write request (POST/PATCH/DELETE) and return the parsed JSON body.

    Raises GitHubError with the API message on any non-2xx status.
    """
    url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
    args = ["-sS", "-w", _STATUS_MARKER + "%{http_code}", "-X", method, "-H", "Content-Type: application/json", url]
    if payload is not None:
        args = ["-d", json.dumps(payload), *args]
    out = curl(args).decode(errors="replace")
    idx = out.rfind(_STATUS_MARKER)
    status = int(out[idx + len(_STATUS_MARKER):]) if idx >= 0 else 0
    body_text = out[:idx] if idx >= 0 else out
    data = json.loads(body_text) if body_text.strip() else {}
    if status >= 300:
        message = data.get("message") if isinstance(data, dict) else None
        raise GitHubError(f"GitHub {method} {path} -> {status}: {message or body_text[:200]}")
    return data if isinstance(data, dict) else {"result": data}


def gh_graphql(query: str, variables: dict | None = None, curl: CurlFn = _curl) -> dict:
    """POST a GraphQL query/mutation to the ``/graphql`` endpoint and return its
    ``data`` object.

    Reuses the same ``curl`` transport as the REST helpers. Raises GitHubError on
    a transport failure, a non-2xx HTTP status, a non-JSON body, or a non-empty
    top-level ``errors`` array (the GraphQL error messages are included).
    """
    url = f"{API}/graphql"
    payload = {"query": query, "variables": variables or {}}
    args = ["-sS", "-w", _STATUS_MARKER + "%{http_code}", "-X", "POST", "-H", "Content-Type: application/json", url]
    args = ["-d", json.dumps(payload), *args]
    out = curl(args).decode(errors="replace")
    idx = out.rfind(_STATUS_MARKER)
    status = int(out[idx + len(_STATUS_MARKER):]) if idx >= 0 else 0
    body_text = out[:idx] if idx >= 0 else out
    try:
        body = json.loads(body_text) if body_text.strip() else {}
    except json.JSONDecodeError as exc:
        raise GitHubError(f"non-JSON GraphQL response: {body_text[:300]!r}") from exc
    if status >= 300:
        raise GitHubError(f"GitHub GraphQL -> {status}: {body_text[:200]}")
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors:
        messages = "; ".join(str(e.get("message", e)) for e in errors)
        raise GitHubError(f"GraphQL errors: {messages}")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise GitHubError(f"GraphQL response missing data: {body_text[:200]}")
    return data


def get_viewer(curl: CurlFn = _curl) -> str:
    """The authenticated user's login."""
    me = gh_json("user", curl)
    assert isinstance(me, dict)
    login = me.get("login")
    if not login:
        raise GitHubError(f"could not resolve viewer login: {me}")
    return login


# ---------------------------------------------------------------------------
# PR list + status enrichment
# ---------------------------------------------------------------------------


def _ci_verdict(check_runs: dict, combined: dict) -> dict:
    counts = {"success": 0, "failure": 0, "pending": 0, "neutral": 0}
    for run in check_runs.get("check_runs", []):
        if run.get("status") != "completed":
            counts["pending"] += 1
        elif run.get("conclusion") == "success":
            counts["success"] += 1
        elif run.get("conclusion") in ("failure", "timed_out", "cancelled"):
            counts["failure"] += 1
        else:
            counts["neutral"] += 1
    # Legacy combined commit status only counts when statuses actually exist --
    # the endpoint defaults to "pending" with zero statuses, which would wrongly
    # override a clean check-runs result.
    overall = combined.get("state") if combined.get("total_count", 0) > 0 else None
    if counts["failure"] or overall == "failure":
        verdict = "failing"
    elif counts["pending"] or overall == "pending":
        verdict = "pending"
    elif counts["success"] or overall == "success":
        verdict = "passing"
    else:
        verdict = "none"
    return {"verdict": verdict, "counts": counts}


def _review_decision(reviews: list) -> str:
    by_user: dict[str, str] = {}
    for review in reviews:
        state = review.get("state")
        login = (review.get("user") or {}).get("login")
        if login and state in ("APPROVED", "CHANGES_REQUESTED"):
            by_user[login] = state
    states = set(by_user.values())
    if "CHANGES_REQUESTED" in states:
        return "changes requested"
    if "APPROVED" in states:
        return "approved"
    if reviews:
        return "commented"
    return "none"


def _summarize_search_item(item: dict, viewer: str) -> dict:
    """A lightweight row from a search result (no per-PR extra calls)."""
    repo = item["repository_url"].replace(f"{API}/repos/", "")
    return {
        "repo": repo,
        "number": item["number"],
        "title": item["title"],
        "author": (item.get("user") or {}).get("login"),
        "is_mine": (item.get("user") or {}).get("login") == viewer,
        "state": "draft" if item.get("draft") else "ready",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "comments": item.get("comments", 0),
        "url": item.get("html_url"),
    }


def list_prs(viewer: str, curl: CurlFn = _curl) -> dict:
    """Both buckets of the viewer's open PRs, lightweight (no per-PR enrichment)."""
    authored = gh_json(f"search/issues?q=is:open+is:pr+author:{viewer}&per_page=100", curl)
    requested = gh_json(f"search/issues?q=is:open+is:pr+review-requested:{viewer}&per_page=100", curl)
    assert isinstance(authored, dict) and isinstance(requested, dict)
    return {
        "viewer": viewer,
        "authored": [_summarize_search_item(it, viewer) for it in authored.get("items", [])],
        "review_requested": [_summarize_search_item(it, viewer) for it in requested.get("items", [])],
    }


def enrich_status(repo: str, number: int, curl: CurlFn = _curl) -> dict:
    """The full status signals for one PR (CI, review decision, conflicts, diffstat)."""
    pr = gh_json(f"repos/{repo}/pulls/{number}", curl)
    assert isinstance(pr, dict)
    sha = pr["head"]["sha"]
    check_runs = gh_json(f"repos/{repo}/commits/{sha}/check-runs", curl)
    combined = gh_json(f"repos/{repo}/commits/{sha}/status", curl)
    reviews = gh_json(f"repos/{repo}/pulls/{number}/reviews", curl)
    assert isinstance(check_runs, dict) and isinstance(combined, dict) and isinstance(reviews, list)
    return {
        "repo": repo,
        "number": number,
        # The PR's GraphQL node id (from the REST response) -- the handle the
        # draft <-> ready-for-review mutations take.
        "node_id": pr.get("node_id"),
        "title": pr["title"],
        "body": pr.get("body") or "",
        "author": pr["user"]["login"],
        "state": "draft" if pr.get("draft") else "ready",
        "base": pr["base"]["ref"],
        "base_sha": pr["base"]["sha"],
        "head": pr["head"]["ref"],
        "head_sha": sha,
        "head_repo": (pr["head"].get("repo") or {}).get("full_name", repo),
        "created_at": pr["created_at"],
        "updated_at": pr["updated_at"],
        "ci": _ci_verdict(check_runs, combined),
        "review_decision": _review_decision(reviews),
        "has_conflicts": pr.get("mergeable_state") == "dirty",
        "mergeable_state": pr.get("mergeable_state"),
        "diffstat": {
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "commits": pr.get("commits", 0),
        },
        "comment_counts": {
            "general": pr.get("comments", 0),
            "line_level": pr.get("review_comments", 0),
            "reviews": len(reviews),
        },
        "url": pr["html_url"],
    }


def list_changed_files(repo: str, number: int, curl: CurlFn = _curl) -> list[dict]:
    """Changed files for a PR (paginated)."""
    files: list[dict] = []
    # GitHub caps PR files at 3000; 30 pages of 100 covers any PR.
    for page in range(1, 31):
        chunk = gh_json(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}", curl)
        assert isinstance(chunk, list)
        if not chunk:
            break
        for entry in chunk:
            files.append({
                "path": entry["filename"],
                "previous_path": entry.get("previous_filename"),
                "status": entry["status"],
                "additions": entry.get("additions", 0),
                "deletions": entry.get("deletions", 0),
                # GitHub omits ``patch`` for binary files, but also for entries with
                # no textual diff to show -- notably a pure rename or copy (content
                # unchanged). additions/deletions don't disambiguate (binary files
                # report 0/0 too), so key off status: only a patchless *modified*
                # file is treated as binary; a renamed/copied entry without a patch
                # is an unchanged move, not binary.
                "is_binary": (
                    entry.get("patch") is None
                    and entry["status"] not in ("added", "removed", "renamed", "copied")
                ),
            })
        if len(chunk) < 100:
            break
    return files


# ---------------------------------------------------------------------------
# Review-comment thread state (GraphQL-only: resolve / unresolve)
# ---------------------------------------------------------------------------


_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isCollapsed
          comments(first: 100) { nodes { databaseId path } }
        }
      }
    }
  }
}
"""


def list_review_threads(repo: str, number: int, curl: CurlFn = _curl) -> list[dict]:
    """Every review-comment thread on a PR with its resolution state.

    Each thread's ``comment_ids`` are the review comments' ``databaseId`` values,
    which equal the REST review-comment ``id`` -- the join the frontend uses to
    map a REST line comment to its GraphQL thread. Capped at the first 100 threads
    (each with its first 100 comments), which covers any realistic PR.
    """
    owner, name = repo.split("/", 1)
    data = gh_graphql(_REVIEW_THREADS_QUERY, {"owner": owner, "name": name, "number": number}, curl)
    pull_request = (data.get("repository") or {}).get("pullRequest") or {}
    nodes = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    threads: list[dict] = []
    for node in nodes:
        comment_nodes = (node.get("comments") or {}).get("nodes") or []
        comment_ids = [c["databaseId"] for c in comment_nodes if c.get("databaseId") is not None]
        threads.append({
            "id": node["id"],
            "is_resolved": bool(node.get("isResolved")),
            "comment_ids": comment_ids,
        })
    return threads


_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""

_UNRESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  unresolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def resolve_review_thread(thread_id: str, curl: CurlFn = _curl) -> dict:
    """Mark a review-comment thread resolved. ``thread_id`` is its GraphQL node id."""
    data = gh_graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id}, curl)
    return (data.get("resolveReviewThread") or {}).get("thread") or {}


def unresolve_review_thread(thread_id: str, curl: CurlFn = _curl) -> dict:
    """Reopen a resolved review-comment thread. ``thread_id`` is its GraphQL node id."""
    data = gh_graphql(_UNRESOLVE_THREAD_MUTATION, {"threadId": thread_id}, curl)
    return (data.get("unresolveReviewThread") or {}).get("thread") or {}


# ---------------------------------------------------------------------------
# Draft <-> ready-for-review (GraphQL-only)
# ---------------------------------------------------------------------------


def get_pr_node_id(repo: str, number: int, curl: CurlFn = _curl) -> str:
    """The PR's GraphQL node id, via a single REST PR fetch.

    The draft <-> ready-for-review mutations take the node id; this resolves it
    with one lightweight ``GET /repos/{repo}/pulls/{number}`` rather than the
    full ``enrich_status`` (which fires several calls to assemble CI/review/diff
    signals the toggle does not need). Raises GitHubError if the id is missing.
    """
    pr = gh_json(f"repos/{repo}/pulls/{number}", curl)
    assert isinstance(pr, dict)
    node_id = pr.get("node_id")
    if not node_id:
        raise GitHubError(f"could not resolve the node id for {repo}#{number}")
    return node_id


_MARK_READY_MUTATION = """
mutation($pullRequestId: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
    pullRequest { id isDraft }
  }
}
"""

_CONVERT_TO_DRAFT_MUTATION = """
mutation($pullRequestId: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
    pullRequest { id isDraft }
  }
}
"""


def mark_pr_ready(pr_node_id: str, curl: CurlFn = _curl) -> dict:
    """Take a draft PR out of draft. ``pr_node_id`` is the PR's GraphQL node id."""
    data = gh_graphql(_MARK_READY_MUTATION, {"pullRequestId": pr_node_id}, curl)
    return (data.get("markPullRequestReadyForReview") or {}).get("pullRequest") or {}


def convert_pr_to_draft(pr_node_id: str, curl: CurlFn = _curl) -> dict:
    """Convert a ready PR back to a draft. ``pr_node_id`` is the PR's GraphQL node id."""
    data = gh_graphql(_CONVERT_TO_DRAFT_MUTATION, {"pullRequestId": pr_node_id}, curl)
    return (data.get("convertPullRequestToDraft") or {}).get("pullRequest") or {}


# ---------------------------------------------------------------------------
# Conversation (read) + write-back (comments, reviews, edits)
# ---------------------------------------------------------------------------


def get_conversation(repo: str, number: int, curl: CurlFn = _curl) -> dict:
    """The PR's general comments, reviews, and line-level review comments.

    Each line-level ``review_comments`` entry is enriched with its GraphQL
    ``thread_id`` and ``is_resolved`` (matched by REST comment id <-> GraphQL
    ``databaseId``). A comment with no matching thread gets ``thread_id: None,
    is_resolved: False``. Thread state is fetched best-effort: if the GraphQL
    call fails (e.g. the credential lacks the GraphQL scope) the comments still
    render, just without resolution state.
    """
    issue_comments = gh_json(f"repos/{repo}/issues/{number}/comments?per_page=100", curl)
    reviews = gh_json(f"repos/{repo}/pulls/{number}/reviews?per_page=100", curl)
    review_comments = gh_json(f"repos/{repo}/pulls/{number}/comments?per_page=100", curl)
    assert isinstance(issue_comments, list) and isinstance(reviews, list) and isinstance(review_comments, list)

    # Map each review-comment id to its thread, so line comments can show whether
    # their thread is resolved. Degrade gracefully if thread state can't be read.
    thread_by_comment_id: dict[int, dict] = {}
    try:
        threads = list_review_threads(repo, number, curl)
    except GitHubError as exc:
        logger.warning("Failed to fetch review-thread state for {} #{}: {}", repo, number, exc)
        threads = []
    for thread in threads:
        for comment_id in thread["comment_ids"]:
            thread_by_comment_id[comment_id] = thread

    def _user(obj: dict) -> str:
        return (obj.get("user") or {}).get("login", "?")

    return {
        "comments": [
            {"id": c["id"], "user": _user(c), "created_at": c["created_at"], "body": c.get("body") or "", "url": c.get("html_url")}
            for c in issue_comments
        ],
        "reviews": [
            {"id": r["id"], "user": _user(r), "state": r.get("state"), "submitted_at": r.get("submitted_at"), "body": r.get("body") or "", "url": r.get("html_url")}
            for r in reviews
            if r.get("state") != "PENDING"
        ],
        "review_comments": [
            {
                "id": rc["id"], "user": _user(rc), "path": rc["path"],
                "line": rc.get("line") or rc.get("original_line"),
                "side": rc.get("side", "RIGHT"), "body": rc.get("body") or "",
                "created_at": rc.get("created_at"), "url": rc.get("html_url"),
                # ``diff_hunk`` is the surrounding diff snippet GitHub shows above a
                # line comment; ``in_reply_to_id`` links a reply to its thread root.
                "diff_hunk": rc.get("diff_hunk") or "",
                "in_reply_to_id": rc.get("in_reply_to_id"),
                # GraphQL thread state, matched by REST comment id <-> databaseId.
                "thread_id": (thread_by_comment_id.get(rc["id"]) or {}).get("id"),
                "is_resolved": (thread_by_comment_id.get(rc["id"]) or {}).get("is_resolved", False),
            }
            for rc in review_comments
        ],
    }


def add_issue_comment(repo: str, number: int, body: str, curl: CurlFn = _curl) -> dict:
    """Post a general (conversation) comment on the PR."""
    return gh_request("POST", f"repos/{repo}/issues/{number}/comments", {"body": body}, curl)


def delete_issue_comment(repo: str, comment_id: int, curl: CurlFn = _curl) -> None:
    """Delete a general comment (used for clean test round-trips)."""
    gh_request("DELETE", f"repos/{repo}/issues/comments/{comment_id}", curl=curl)


def update_pr(repo: str, number: int, fields: dict, curl: CurlFn = _curl) -> dict:
    """Edit the PR title and/or body."""
    allowed = {k: v for k, v in fields.items() if k in ("title", "body")}
    if not allowed:
        raise GitHubError("nothing to update (expected title and/or body)")
    return gh_request("PATCH", f"repos/{repo}/pulls/{number}", allowed, curl)


def set_pr_state(repo: str, number: int, state: str, curl: CurlFn = _curl) -> dict:
    """Close or reopen a PR. ``state`` is "closed" or "open"."""
    if state not in ("open", "closed"):
        raise GitHubError(f"invalid state: {state!r} (expected 'open' or 'closed')")
    return gh_request("PATCH", f"repos/{repo}/pulls/{number}", {"state": state}, curl)


def merge_pr(repo: str, number: int, method: str = "merge", curl: CurlFn = _curl) -> dict:
    """Merge a PR. ``method`` is "merge", "squash", or "rebase"."""
    if method not in ("merge", "squash", "rebase"):
        raise GitHubError(f"invalid merge method: {method!r}")
    return gh_request("PUT", f"repos/{repo}/pulls/{number}/merge", {"merge_method": method}, curl)


def create_review(
    repo: str, number: int, commit_id: str, body: str, event: str, comments: list[dict], curl: CurlFn = _curl
) -> dict:
    """Create a review. ``event`` is one of COMMENT / APPROVE / REQUEST_CHANGES,
    or empty/"PENDING_CREATE" to leave it pending (used for clean test round-trips).
    ``comments`` are ``{path, line, side, body}`` line-level comments.
    """
    payload: dict = {"commit_id": commit_id, "comments": comments}
    if body:
        payload["body"] = body
    if event and event != "PENDING_CREATE":
        payload["event"] = event
    return gh_request("POST", f"repos/{repo}/pulls/{number}/reviews", payload, curl)


def delete_pending_review(repo: str, number: int, review_id: int, curl: CurlFn = _curl) -> None:
    """Delete a still-pending review (used for clean test round-trips)."""
    gh_request("DELETE", f"repos/{repo}/pulls/{number}/reviews/{review_id}", curl=curl)


# ---------------------------------------------------------------------------
# Repo source-tree fetch + cache (the "auto-clone")
# ---------------------------------------------------------------------------


class RepoTree(NamedTuple):
    """An extracted source tree at a specific commit, on disk."""

    repo: str
    sha: str
    root: Path


def repo_slug(repo: str) -> str:
    """A filesystem-safe directory name for a ``owner/name`` repo.

    Public so other modules that key on-disk caches by repo (e.g. the prep store
    in ``prepare``) share this exact scheme instead of reaching for a private helper.
    """
    return repo.replace("/", "__")


def ensure_repo_tree(repo: str, sha: str, curl: CurlFn = _curl) -> RepoTree:
    """Fetch+extract the repo at ``sha`` (cached). Reuses existing GitHub auth.

    ``repo`` is the full ``owner/name`` of the repo that hosts the commit (for a
    PR this is the head repo, which may be a fork).
    """
    dest = REPO_CACHE / repo_slug(repo) / sha
    marker = dest / ".extracted"
    if marker.exists():
        root = next(p for p in dest.iterdir() if p.is_dir())
        return RepoTree(repo=repo, sha=sha, root=root)

    dest.mkdir(parents=True, exist_ok=True)
    tarball = dest / "src.tar.gz"
    # The tarball endpoint 302-redirects to codeload; -L follows it with auth.
    raw = curl(["-L", f"{API}/repos/{repo}/tarball/{sha}"])
    tarball.write_bytes(raw)
    with tarfile.open(tarball, "r:gz") as tf:
        _safe_extract(tf, dest)
    tarball.unlink()
    marker.write_text(sha)
    root = next(p for p in dest.iterdir() if p.is_dir())
    return RepoTree(repo=repo, sha=sha, root=root)


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract guarding against path traversal outside ``dest``."""
    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved)):
            raise GitHubError(f"unsafe path in tarball: {member.name}")
    # The explicit check above is the primary guard; the "data" filter is a
    # second line of defense (and silences the 3.14 default-filter warning).
    tf.extractall(dest, filter="data")


def read_tree_file(tree: RepoTree, rel_path: str) -> str | None:
    """Read a file from an extracted tree. None if missing or binary."""
    target = (tree.root / rel_path).resolve()
    if not str(target).startswith(str(tree.root.resolve())):
        raise GitHubError(f"path escapes tree: {rel_path}")
    if not target.is_file():
        return None
    data = target.read_bytes()
    if b"\x00" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def list_tree_files(tree: RepoTree) -> list[str]:
    """All file paths in the tree, relative to its root (sorted)."""
    root = tree.root
    out: list[str] = []
    for path in root.rglob("*"):
        rel = str(path.relative_to(root)) if path.is_file() else ""
        # Skip VCS internals and installed dependencies: node_modules is only
        # present after an opt-in "prepare" install and would otherwise swamp
        # the file list and search results with third-party code.
        skip = ".git/" in str(path) or "node_modules/" in (rel + "/") or ".pr-review-prep/" in (rel + "/")
        if rel and not skip:
            out.append(rel)
    out.sort()
    return out


_RG = shutil.which("rg") or "rg"
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A line "looks like" a definition of ``sym`` when a definition keyword precedes
# it, or it is assigned/typed at the start of the line. Heuristic, language-
# agnostic -- used only to float likely definitions to the top of usage results.
_DEF_KEYWORDS = "def|class|func|fn|function|struct|interface|type|enum|impl|trait|const|let|var|module|package"


def _looks_like_def(text: str, symbol: str) -> bool:
    esc = re.escape(symbol)
    if re.search(rf"\b(?:{_DEF_KEYWORDS})\b[^=]*\b{esc}\b", text):
        return True
    if re.search(rf"#\s*define\s+{esc}\b", text):
        return True
    return bool(re.match(rf"\s*{esc}\s*[:=]", text))


def find_usages(tree: "RepoTree", symbol: str, limit: int = 400) -> dict:
    """Every whole-word occurrence of ``symbol`` in the tree, definitions first.

    Powered by ripgrep over the cached source -- language-agnostic find-usages
    plus a heuristic guess at which occurrences are the definition.
    """
    if not _SYMBOL_RE.fullmatch(symbol):
        raise GitHubError(f"invalid symbol: {symbol!r}")
    proc = subprocess.run(
        # Exclude node_modules: it is only present after an opt-in "prepare"
        # install, and searching third-party dependency code is noise.
        [_RG, "--json", "--word-regexp", "--fixed-strings", "--glob", "!node_modules", "--", symbol, "."],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
    )
    # rg exits 1 when there are simply no matches -- not an error.
    if proc.returncode not in (0, 1):
        raise GitHubError(f"ripgrep failed: {proc.stderr[:300]}")
    results: list[dict] = []
    truncated = False
    for raw in proc.stdout.splitlines():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        text = data["lines"]["text"].rstrip("\n")
        if "\x00" in text:  # binary line
            continue
        submatches = data.get("submatches") or [{"start": 0}]
        results.append({
            "path": data["path"]["text"],
            "line": data["line_number"],
            "col": submatches[0]["start"],
            "text": text[:240],
            "is_def": _looks_like_def(text, symbol),
        })
        if len(results) >= limit:
            truncated = True
            break
    results.sort(key=lambda r: (not r["is_def"], r["path"], r["line"]))
    return {
        "symbol": symbol,
        "total": len(results),
        "definitions": sum(1 for r in results if r["is_def"]),
        "truncated": truncated,
        "results": results,
    }


def get_file_at_ref(repo: str, path: str, ref: str, curl: CurlFn = _curl) -> str | None:
    """Base-version content of a file via the contents/blobs API. None if absent."""
    meta = gh_json(f"repos/{repo}/contents/{path}?ref={ref}", curl)
    if isinstance(meta, dict) and meta.get("message") == "Not Found":
        return None
    assert isinstance(meta, dict)
    content = meta.get("content")
    encoding = meta.get("encoding")
    if content and encoding == "base64":
        data = base64.b64decode(content)
        if b"\x00" in data[:8000]:
            return None
        return data.decode("utf-8", errors="replace")
    # Large files: contents API omits content; fall back to the blob by sha.
    sha = meta.get("sha")
    if sha:
        blob = gh_json(f"repos/{repo}/git/blobs/{sha}", curl)
        assert isinstance(blob, dict)
        if blob.get("encoding") == "base64" and blob.get("content"):
            data = base64.b64decode(blob["content"])
            if b"\x00" in data[:8000]:
                return None
            return data.decode("utf-8", errors="replace")
    return None
