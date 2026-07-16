"""Opt-in "prepare a repo for rich types": install dependencies and set up
type-resolution tooling for a cached repo tree, driven by a headless agent.

The zero-setup engines (``jsintel`` tree-sitter for JS/TS, ``pyintel`` Jedi over
the tree's own source for Python) resolve the repo's own code but not third-party
members (e.g. ``session.fromPartition`` from ``require('electron')``, or a
member of a pip-installed class). For the few repos a user actually reviews, this
module runs a one-shot ``claude -p`` agent *inside* the cached source tree to
install dependencies for whichever languages are present -- JavaScript/TypeScript
(npm / pnpm / ...) plus a pinned ``typescript``, and/or Python (a venv populated
via uv / pip / poetry) -- so the rich engines can resolve real types. One agent
handles both languages in a single run. The agent is used because the install
shape is too irregular to hardcode (npm vs pnpm, uv vs pip vs poetry, no root
manifest, multiple package dirs, monorepos).

State lives in a ``.pr-review-prep/`` sidecar next to the source root (not inside
it, so it never shows up in file listings); the Python venv lives at
``.pr-review-prep/venv``. ``tsintel`` consumes ``roots`` / ``typescript_dir`` and
``pyintel`` consumes ``python_venv`` from ``status.json`` once the state is
``ready``. Nothing here runs automatically -- it is triggered only by an explicit
user action, and it installs dependencies (running arbitrary ``postinstall`` /
build scripts), so it is strictly opt-in.

The ``claude -p`` invocation is a compact, dependency-free adaptation of the
copyable helper documented by the ``use-ai-integration`` skill
(``scripts/claude_p.py``): it keeps the load-bearing bits -- unsetting
``MAIN_CLAUDE_SESSION_ID`` so the child is not mistaken for the managed main
session, ``--permission-mode bypassPermissions`` for a headless run, and strict
parsing of the JSON result -- but runs in the tree's working directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from pr_review.agent_stream import AgentError, AgentRun, run_streaming_agent
from pr_review.github import DATA_DIR, RepoTree, repo_slug

PREP_DIRNAME = ".pr-review-prep"

# A completed prep (the isolated typescript@5 plus each project's node_modules)
# is keyed by a fingerprint of the repo's *dependency* files, not by commit SHA.
# Two PRs -- or two pushes to one PR -- whose package.json/lockfiles are identical
# reuse the same installed prep instead of re-running the multi-minute agent. The
# store lives outside any single checkout so it survives when disposable per-SHA
# trees are evicted.
PREP_STORE = DATA_DIR / "prep"

# The files whose contents define a dependency set. A commit that touches none of
# these produces the same fingerprint, so its prep is reusable. One fingerprint
# spans both languages: a single agent run prepares JS and Python together, so a
# change to either language's manifests re-preps (and re-shares) the whole thing.
_DEP_FILENAMES = frozenset(
    {
        # JavaScript / TypeScript
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        # Python
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.py",
        "setup.cfg",
    }
)

# Dependency files whose names vary (requirements.txt, requirements-dev.txt, ...).
_DEP_FILE_GLOBS = ("requirements*.txt",)

# Directories never descended into when fingerprinting or capturing artifacts.
_ARTIFACT_DIRNAMES = frozenset({"node_modules", PREP_DIRNAME, ".venv"})

# Setting up an install across an unfamiliar repo is real agentic reasoning, so
# default to a stronger model than the haiku default; the run is explicit and
# rare. The user can pick a different one per run from the dialog.
DEFAULT_MODEL = "claude-sonnet-4-6"
_ALLOWED_MODELS = ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5")
_AGENT_TIMEOUT_S = 1800


def normalize_model(model: str | None) -> str:
    """The requested model if it is one we allow, else the default."""
    return model if model in _ALLOWED_MODELS else DEFAULT_MODEL


_AGENT_PROMPT = """\
You are preparing a checked-out copy of a Git repository so that code-intelligence \
tools can resolve types for its source files, including types from third-party \
dependencies. The repository may contain JavaScript/TypeScript projects, Python \
projects, or both. Prepare EVERY language that is present.

