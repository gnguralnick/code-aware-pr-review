"""Unit tests for pr_review.pyintel (Jedi-backed hover and go-to-definition).

These run Jedi for real against small Python trees built in ``tmp_path`` -- no
network and no GitHub access are involved.
"""

import subprocess
import sys
from pathlib import Path

from pr_review import prepare, pyintel
from pr_review.testing import write_tree


def test_hover_returns_signature_and_docstring(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {"mod.py": 'def greet(name: str) -> str:\n    """Say hello to someone."""\n    return "hi " + name\n\n\ngreet("world")\n'},
    )
    # Hover over the ``greet`` call on the last line (1-based line 6, column 1).
    result = pyintel.hover(tree, "mod.py", line=6, column=1)
    assert result is not None
    contents = result["contents"]
    assert "greet" in contents
    assert "Say hello to someone." in contents


def test_hover_returns_none_on_empty_location(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "\n\n"})
    assert pyintel.hover(tree, "mod.py", line=1, column=1) is None


def test_hover_returns_none_for_path_escape(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "x = 1\n"})
    assert pyintel.hover(tree, "../outside.py", line=1, column=1) is None


def test_hover_returns_none_for_missing_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"mod.py": "x = 1\n"})
    assert pyintel.hover(tree, "nope.py", line=1, column=1) is None


def test_definition_resolves_in_repo_symbol_across_files(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "defs.py": "def helper() -> int:\n    return 1\n",
            "main.py": "from defs import helper\n\nhelper()\n",
        },
    )
    # Go to definition of the ``helper`` call in main.py (1-based line 3, col 1).
    result = pyintel.definition(tree, "main.py", line=3, column=1)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "defs.py"
    assert result["line"] == 1
    assert result["name"] == "helper"
    assert result["type"] == "function"


def test_definition_returns_none_for_missing_file(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, {"main.py": "x = 1\n"})
    assert pyintel.definition(tree, "ghost.py", line=1, column=1) is None


def test_source_roots_include_project_dirs_and_src(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "libs/acme/pyproject.toml": "[project]\nname = 'acme'\n",
            "libs/acme/src/acme/__init__.py": "",
            "node_modules/junk/pyproject.toml": "[project]\nname = 'junk'\n",
        },
    )
    roots = pyintel._source_roots(tree)
    assert str(tmp_path.resolve()) in roots
    assert str((tmp_path / "libs/acme").resolve()) in roots
    assert str((tmp_path / "libs/acme/src").resolve()) in roots
    # Installed-artifact dirs are skipped, not treated as source roots.
    assert not any("node_modules" in r for r in roots)


def test_definition_resolves_member_across_package_via_source_root(tmp_path: Path) -> None:
    # A src-layout package where importing `acme.models` requires `libs/acme/src`
    # on the path -- the case a bare Jedi project misses (and that let a stale
    # vendored copy shadow the tree for mngr PRs).
    tree = write_tree(
        tmp_path,
        {
            "libs/acme/pyproject.toml": "[project]\nname = 'acme'\n",
            "libs/acme/src/acme/__init__.py": "",
            "libs/acme/src/acme/models.py": "class Widget:\n    size: int = 0\n",
            "libs/acme/src/acme/use.py": (
                "from acme.models import Widget\n\n\ndef f(w: Widget) -> None:\n    w.size\n"
            ),
        },
    )
    # Go to definition of the `size` member on line 5 (`    w.size`), col 7.
    result = pyintel.definition(tree, "libs/acme/src/acme/use.py", line=5, column=7)
    assert result is not None
    assert result["in_repo"] is True
    assert result["path"] == "libs/acme/src/acme/models.py"
    assert result["name"] == "size"


def test_rich_tier_resolves_third_party_from_prepared_env(tmp_path: Path) -> None:
    # A third-party package the pr-review venv does NOT have: basic tier can't
    # resolve it, the rich tier (the prepared venv as Jedi's environment) can.
    tree = write_tree(tmp_path, {"app.py": "import acmelib\n\nacmelib.gadget\n"})
    # Basic tier: acmelib is nowhere on the path, so it does not resolve.
    assert pyintel.hover(tree, "app.py", line=3, column=11) is None

    # Build a real throwaway venv (offline, no pip) and drop a fake package into it.
    venv = tree.root / prepare.PREP_DIRNAME / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
    )
    site = next(venv.glob("lib/python*/site-packages"))
    (site / "acmelib.py").write_text("def gadget() -> int:\n    return 1\n")
    prepare._write_status(
        tree, {"state": "ready", "python_venv": f"{prepare.PREP_DIRNAME}/venv"}
    )
    pyintel._env_cache.clear()  # don't reuse another test's environment

    # Rich tier: acmelib.gadget now resolves through the prepared environment.
    result = pyintel.hover(tree, "app.py", line=3, column=11)
    assert result is not None
    assert "gadget" in result["contents"]


def test_rich_tier_degrades_to_basic_when_venv_invalid(tmp_path: Path) -> None:
    # The tree is marked "ready" with a python_venv, but the interpreter there is a
    # stub Jedi will reject. Rich resolution must degrade to the basic source-roots
    # tier rather than crash: the repo's own code still resolves.
    tree = write_tree(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname = 'acme'\n",
            "src/acme/__init__.py": "",
            "src/acme/models.py": "class Widget:\n    size: int = 0\n",
            "src/acme/use.py": (
                "from acme.models import Widget\n\n\ndef f(w: Widget) -> None:\n    w.size\n"
            ),
        },
    )
    venv_bin = tree.root / prepare.PREP_DIRNAME / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexit 1\n")  # not a real interpreter
    (venv_bin / "python").chmod(0o755)
    prepare._write_status(
        tree, {"state": "ready", "python_venv": f"{prepare.PREP_DIRNAME}/venv"}
    )
    pyintel._env_cache.clear()  # don't reuse another test's environment

    # Falls back to the basic tier: the repo's own member type still resolves.
    result = pyintel.hover(tree, "src/acme/use.py", line=5, column=7)
    assert result is not None
    assert "int" in result["contents"]


def test_hover_resolves_member_type_across_package(tmp_path: Path) -> None:
    tree = write_tree(
        tmp_path,
        {
            "libs/acme/pyproject.toml": "[project]\nname = 'acme'\n",
            "libs/acme/src/acme/__init__.py": "",
            "libs/acme/src/acme/models.py": "class Widget:\n    size: int = 0\n",
            "libs/acme/src/acme/use.py": (
                "from acme.models import Widget\n\n\ndef f(w: Widget) -> None:\n    w.size\n"
            ),
        },
    )
    result = pyintel.hover(tree, "libs/acme/src/acme/use.py", line=5, column=7)
    assert result is not None
    assert "int" in result["contents"]
