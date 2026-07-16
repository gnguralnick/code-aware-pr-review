"""Type-aware Python intelligence for the diff view, backed by Jedi.

Jedi is a static-analysis library (the engine behind most Python IDE
completion/hover) -- it runs in-process, so there is no language-server process
to manage. We point it at the cached PR source tree as its project root so
within-repo imports resolve, and expose two operations the editor needs: hover
(type + signature + docstring) and go-to-definition.

Import resolution is pinned to the cached tree. By default Jedi would also search
pr-review's own venv site-packages, and this host *vendors* some of the repos a
user reviews (e.g. mngr) -- so a bare Jedi would resolve ``imbue.mngr.*`` to the
stale installed copy, missing any symbol added in the PR (a member type, a new
class). We instead pin resolution to the repo's own source roots so the tree
always wins.

Two tiers, mirroring the JS engines:
- *basic* (always): source roots on ``sys_path``; the repo's own code resolves,
  third-party libraries do not.
- *rich* (when the repo has been prepared -- see ``prepare``): Jedi additionally
  runs against the prepared venv (``prepare.prepared_python_env``) as its
  ``environment``, so third-party types resolve too, while the source roots
  (added first) keep the PR's own code winning over any copy in the venv.
"""

import os
import threading
from pathlib import Path

import jedi

from pr_review import prepare
from pr_review.github import RepoTree

# A directory holding one of these is the root of an installable Python project;
# its own dir (flat layout) and its ``src/`` (src layout) go on the import path.
_PROJECT_MARKERS = frozenset({"pyproject.toml", "setup.py", "setup.cfg"})
_SKIP_DIRS = frozenset({"node_modules", ".pr-review-prep", ".git", ".venv"})

# Source roots are stable for a given cached tree (immutable per SHA) and walking
# the whole tree per hover would be wasteful, so memoize per tree root.
_roots_cache: dict[str, list[str]] = {}
_roots_lock = threading.Lock()

# Building a Jedi environment spawns the interpreter to read its sys.path; cache
# it per venv-python path so hovers don't pay that repeatedly.
_env_cache: dict[str, jedi.api.environment.Environment] = {}
_env_lock = threading.Lock()


def _prepared_environment(tree: RepoTree) -> "jedi.api.environment.Environment | None":
    """The Jedi environment for ``tree``'s prepared venv, or ``None`` if not rich.

    Cached per venv-python path. A venv that Jedi rejects (never fully built,
    interpreter missing) degrades to the basic source-roots-only tier.
    """
    env_python = prepare.prepared_python_env(tree)
    if env_python is None:
        return None
    with _env_lock:
        cached = _env_cache.get(env_python)
    if cached is not None:
        return cached
    try:
        env = jedi.create_environment(env_python, safe=False)
    except (jedi.InvalidPythonEnvironment, OSError):
        return None
    with _env_lock:
        _env_cache[env_python] = env
    return env


def _source_roots(tree: RepoTree) -> list[str]:
    """Directories to put on Jedi's path so the repo's own modules resolve from
    the cached tree. Every project dir (one holding a project marker) plus its
    ``src/`` layout dir, plus the tree root itself. Cached per tree."""
    # Key on the resolved absolute path, not tree.root as-given: tree.root is
    # relative to the app cwd, so two different checkouts (or two tests) can share
    # the same relative string and would otherwise collide on a stale cache entry.
    root = tree.root.resolve()
    key = str(root)
    with _roots_lock:
        cached = _roots_cache.get(key)
    if cached is not None:
        return cached
    roots = {str(root)}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if any(marker in filenames for marker in _PROJECT_MARKERS):
            roots.add(dirpath)
            src = Path(dirpath) / "src"
            if src.is_dir():
                roots.add(str(src))
    result = sorted(roots)
    with _roots_lock:
        _roots_cache[key] = result
    return result


def _script(tree: RepoTree, rel_path: str) -> jedi.Script | None:
    abs_path = (tree.root / rel_path).resolve()
    if not str(abs_path).startswith(str(tree.root.resolve())) or not abs_path.is_file():
        return None
    code = abs_path.read_text(errors="replace")
    roots = _source_roots(tree)
    env = _prepared_environment(tree)
    if env is not None:
        # Rich tier: third-party types come from the prepared venv; source roots go
        # first (added_sys_path) so the PR's own code still shadows any copy the venv
        # installed editable.
        project = jedi.Project(str(tree.root), added_sys_path=roots)
        return jedi.Script(code=code, path=str(abs_path), project=project, environment=env)
    # Basic tier: source roots only. The override (not added_sys_path) plus
    # smart_sys_path off keeps the cached tree shadowing pr-review's own venv.
    project = jedi.Project(str(tree.root), sys_path=roots, smart_sys_path=False)
    return jedi.Script(code=code, path=str(abs_path), project=project)


def hover(tree: RepoTree, rel_path: str, line: int, column: int) -> dict | None:
    """Markdown hover for the symbol at (line, column).

    ``line``/``column`` are 1-based (Monaco). Jedi columns are 0-based.
    """
    script = _script(tree, rel_path)
    if script is None:
        return None
    jcol = max(0, column - 1)
    try:
        names = script.help(line, jcol)
    except (ValueError, IndexError, RecursionError):
        return None
    if not names:
        return None
    name = names[0]
    parts: list[str] = []
    signatures = name.get_signatures()
    if signatures:
        parts.append("```python\n" + signatures[0].to_string() + "\n```")
    elif name.description:
        parts.append("```python\n" + name.description + "\n```")
    full = name.full_name
    if full and full != name.name:
        parts.append("`" + full + "`")
    doc = name.docstring(raw=True)
    if doc:
        parts.append(doc.strip())
    body = "\n\n".join(p for p in parts if p).strip()
    if not body:
        return None
    return {"contents": body}


def definition(tree: RepoTree, rel_path: str, line: int, column: int) -> dict | None:
    """Resolve the definition of the symbol at (line, column).

    Returns ``in_repo`` plus the path (relative to the tree root when in-repo,
    else the absolute path of a stdlib/stub file) so the editor can navigate.
    """
    script = _script(tree, rel_path)
    if script is None:
        return None
    jcol = max(0, column - 1)
    try:
        defs = script.goto(line, jcol, follow_imports=True, follow_builtin_imports=False)
    except (ValueError, IndexError, RecursionError):
        return None
    for found in defs:
        if not found.module_path:
            continue
        target = Path(found.module_path).resolve()
        root = tree.root.resolve()
        in_repo = str(target).startswith(str(root))
        return {
            "in_repo": in_repo,
            "path": str(target.relative_to(root)) if in_repo else str(target),
            "line": found.line or 1,
            "column": (found.column or 0) + 1,
            "name": found.name,
            "type": found.type,
        }
    return None