Your current working directory is the root of the repository checkout. Put tooling \
you install under an isolated `.pr-review-prep/` directory at the repo root.

JavaScript / TypeScript -- do this only if the repo has package.json files outside \
node_modules:
1. Locate every package.json (ignore any under node_modules). Determine the package \
manager from the lockfile present (package-lock.json -> npm, pnpm-lock.yaml -> pnpm, \
yarn.lock -> yarn); default to npm if there is no lockfile.
2. Install dependencies in each relevant project directory with that package manager \
(e.g. `npm install`, `pnpm install --no-frozen-lockfile`). This can take several \
minutes; let it finish.
3. Install a TypeScript 5.x for the language server in an ISOLATED directory so it \
does not clobber the repo's own typescript and so we get the classic language \
service API (TypeScript 7.x does NOT expose it): `npm install --prefix \
.pr-review-prep typescript@5`. Do NOT rely on `npm install typescript` without a \
version (that now installs 7.x, which is unusable here).
4. If it helps resolution for plain JavaScript files, add a permissive \
`jsconfig.json` or `tsconfig.json` at a project root with `allowJs` enabled and \
`checkJs` disabled. Do NOT overwrite an existing config file.
5. Verify: `node -e "require.resolve('typescript')"` run with cwd `.pr-review-prep` \
succeeds, and `require('typescript').createLanguageService` is a function (5.x).

Python -- do this only if the repo has pyproject.toml / setup.py / setup.cfg / \
requirements*.txt / Pipfile / poetry.lock / uv.lock:
6. Create an ISOLATED virtual environment at `.pr-review-prep/venv`: \
`uv venv .pr-review-prep/venv`.
7. Install the repo's Python dependencies INTO that venv using the repo's own \
toolchain -- for a uv project/workspace `VIRTUAL_ENV=.pr-review-prep/venv uv sync \
--all-packages` (or `uv pip install --python .pr-review-prep/venv ...`); for a \
requirements file `.pr-review-prep/venv/bin/python -m pip install -r <file>`; for \
poetry, export to requirements and pip-install. Install the repo's own packages too \
(editable) so intra-repo imports resolve. Everything MUST land in that venv, never \
the system interpreter. This can take several minutes; let it finish.
8. Verify: `.pr-review-prep/venv/bin/python -c "import sys"` runs, and a \
representative third-party import the repo uses resolves in that venv.

