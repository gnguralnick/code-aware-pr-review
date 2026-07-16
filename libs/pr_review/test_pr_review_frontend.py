"""Browser tests for the diff-view changed-files sidebar.

The changed-files list renders a per-file comment-count badge
(``commentCountByPath`` + the ``.fcomments`` span in ``changedFilesHTML``).
That logic is pure client-side JavaScript with no server counterpart, so it is
exercised the only way that is faithful: load the real page in a real browser
against a throwaway in-process server, inject a crafted ``DETAIL`` (PR + files +
conversation), render the real detail shell, and assert on the rendered badges.
No GitHub network is touched -- the counted data is injected directly.

An integration test (browser-driven), skipped when Playwright's browsers are not
installed -- mirroring the plain-lib Playwright pattern in ``libs/browser``.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from pr_review.runner import app


def _playwright_browsers_installed() -> bool:
    """True when Playwright's Chromium cache exists (mirrors system_interface e2e)."""
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        cache_dir = Path(env_path)
    elif sys.platform == "darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
    return cache_dir.exists() and any(cache_dir.iterdir())


pytestmark = [
    pytest.mark.skipif(not _playwright_browsers_installed(), reason="Playwright browsers not installed"),
    # Real Chromium cannot start on the GitHub Actions runner -- the launch hangs even
    # after `playwright install` and with the sandbox off -- so skip there. The tests
    # still run locally and on offload, where a real browser actually comes up. This
    # mirrors libs/browser/test_browser_integration.py's real-Chromium skip guard.
    pytest.mark.skipif(
        os.environ.get("GITHUB_ACTIONS") == "true",
        reason="real Chromium can't start under the GitHub Actions runner; runs locally / on offload",
    ),
    # Chromium cold-start + navigation lives in the module fixtures (excluded by
    # ``timeout_func_only``); each test body only runs fast ``page.evaluate`` calls.
    # Set an explicit generous ceiling anyway -- mirroring libs/browser -- so the
    # test does not silently depend on the global 10s limit's func-only exclusion.
    pytest.mark.timeout(120),
]

_PORT = 18791

# Crafted detail exercising every branch of the badge logic:
#  - a.py: two top-level comments                 -> badge "2"
#  - b.js: one comment plus one reply             -> badge "2" (replies count)
#  - c.md: no comments                            -> no badge
#  - gone.py: a comment whose path is NOT in the  -> counted in the map but
#             diff                                    never rendered (not a file row)
#  - a pathless comment (path == null)            -> skipped by the guard
_DETAIL = {
    "pr": {
        "repo": "octo/demo", "number": 42, "title": "Demo PR",
        "url": "https://github.com/octo/demo/pull/42",
        "head": "feature", "base": "main", "state": "open",
        "ci": {"verdict": "passing"}, "review_decision": "none", "has_conflicts": False,
        "diffstat": {"additions": 10, "deletions": 3, "changed_files": 3},
    },
    "files": [
        {"path": "a.py", "status": "modified", "additions": 5, "deletions": 1},
        {"path": "b.js", "status": "modified", "additions": 4, "deletions": 2},
        {"path": "c.md", "status": "added", "additions": 1, "deletions": 0},
    ],
    "conversation": {
        "comments": [], "reviews": [],
        "review_comments": [
            {"id": 1, "user": "u", "path": "a.py", "line": 3, "body": "one", "in_reply_to_id": None},
            {"id": 2, "user": "u", "path": "a.py", "line": 4, "body": "two", "in_reply_to_id": None},
            {"id": 3, "user": "u", "path": "b.js", "line": 2, "body": "root", "in_reply_to_id": None},
            {"id": 4, "user": "u", "path": "b.js", "line": 2, "body": "reply", "in_reply_to_id": 3},
            {"id": 5, "user": "u", "path": "gone.py", "line": 1, "body": "orphan", "in_reply_to_id": None},
            {"id": 6, "user": "u", "path": None, "line": None, "body": "pathless", "in_reply_to_id": None},
        ],
    },
}


@pytest.fixture(scope="module")
def served_url() -> Iterator[str]:
    """A throwaway in-process pr-review server on an alternate port."""
    server = make_server("127.0.0.1", _PORT, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{_PORT}/"
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        instance = pw.chromium.launch(args=["--no-sandbox"])
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def sidebar_page(served_url: str, browser: Browser) -> Iterator[Page]:
    """A page with the crafted DETAIL rendered into the changed-files sidebar.

    ``DETAIL`` is a top-level ``let`` in app.js, so it is assigned by bare name
    (not via ``window``). The conversation card needs author fields the minimal
    fake PR omits, so its render error is tolerated -- the sidebar DOM (our
    target) is committed before that path runs.
    """
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    page.goto(served_url)
    page.wait_for_function("typeof changedFilesHTML === 'function'")
    page.evaluate(
        """(d) => {
            DETAIL = d;
            document.getElementById('list').classList.add('hidden');
            document.getElementById('detail').classList.remove('hidden');
            try { renderDetailShell(); } catch (e) {}
        }""",
        _DETAIL,
    )
    page.wait_for_selector("#sb-changed .fitem", state="attached")
    try:
        yield page
    finally:
        page.close()


def test_comment_count_by_path_counts_published_comments_including_replies(sidebar_page: Page) -> None:
    counts = sidebar_page.evaluate("commentCountByPath()")
    assert counts == {"a.py": 2, "b.js": 2, "gone.py": 1}


def test_badges_render_only_for_in_diff_files_with_comments(sidebar_page: Page) -> None:
    badges = sidebar_page.evaluate(
        """() => {
            const out = {};
            document.querySelectorAll('#sb-changed .fitem').forEach(it => {
                const c = it.querySelector('.fcomments');
                out[it.dataset.path] = c ? c.textContent.trim() : null;
            });
            return out;
        }"""
    )
    # a.py/b.js show the speech-balloon glyph + count; c.md (zero) shows nothing.
    assert badges["a.py"] == "\U0001f4ac 2"
    assert badges["b.js"] == "\U0001f4ac 2"
    assert badges["c.md"] is None
    # gone.py is not a changed file, so it never becomes a row at all.
    assert "gone.py" not in badges


def test_badge_title_pluralizes(sidebar_page: Page) -> None:
    title = sidebar_page.evaluate(
        "() => document.querySelector('#sb-changed .fitem[data-path=\"a.py\"] .fcomments').getAttribute('title')"
    )
    assert title == "2 comments on this file"


def test_badge_title_singular(sidebar_page: Page) -> None:
    # Re-key the injected conversation so a.py carries exactly one comment.
    title = sidebar_page.evaluate(
        """() => {
            DETAIL.conversation.review_comments = [
                {id: 9, user: 'u', path: 'a.py', line: 1, body: 'solo', in_reply_to_id: null},
            ];
            const html = changedFilesHTML();
            const doc = new DOMParser().parseFromString(html, 'text/html');
            return doc.querySelector('.fitem[data-path="a.py"] .fcomments').getAttribute('title');
        }"""
    )
    assert title == "1 comment on this file"


def test_missing_conversation_yields_no_counts(sidebar_page: Page) -> None:
    counts = sidebar_page.evaluate(
        "() => { const s = DETAIL.conversation; DETAIL.conversation = null;"
        " const r = commentCountByPath(); DETAIL.conversation = s; return r; }"
    )
    assert counts == {}