Finally, write a JSON file at `.pr-review-prep/agent_result.json` with these keys \
(set a language's keys to null / [] if that language is not present):
   - "package_manager": JS package manager used (e.g. "npm" or "pnpm"), or null
   - "roots": array of JS project dirs (relative to repo root) you installed into, or []
   - "typescript_dir": ".pr-review-prep" if you set up TypeScript, else null
   - "python_venv": ".pr-review-prep/venv" if you set up a Python venv, else null
   - "notes": a short summary of what you did (both languages) and anything notable

Keep going until the applicable installs succeed and tooling resolves. Then give a \
concise final summary."""

_AGENT_APPEND_SYSTEM = (
    "You are preparing a repository checkout for type analysis. Only create or "
    "modify files inside the current working directory (the checkout). Do not "
    "touch anything outside it, and do NOT modify the host system: no `apt`/`brew`/"
    "`curl | sh`, no global or system-wide installs, no changing the installed "
    "Node/npm/pnpm/uv/Python versions. Use the package managers already on PATH; if "
    "a lockfile's engine constraints reject the available version, install with the "
    "engine check relaxed (e.g. `npm install --engine-strict=false`) rather than "
    "installing a different runtime. For Python, create and populate a virtualenv "
    "inside the checkout (`uv venv` / `uv sync` / the venv's own pip) -- never "
    "install into the system interpreter or change global state. The only shell "
    "commands you should run are for in-tree dependency installation and "
    "verification -- no destructive operations."
)


class PrepareError(RuntimeError):
    """Raised when the prepare agent fails to run or its output is unusable."""


# Launcher seam: production spawns a background thread that runs the real agent;
# tests inject a fake that writes a terminal status synchronously.
Launcher = Callable[[RepoTree], None]


def _prep_dir(tree: RepoTree) -> Path:
    # Lives at the source-tree root (the prepare agent runs with this as its cwd,
    # and may only write inside it). Excluded from file listing / search like
    # node_modules, so it never shows up in the UI.
    return tree.root / PREP_DIRNAME


def _status_path(tree: RepoTree) -> Path:
    return _prep_dir(tree) / "status.json"


def _log_path(tree: RepoTree) -> Path:
    return _prep_dir(tree) / "prepare.log"


def _agent_result_path(tree: RepoTree) -> Path:
    return _prep_dir(tree) / "agent_result.json"


def _iter_dep_files(root: Path) -> list[Path]:
    """Every dependency-defining file under ``root`` (skipping installed artifacts)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune artifact dirs in place so os.walk never descends into them.
        dirnames[:] = [d for d in dirnames if d not in _ARTIFACT_DIRNAMES]
        for name in filenames:
            if name in _DEP_FILENAMES or any(fnmatch(name, g) for g in _DEP_FILE_GLOBS):
                found.append(Path(dirpath) / name)
    return found


def dep_fingerprint(root: Path) -> str | None:
    """A stable hash of the repo's dependency files, or ``None`` if it has none.

    Keyed on each file's repo-relative path and byte contents, so two checkouts
    with identical dependency manifests -- regardless of commit SHA or any change
    to non-dependency source -- hash the same and can share an installed prep.
    Files are hashed on the *pristine* tree (before any install rewrites a
    lockfile), so a fresh checkout matches what a prior run published.
    """
    files = _iter_dep_files(root)
    if not files:
        return None
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:32]


# Bound the shared store: keep at most this many dependency-set preps per repo,
# evicting the least-recently-used on publish. Each entry is a full node_modules
# copy, so unbounded growth would eventually fill the disk; reuse bumps an entry's
# mtime (see _materialize) so actively-used dependency sets are the ones kept.
_MAX_STORE_ENTRIES_PER_REPO = 12


def _repo_store_dir(repo: str) -> Path:
    return PREP_STORE / repo_slug(repo)


def _store_entry(repo: str, fingerprint: str) -> Path:
    return _repo_store_dir(repo) / fingerprint


def _store_manifest_path(entry: Path) -> Path:
    return entry / "manifest.json"


def _entry_is_ready(entry: Path) -> bool:
    """Whether ``entry`` holds a complete, ready-state published prep."""
    manifest = _store_manifest_path(entry)
    status = entry / "prep" / "status.json"
    if not manifest.exists() or not status.exists():
        return False
    try:
        return json.loads(status.read_text()).get("state") == "ready"
    except (ValueError, OSError):
        return False


def reusable_entry(tree: RepoTree) -> Path | None:
    """The shared-store entry a pristine ``tree`` can reuse, or ``None``.

    Fingerprints the tree and returns the matching ready entry if one exists.
    Short-circuits before the fingerprint walk when nothing has ever been
    published for the repo -- ``auto_enable`` runs on every hover / poll, so the
    common "repo was never prepared" case must not pay a full-tree walk each time.
    """
    if not _repo_store_dir(tree.repo).is_dir():
        return None
    fingerprint = dep_fingerprint(tree.root)
    if fingerprint is None:
        return None
    entry = _store_entry(tree.repo, fingerprint)
    return entry if _entry_is_ready(entry) else None


def _publish(tree: RepoTree, fingerprint: str, roots: list[str]) -> None:
    """Copy a freshly-prepared tree's artifacts into the shared store.

    Captures the ``.pr-review-prep`` sidecar (with its pinned typescript@5) and
    each project root's ``node_modules`` under a fingerprint-keyed entry, so a
    later checkout with the same dependencies can reuse it without reinstalling.
    Best-effort: a failure here leaves the just-prepared tree fully working.
    """
    entry = _store_entry(tree.repo, fingerprint)
    if _entry_is_ready(entry):
        return  # another checkout already published this dependency set
    prep_src = _prep_dir(tree)
    if not prep_src.is_dir():
        return
    entry.parent.mkdir(parents=True, exist_ok=True)
    staging = entry.parent / f".staging-{fingerprint}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(prep_src, staging / "prep", symlinks=True)
        modules = staging / "modules"
        modules.mkdir()
        captured: list[dict] = []
        for idx, root in enumerate(roots):
            nm = (tree.root / root / "node_modules").resolve()
            if nm.is_dir() and nm.is_relative_to(tree.root.resolve()):
                shutil.copytree(nm, modules / str(idx), symlinks=True)
                captured.append({"root": root, "modules": str(idx)})
        _store_manifest_path(staging).write_text(
            json.dumps(
                {"fingerprint": fingerprint, "roots": roots, "modules": captured},
                indent=2,
            )
        )
        shutil.rmtree(entry, ignore_errors=True)
        os.replace(staging, entry)
    except (OSError, shutil.Error):
        shutil.rmtree(staging, ignore_errors=True)
        return
    _evict_store(tree.repo, keep=_MAX_STORE_ENTRIES_PER_REPO)


def _evict_store(repo: str, keep: int) -> None:
    """Keep only the ``keep`` most-recently-used store entries for ``repo``.

    Least-recently-used entries (by mtime, which reuse bumps) are removed. An
    evicted entry that some checkout still symlinks into simply makes that checkout
    fall back to ``absent`` -- it can be re-enabled -- so eviction never corrupts a
    live checkout, only reclaims disk. Best-effort.
    """
    base = _repo_store_dir(repo)
    if not base.is_dir():
        return
    entries = [e for e in base.iterdir() if e.is_dir() and not e.name.startswith(".staging-")]
    if len(entries) <= keep:
        return
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    for stale in entries[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _link(target: Path, source: Path) -> None:
    """Point ``target`` at ``source`` via a symlink, replacing whatever is there."""
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Absolute target: the store and checkout paths are relative to the app's cwd,
    # and a relative symlink would resolve against the link's own directory.
    try:
        target.symlink_to(source.resolve())
    except FileExistsError:
        # A concurrent auto-enable won the race; the link is already there.
        pass


def _materialize(tree: RepoTree, entry: Path) -> bool:
    """Symlink a store entry's artifacts into ``tree`` so its rich types work.

    Returns True once the prep sidecar and captured node_modules are linked in.
    """
    try:
        manifest = json.loads(_store_manifest_path(entry).read_text())
    except (ValueError, OSError):
        return False
    _link(_prep_dir(tree), entry / "prep")
    for captured in manifest.get("modules") or []:
        root = captured.get("root")
        rel = captured.get("modules")
        if not isinstance(root, str) or not isinstance(rel, str):
            continue
        src = entry / "modules" / rel
        if src.is_dir():
            _link(tree.root / root / "node_modules", src)
    # Bump the entry's mtime so LRU eviction (_evict_store) treats a reused
    # dependency set as fresh, not stale.
    try:
        os.utime(entry, None)
    except OSError:
        pass
    return True


def _ready_entries(repo: str) -> list[Path]:
    """All ready store entries for ``repo``, most recently published first."""
    base = _repo_store_dir(repo)
    if not base.is_dir():
        return []
    entries = [e for e in base.iterdir() if e.is_dir() and _entry_is_ready(e)]
    return sorted(entries, key=lambda e: e.stat().st_mtime, reverse=True)


def _prior_findings(entry: Path) -> dict:
    """The prior run's reported findings (package manager, roots, notes) for a store entry."""
    for name in ("agent_result.json", "status.json"):
        path = entry / "prep" / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _seed_from_prior(tree: RepoTree, entry: Path) -> dict:
    """Copy a prior prep's artifacts into ``tree`` as writable starting state.

    Unlike ``_materialize`` (symlinks into the shared store for an *exact* match),
    this makes independent *copies* -- the install agent will mutate them to
    reconcile the differing dependencies, so they must not alias the store. Copies
    the already-built typescript@5 sidecar and each prior root's ``node_modules``
    so the package manager updates incrementally instead of installing cold.
    Returns the prior findings for use as agent context. Best-effort throughout.
    """
    findings = _prior_findings(entry)
    try:
        manifest = json.loads(_store_manifest_path(entry).read_text())
    except (ValueError, OSError):
        manifest = {}
    prep_src = entry / "prep"
    prep_dst = _prep_dir(tree)
    if prep_src.is_dir():
        # start_prepare already wrote an ``installing`` status into prep_dst (and
        # detached any store symlink). MERGE the prior artifacts in on top of it,
        # skipping the prior run's metadata -- crucially status.json: copying its
        # ``ready`` state would make a status poll landing mid-copy flip the UI to
        # "rich" and stop polling, dropping the live install progress + log. The
        # prepare agent overwrites these artifacts as it reconciles dependencies.
        try:
            shutil.copytree(
                prep_src,
                prep_dst,
                symlinks=True,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "status.json", "agent_result.json", "prepare.log"
                ),
            )
        except (OSError, shutil.Error):
            pass
    for captured in manifest.get("modules") or []:
        root = captured.get("root")
        rel = captured.get("modules")
        if not isinstance(root, str) or not isinstance(rel, str):
            continue
        src = entry / "modules" / rel
        dst = tree.root / root / "node_modules"
        if src.is_dir() and (tree.root / root).is_dir() and not dst.exists():
            try:
                shutil.copytree(src, dst, symlinks=True)
            except (OSError, shutil.Error):
                pass
    return findings


def _seed_hint(findings: dict) -> str:
    """A prompt preamble describing prior-run state the seeded agent can build on."""
    parts = [
        "This repository was prepared before. That prior preparation's installed "
        "dependencies, its typescript@5 sidecar, and (if present) its Python venv at "
        ".pr-review-prep/venv have ALREADY been copied into this checkout under "
        ".pr-review-prep and the project dirs. Reconcile them with the current "
        "manifests -- rerun the package managers' install (npm/pnpm for JS, "
        "`VIRTUAL_ENV=.pr-review-prep/venv uv sync` or the venv's pip for Python), "
        "which updates incrementally -- instead of installing from scratch, and "
        "reuse the existing .pr-review-prep and venv rather than rebuilding them."
    ]
    pm = findings.get("package_manager")
    if isinstance(pm, str) and pm:
        parts.append(f"JS package manager used previously: {pm}.")
    roots = [r for r in (findings.get("roots") or []) if isinstance(r, str)]
    if roots:
        parts.append("JS project roots found previously: " + ", ".join(roots) + ".")
    if isinstance(findings.get("python_venv"), str) and findings.get("python_venv"):
        parts.append("A Python venv was prepared previously at .pr-review-prep/venv.")
    notes = findings.get("notes")
    if isinstance(notes, str) and notes:
        parts.append("Notes from the previous run (repo-specific gotchas):\n" + notes)
    return "\n\n".join(parts)


def _build_prompt(seed_hint: str | None) -> str:
    """The agent prompt, with an optional prior-preparation context section prepended."""
    if not seed_hint:
        return _AGENT_PROMPT
    return f"PRIOR PREPARATION CONTEXT (use it to go faster):\n{seed_hint}\n\n{_AGENT_PROMPT}"


def prepare_status(tree: RepoTree) -> dict:
    """The current prepare state for ``tree`` (``{"state": "absent"}`` if none)."""
    path = _status_path(tree)
    if not path.exists():
        return {"state": "absent"}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {"state": "absent"}


def is_ready(tree: RepoTree) -> bool:
    return prepare_status(tree).get("state") == "ready"


def auto_enable(tree: RepoTree) -> dict:
    """Silently enable rich types for ``tree`` iff it needs no install agent.

    When the tree has no prep yet and the shared store holds an exact
    dependency-fingerprint match, that reuse is free (symlinks, no agent), so we
    materialize it and report ``ready`` without the user asking. When an install
    would be required (no match, or only a partial one to seed from), this is a
    no-op and rich types stay opt-in behind the explicit Enable action.
    """
    current = prepare_status(tree)
    if current.get("state") != "absent":
        return current
    entry = reusable_entry(tree)
    if entry is None:
        return current
    try:
        if _materialize(tree, entry):
            return prepare_status(tree)
    except OSError:
        pass  # a later call retries; keep the app responsive
    return current


def ready_roots(tree: RepoTree) -> list[str]:
    """Project roots the agent set up, for a ready tree (empty otherwise)."""
    status = prepare_status(tree)
    if status.get("state") != "ready":
        return []
    roots = status.get("roots") or []
    return [r for r in roots if isinstance(r, str)]


def prepared_python_env(tree: RepoTree) -> str | None:
    """Path to the prepared venv's Python for ``tree``, or ``None``.

    Returns the interpreter ``pyintel`` should point Jedi at to resolve
    third-party types, once the tree is ready and a Python venv was prepared. The
    path is resolved through the ``.pr-review-prep`` symlink, so a reused checkout
    transparently points at the venv living in the shared store.
    """
    status = prepare_status(tree)
    if status.get("state") != "ready":
        return None
    venv_rel = status.get("python_venv")
    if not isinstance(venv_rel, str) or not venv_rel:
        return None
    # Resolve the venv *directory* (to follow the .pr-review-prep store symlink for a
    # reused checkout) but keep the venv-local bin/python -- resolving that symlink
    # would follow it to the base interpreter and lose the venv's own site-packages.
    python = (tree.root / venv_rel).resolve() / "bin" / "python"
    return str(python) if python.exists() else None


def log_tail(tree: RepoTree, lines: int = 50) -> str:
    path = _log_path(tree)
    if not path.exists():
        return ""
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


def _write_status(tree: RepoTree, status: dict) -> None:
    prep = _prep_dir(tree)
    prep.mkdir(parents=True, exist_ok=True)
    _status_path(tree).write_text(json.dumps(status, indent=2))


def _detach_store_links(tree: RepoTree) -> None:
    """Unlink any of ``tree``'s paths that symlink into the shared store.

    A reused / auto-enabled checkout points its ``.pr-review-prep`` sidecar and
    each root's ``node_modules`` at the shared store via symlinks. Before a real
    install runs against this checkout, those links must go so the install writes
    to local paths -- otherwise ``npm install`` (and the status writes) would
    mutate the shared store that other checkouts of the same dependency set are
    symlinked to. Real directories (a prior from-scratch install) are left alone
    so the package manager can update them incrementally.
    """
    prep = _prep_dir(tree)
    if prep.is_symlink():
        prep.unlink(missing_ok=True)
    for dirpath, dirnames, _files in os.walk(tree.root):
        if PREP_DIRNAME in dirnames:
            dirnames.remove(PREP_DIRNAME)  # its node_modules belongs to the prep
        if "node_modules" in dirnames:
            nm = Path(dirpath) / "node_modules"
            if nm.is_symlink():
                nm.unlink(missing_ok=True)
            dirnames.remove("node_modules")  # don't descend into a store-backed link


def start_prepare(
    tree: RepoTree,
    launcher: Launcher | None = None,
    force: bool = False,
    model: str | None = None,
) -> dict:
    """Kick off preparation for ``tree`` (idempotent).

    Returns the current status without relaunching when a run is already in
    flight (``installing``) or complete (``ready``), unless ``force`` is set.
    ``model`` selects the agent model (validated against the allow-list).
    """
    chosen = normalize_model(model)
    launcher = launcher or (lambda t: _default_launcher(t, chosen))
    current = prepare_status(tree)
    if not force and current.get("state") in ("installing", "ready"):
        return current
    # Reuse an installed prep from a prior checkout with the same dependencies
    # (a different PR, or an earlier push) instead of re-running the agent.
    if not force:
        entry = reusable_entry(tree)
        if entry is not None and _materialize(tree, entry):
            return prepare_status(tree)
    # A real install follows (force, or no reusable match). If this checkout was
    # previously reused/auto-enabled, its sidecar and node_modules are symlinks
    # into the shared store; detach them first so the install writes to local
    # paths and never mutates the store other checkouts are symlinked to.
    _detach_store_links(tree)
    status = {"state": "installing", "model": chosen, "error": None}
    _write_status(tree, status)
    launcher(tree)
    return status


def clear_prepared(tree: RepoTree) -> dict:
    """Remove this checkout's prepared state to reclaim disk.

    Handles both a freshly-installed tree (real ``node_modules`` / sidecar) and a
    reused one (symlinks into the shared store): real dirs are deleted, symlinks
    are unlinked. The shared store itself is left intact so other checkouts of the
    same dependency set keep reusing it -- this only clears the local checkout.
    """
    root = tree.root
    for dirpath, dirnames, _files in os.walk(root):
        # Never descend into the sidecar (its node_modules belongs to the prep).
        if PREP_DIRNAME in dirnames:
            dirnames.remove(PREP_DIRNAME)
        if "node_modules" in dirnames:
            nm = Path(dirpath) / "node_modules"
            if nm.is_symlink():
                nm.unlink(missing_ok=True)
            elif nm.is_dir():
                shutil.rmtree(nm, ignore_errors=True)
            dirnames.remove("node_modules")  # don't descend into what we just removed
    prep = _prep_dir(tree)
    if prep.is_symlink():
        prep.unlink(missing_ok=True)
    else:
        shutil.rmtree(prep, ignore_errors=True)
    return {"state": "absent"}


def _default_launcher(tree: RepoTree, model: str = DEFAULT_MODEL) -> None:
    threading.Thread(target=_run_prepare, args=(tree, model), daemon=True).start()


def _run_prepare(tree: RepoTree, model: str = DEFAULT_MODEL) -> None:
    # Fingerprint the pristine tree before the agent installs (installers may
    # rewrite lockfiles), so a later fresh checkout with the same deps matches
    # what we publish below.
    fingerprint = dep_fingerprint(tree.root)
    seed_hint = _seed_for_install(tree, fingerprint, model)
    try:
        run = _run_agent(tree, model, seed_hint=seed_hint)
        findings = _read_agent_findings(tree)
        ok, detail = _verify(tree, findings)
        roots = findings.get("roots") or []
        status = {
            "state": "ready" if ok else "failed",
            "model": model,
            "package_manager": findings.get("package_manager"),
            "roots": roots,
            "typescript_dir": findings.get("typescript_dir"),
            "python_venv": findings.get("python_venv"),
            "notes": findings.get("notes"),
            "cost_usd": run.cost_usd,
            "error": None if ok else detail,
        }
    except (
        PrepareError,
        AgentError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        # Any expected failure in this background thread becomes a failed status
        # the UI can show, rather than a silently dead thread.
        status = {"state": "failed", "model": model, "error": str(exc)[:1000]}
    _write_status(tree, status)
    if status["state"] == "ready" and fingerprint is not None:
        # Share the install so sibling checkouts (other PRs, later pushes) reuse it.
        _publish(tree, fingerprint, [r for r in status["roots"] if isinstance(r, str)])


def _seed_for_install(
    tree: RepoTree, fingerprint: str | None, model: str
) -> str | None:
    """Seed a from-scratch install with the repo's nearest previous prep, if any.

    Copies the most recent ready prep for this repo (with a *different*
    fingerprint -- an exact match would have been reused, not reinstalled) into the
    checkout so the agent updates incrementally, and returns a prompt hint carrying
    what that prior run learned. Returns None when there is nothing to seed from.
    """
    priors = [e for e in _ready_entries(tree.repo) if e.name != fingerprint]
    if not priors:
        return None
    findings = _seed_from_prior(tree, priors[0])
    # Re-assert the installing status defensively (the seed merge preserves it, but
    # a partial/failed seed could have disturbed it); keeps the UI showing progress.
    _write_status(tree, {"state": "installing", "model": model, "error": None})
    return _seed_hint(findings)


def _run_agent(
    tree: RepoTree, model: str = DEFAULT_MODEL, seed_hint: str | None = None
) -> AgentRun:
    """Run the headless prepare agent in the tree, streaming its activity to the
    log line-by-line so the UI can show live progress while it installs."""
    return run_streaming_agent(
        _build_prompt(seed_hint),
        cwd=tree.root,
        log_path=_log_path(tree),
        model=model,
        append_system_prompt=_AGENT_APPEND_SYSTEM,
        header=f"● Preparing rich types for {tree.repo} — this can take a few minutes.",
        timeout_s=_AGENT_TIMEOUT_S,
    )


def _read_agent_findings(tree: RepoTree) -> dict:
    path = _agent_result_path(tree)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _verify(tree: RepoTree, findings: dict) -> tuple[bool, str]:
    """Independently confirm the tooling the agent reported actually works.

    A run is ready when at least one language was prepared and every language it
    claims to have prepared verifies. Whichever languages are present (JS, Python,
    or both) are checked; a claimed-but-broken one fails the whole run.
    """
    ts_dir = findings.get("typescript_dir")
    py_venv = findings.get("python_venv")
    results: list[tuple[bool, str]] = []
    if isinstance(ts_dir, str) and ts_dir:
        results.append(_verify_typescript(tree, ts_dir))
    if isinstance(py_venv, str) and py_venv:
        results.append(_verify_python(tree, py_venv))
    if not results:
        return False, "prepare agent reported neither a typescript_dir nor a python_venv"
    for ok, detail in results:
        if not ok:
            return False, detail
    return True, ""


def _verify_typescript(tree: RepoTree, ts_dir: str) -> tuple[bool, str]:
    """Confirm typescript resolves where the agent said it does."""
    root = tree.root.resolve()
    abs_dir = (tree.root / ts_dir).resolve()
    if not abs_dir.is_relative_to(root) or not abs_dir.is_dir():
        return False, f"typescript_dir {ts_dir!r} is not a directory inside the tree"
    probe = subprocess.run(
        ["node", "-e", "require.resolve('typescript')"],
        cwd=str(abs_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return False, f"typescript is not resolvable in {ts_dir!r}"
    return True, ""


def _verify_python(tree: RepoTree, venv_rel: str) -> tuple[bool, str]:
    """Confirm the prepared Python venv exists inside the tree and is runnable."""
    root = tree.root.resolve()
    venv = (tree.root / venv_rel).resolve()
    if not venv.is_relative_to(root) or not venv.is_dir():
        return False, f"python_venv {venv_rel!r} is not a directory inside the tree"
    python = venv / "bin" / "python"
    if not python.exists():
        return False, f"python_venv {venv_rel!r} has no bin/python"
    probe = subprocess.run(
        [str(python), "-c", "import sys"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return False, f"python venv at {venv_rel!r} is not runnable"
    return True, ""
