"""Unit tests for pr_review.prepare (opt-in rich-types state machine).

These never launch a real agent or run a real install: the launcher is injected,
and state is asserted through the on-disk sidecar written under ``tmp_path``.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pr_review import prepare
from pr_review.github import RepoTree
from pr_review.testing import seed_prepared_state


def _build_real_venv(root: Path, rel: str = f"{prepare.PREP_DIRNAME}/venv") -> str:
    """Create a real, minimal (pip-less, offline) venv under ``root`` and return
    its repo-relative path -- enough for ``_verify_python`` to run its interpreter."""
    venv = root / rel
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
    )
    return rel


def _fake_typescript(root: Path, rel: str = prepare.PREP_DIRNAME) -> str:
    """Drop a stand-in typescript package under ``root/rel`` so that
    ``node -e "require.resolve('typescript')"`` succeeds there. Returns ``rel``."""
    pkg = root / rel / "node_modules" / "typescript"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "package.json").write_text(
        '{"name":"typescript","version":"5.4.0","main":"lib/typescript.js"}'
    )
    (pkg / "lib" / "typescript.js").write_text(
        "module.exports = {createLanguageService: function () {}};\n"
    )
    return rel


def _tree(tmp_path: Path) -> RepoTree:
    root = tmp_path / "repo-abc1234"
    root.mkdir()
    return RepoTree(repo="octocat/hello", sha="abc1234", root=root)


def _tree_at(tmp_path: Path, sha: str, deps: dict[str, str]) -> RepoTree:
    """A checkout dir under ``tmp_path`` seeded with the given dependency files."""
    root = tmp_path / f"repo-{sha}"
    root.mkdir()
    for rel, content in deps.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return RepoTree(repo="octocat/hello", sha=sha, root=root)


def _seed_prepared(
    tree: RepoTree, roots: list[str], notes: str = "used pnpm; engine-strict fallback"
) -> None:
    """Fake a completed install on ``tree``: ready sidecar + a typescript@5 + node_modules."""
    seed_prepared_state(tree, roots, notes=notes)


def test_status_absent_by_default(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    assert prepare.prepare_status(tree) == {"state": "absent"}
    assert prepare.is_ready(tree) is False
    assert prepare.ready_roots(tree) == []


def test_start_prepare_sets_installing_and_invokes_launcher(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    launched: list[RepoTree] = []
    status = prepare.start_prepare(tree, launcher=launched.append)
    assert status["state"] == "installing"
    assert launched == [tree]
    # The sidecar lives at the tree root (where the agent's cwd is).
    assert (tree.root / prepare.PREP_DIRNAME / "status.json").exists()
    assert prepare.prepare_status(tree)["state"] == "installing"


def test_normalize_model_validates() -> None:
    assert prepare.normalize_model("claude-opus-4-8") == "claude-opus-4-8"
    assert prepare.normalize_model(None) == prepare.DEFAULT_MODEL
    assert prepare.normalize_model("gpt-4") == prepare.DEFAULT_MODEL


def test_start_prepare_records_chosen_model(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare.start_prepare(tree, launcher=lambda _t: None, model="claude-opus-4-8")
    assert prepare.prepare_status(tree)["model"] == "claude-opus-4-8"


def test_start_prepare_defaults_invalid_model(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare.start_prepare(tree, launcher=lambda _t: None, model="nonsense")
    assert prepare.prepare_status(tree)["model"] == prepare.DEFAULT_MODEL


def test_start_prepare_is_idempotent_while_installing(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    calls: list[RepoTree] = []
    prepare.start_prepare(tree, launcher=calls.append)
    # A second call while installing does not relaunch.
    prepare.start_prepare(tree, launcher=calls.append)
    assert len(calls) == 1


def test_start_prepare_does_not_relaunch_when_ready(tmp_path: Path) -> None:
    tree = _tree(tmp_path)

    def ready_launcher(t: RepoTree) -> None:
        prepare._write_status(
            t, {"state": "ready", "roots": ["."], "typescript_dir": "."}
        )

    prepare.start_prepare(tree, launcher=ready_launcher)
    assert prepare.is_ready(tree) is True
    assert prepare.ready_roots(tree) == ["."]

    calls: list[RepoTree] = []
    prepare.start_prepare(tree, launcher=calls.append)
    assert calls == []  # already ready -> no relaunch


def test_force_relaunches_even_when_ready(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare._write_status(tree, {"state": "ready", "roots": ["."]})
    calls: list[RepoTree] = []
    prepare.start_prepare(tree, launcher=calls.append, force=True)
    assert calls == [tree]
    assert prepare.prepare_status(tree)["state"] == "installing"


def test_clear_prepared_removes_state_and_node_modules(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare._write_status(tree, {"state": "ready"})
    node_modules = tree.root / "pkg" / "node_modules" / "left-pad"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("module.exports = 1;\n")

    result = prepare.clear_prepared(tree)
    assert result == {"state": "absent"}
    assert not (tree.root / "pkg" / "node_modules").exists()
    assert not (tree.root / prepare.PREP_DIRNAME).exists()
    assert prepare.prepare_status(tree) == {"state": "absent"}


def test_dep_fingerprint_reflects_deps_not_source(tmp_path: Path) -> None:
    tree = _tree_at(
        tmp_path,
        "sha1",
        {"package.json": '{"deps":1}', "package-lock.json": '{"lock":1}'},
    )
    fp = prepare.dep_fingerprint(tree.root)
    assert fp is not None
    # Non-dependency source changes do not affect the fingerprint.
    (tree.root / "app.js").write_text("console.log(1)\n")
    (tree.root / "src").mkdir()
    (tree.root / "src" / "index.ts").write_text("export const x = 1\n")
    assert prepare.dep_fingerprint(tree.root) == fp
    # Installed artifacts are ignored, even when they contain package.json files.
    nm = tree.root / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name":"left-pad"}')
    assert prepare.dep_fingerprint(tree.root) == fp
    # A real dependency change flips it.
    (tree.root / "package.json").write_text('{"deps":2}')
    assert prepare.dep_fingerprint(tree.root) != fp


def test_dep_fingerprint_none_without_dep_files(tmp_path: Path) -> None:
    tree = _tree_at(tmp_path, "sha1", {"README.md": "# hi\n"})
    assert prepare.dep_fingerprint(tree.root) is None


def test_dep_fingerprint_matches_across_checkouts(tmp_path: Path) -> None:
    deps = {"package.json": '{"deps":1}', "pnpm-lock.yaml": "lockfile: 6\n"}
    a = _tree_at(tmp_path, "sha_a", deps)
    b = _tree_at(tmp_path, "sha_b", {**deps, "unrelated.py": "x = 1\n"})
    assert prepare.dep_fingerprint(a.root) == prepare.dep_fingerprint(b.root)


def test_dep_fingerprint_spans_python_manifests(tmp_path: Path) -> None:
    # A Python-only repo (no package.json) still fingerprints, and the varying-name
    # requirements*.txt glob is picked up alongside the fixed-name manifests.
    tree = _tree_at(
        tmp_path,
        "sha1",
        {
            "pyproject.toml": "[project]\nname = 'x'\n",
            "uv.lock": "version = 1\n",
            "requirements-dev.txt": "pytest\n",
        },
    )
    fp = prepare.dep_fingerprint(tree.root)
    assert fp is not None
    # A change to a Python manifest flips the fingerprint...
    (tree.root / "pyproject.toml").write_text("[project]\nname = 'y'\n")
    assert prepare.dep_fingerprint(tree.root) != fp
    # ...while a change to the requirements glob file does too.
    fp2 = prepare.dep_fingerprint(tree.root)
    (tree.root / "requirements-dev.txt").write_text("pytest\nruff\n")
    assert prepare.dep_fingerprint(tree.root) != fp2


def test_dep_fingerprint_ignores_manifests_inside_dot_venv(tmp_path: Path) -> None:
    # A repo's own .venv holds installed packages with their own pyproject.toml /
    # setup.py; those must not perturb the dependency fingerprint.
    tree = _tree_at(tmp_path, "sha1", {"pyproject.toml": "[project]\nname = 'x'\n"})
    fp = prepare.dep_fingerprint(tree.root)
    installed = tree.root / ".venv" / "lib" / "site-packages" / "dep"
    installed.mkdir(parents=True)
    (installed / "setup.py").write_text("from setuptools import setup; setup()\n")
    (installed / "pyproject.toml").write_text("[project]\nname = 'dep'\n")
    assert prepare.dep_fingerprint(tree.root) == fp


def test_start_prepare_reuses_published_prep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # store paths are relative to cwd
    deps = {"package.json": '{"deps":1}', "package-lock.json": '{"lock":1}'}
    producer = _tree_at(tmp_path, "sha_producer", deps)
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    # A different checkout with identical deps reuses without launching the agent.
    consumer = _tree_at(tmp_path, "sha_consumer", deps)
    launched: list[RepoTree] = []
    status = prepare.start_prepare(consumer, launcher=launched.append)

    assert launched == []  # no agent run
    assert status["state"] == "ready"
    assert prepare.is_ready(consumer) is True
    # Artifacts are symlinks into the shared store, not fresh installs.
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()
    linked_nm = consumer.root / "node_modules"
    assert linked_nm.is_symlink()
    assert (linked_nm / "left-pad" / "index.js").exists()


def test_start_prepare_no_reuse_for_different_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    producer = _tree_at(tmp_path, "sha_p", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    consumer = _tree_at(tmp_path, "sha_c", {"package.json": '{"deps":2}'})
    launched: list[RepoTree] = []
    status = prepare.start_prepare(consumer, launcher=launched.append)
    assert launched == [consumer]  # different deps -> real install
    assert status["state"] == "installing"


def test_clear_prepared_unlinks_reused_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_p", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    consumer = _tree_at(tmp_path, "sha_c", deps)
    prepare.start_prepare(consumer, launcher=lambda _t: None)
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()

    result = prepare.clear_prepared(consumer)
    assert result == {"state": "absent"}
    assert not (consumer.root / prepare.PREP_DIRNAME).exists()
    assert not (consumer.root / "node_modules").exists()
    # The shared store survives so other checkouts keep reusing it.
    assert prepare._entry_is_ready(prepare._store_entry(consumer.repo, fp))


def test_force_reprepare_detaches_store_links_and_leaves_store_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_p", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    # A consumer reuses the published prep: its sidecar + node_modules are now
    # symlinks pointing into the shared store.
    consumer = _tree_at(tmp_path, "sha_c", deps)
    prepare.start_prepare(consumer, launcher=lambda _t: None)
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()
    assert (consumer.root / "node_modules").is_symlink()

    # A forced re-prepare must NOT write the "installing" status (nor later run the
    # installer) through those symlinks into the shared store. The store entry it
    # was symlinked to stays ready, and the checkout's own paths are detached.
    launched: list[RepoTree] = []
    status = prepare.start_prepare(consumer, launcher=launched.append, force=True)
    assert status["state"] == "installing"
    assert launched == [consumer]
    assert not (consumer.root / prepare.PREP_DIRNAME).is_symlink()  # local real dir now
    assert not (consumer.root / "node_modules").is_symlink()
    # The shared store entry is untouched -- other checkouts keep reusing it.
    assert prepare._entry_is_ready(prepare._store_entry(consumer.repo, fp))


def test_publish_and_reuse_carry_the_python_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"pyproject.toml": "[project]\nname = 'x'\n"}
    producer = _tree_at(tmp_path, "sha_old", deps)
    seed_prepared_state(producer, ["."], python_venv=True)
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    # A different checkout with identical deps reuses the venv via the symlink.
    consumer = _tree_at(tmp_path, "sha_new", deps)
    assert prepare.start_prepare(consumer, launcher=lambda _t: None)["state"] == "ready"
    env = prepare.prepared_python_env(consumer)
    assert env is not None
    # It resolves through .pr-review-prep into the shared store, not the checkout.
    assert prepare.PREP_STORE.resolve().as_posix() in env
    assert Path(env).exists()


def test_prepared_python_env_none_without_venv(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prepare._write_status(tree, {"state": "ready", "typescript_dir": prepare.PREP_DIRNAME})
    assert prepare.prepared_python_env(tree) is None


def test_evict_store_keeps_most_recently_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    repo = "octocat/hello"
    base = prepare._repo_store_dir(repo)
    base.mkdir(parents=True)
    for i in range(5):
        entry = base / f"fp{i}"
        (entry / "prep").mkdir(parents=True)
        (entry / "manifest.json").write_text("{}")
        os.utime(entry, (1000 + i, 1000 + i))  # fp4 newest, fp0 oldest
    # A concurrent publish's staging dir must not be counted or evicted.
    (base / ".staging-x-1").mkdir()

    prepare._evict_store(repo, keep=3)

    remaining = sorted(
        e.name for e in base.iterdir() if e.is_dir() and not e.name.startswith(".")
    )
    assert remaining == ["fp2", "fp3", "fp4"]  # the two least-recently-used were evicted
    assert (base / ".staging-x-1").is_dir()  # staging left alone


def test_reuse_bumps_entry_mtime_so_it_survives_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_old", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])
    entry = prepare._store_entry(producer.repo, fp)
    os.utime(entry, (1000, 1000))  # make it look stale

    # Reusing it (materialize) must refresh its mtime.
    consumer = _tree_at(tmp_path, "sha_new", deps)
    assert prepare._materialize(consumer, entry) is True
    assert entry.stat().st_mtime > 1000


def test_auto_enable_materializes_exact_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_old", deps)
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    consumer = _tree_at(tmp_path, "sha_new", deps)  # same deps, never enabled
    status = prepare.auto_enable(consumer)
    assert status["state"] == "ready"
    assert prepare.is_ready(consumer) is True
    assert (consumer.root / prepare.PREP_DIRNAME).is_symlink()


def test_auto_enable_noop_without_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    producer = _tree_at(tmp_path, "sha_old", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["."])
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])

    # Different deps: an install would be needed, so auto-enable does nothing.
    consumer = _tree_at(tmp_path, "sha_new", {"package.json": '{"deps":2}'})
    status = prepare.auto_enable(consumer)
    assert status == {"state": "absent"}
    assert not (consumer.root / prepare.PREP_DIRNAME).exists()


def test_auto_enable_leaves_installing_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tree = _tree_at(tmp_path, "sha", {"package.json": '{"deps":1}'})
    prepare._write_status(tree, {"state": "installing"})
    assert prepare.auto_enable(tree)["state"] == "installing"


def test_reusable_entry_short_circuits_when_repo_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    tree = _tree_at(tmp_path, "sha", {"package.json": '{"deps":1}'})
    # No prep has ever been published for this repo, so its store dir is absent
    # and the lookup returns without fingerprinting the tree.
    assert not prepare._repo_store_dir(tree.repo).exists()
    assert prepare.reusable_entry(tree) is None
    assert prepare.auto_enable(tree) == {"state": "absent"}


def test_link_creates_replaces_dir_and_replaces_symlink(tmp_path: Path) -> None:
    source = tmp_path / "store" / "node_modules"
    source.mkdir(parents=True)
    (source / "marker.txt").write_text("from store\n")
    target = tmp_path / "checkout" / "node_modules"

    # Fresh create: the target does not exist yet.
    prepare._link(target, source)
    assert target.is_symlink()
    assert (target / "marker.txt").read_text() == "from store\n"

    # Replace a real directory sitting where the link should go (a prior install).
    target.unlink()
    target.mkdir()
    (target / "stale.txt").write_text("old install\n")
    prepare._link(target, source)
    assert target.is_symlink()
    assert not (target / "stale.txt").exists()
    assert (target / "marker.txt").read_text() == "from store\n"

    # Replace an existing symlink that points somewhere else (a stale reuse).
    other = tmp_path / "other" / "node_modules"
    other.mkdir(parents=True)
    target.unlink()
    target.symlink_to(other.resolve())
    prepare._link(target, source)
    assert target.is_symlink()
    assert target.resolve() == source.resolve()


def test_seed_for_install_copies_prior_and_returns_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # A prior prep for the repo, under some other dependency fingerprint.
    producer = _tree_at(tmp_path, "sha_old", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["apps/minds"], notes="engine-strict fallback to npm")
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["apps/minds"])

    # A new checkout with *different* deps: no exact reuse, so we seed the install.
    consumer = _tree_at(
        tmp_path, "sha_new", {"package.json": '{"deps":2}', "apps/minds/x": ""}
    )
    fp = prepare.dep_fingerprint(consumer.root)
    hint = prepare._seed_for_install(consumer, fp, model="claude-haiku-4-5")

    assert hint is not None
    assert (
        "pnpm" in hint
        and "apps/minds" in hint
        and "engine-strict fallback to npm" in hint
    )
    # Seeded artifacts are real writable copies (the agent mutates them), not symlinks.
    prep = consumer.root / prepare.PREP_DIRNAME
    assert prep.is_dir() and not prep.is_symlink()
    assert (prep / "node_modules" / "typescript" / "package.json").exists()
    nm = consumer.root / "apps/minds" / "node_modules"
    assert nm.is_dir() and not nm.is_symlink()
    # The stale prior status/result are dropped; status is back to installing.
    assert not (prep / "agent_result.json").exists()
    assert prepare.prepare_status(consumer)["state"] == "installing"


def test_seed_from_prior_preserves_installing_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seeding must NOT let the prior entry's ready status.json appear in the
    # checkout mid-copy: a status poll landing then would flip the UI to "rich"
    # and stop streaming the live install log.
    monkeypatch.chdir(tmp_path)
    producer = _tree_at(tmp_path, "sha_old", {"package.json": '{"deps":1}'})
    _seed_prepared(producer, ["."])  # writes a ready status into the entry
    prepare._publish(producer, prepare.dep_fingerprint(producer.root), ["."])
    entry = prepare._store_entry(producer.repo, prepare.dep_fingerprint(producer.root))

    consumer = _tree_at(tmp_path, "sha_new", {"package.json": '{"deps":2}'})
    prepare._write_status(consumer, {"state": "installing", "model": "m", "error": None})
    prepare._seed_from_prior(consumer, entry)

    # The installing status survives, and the prior artifacts were merged in.
    assert prepare.prepare_status(consumer)["state"] == "installing"
    assert (consumer.root / prepare.PREP_DIRNAME / "node_modules" / "typescript").exists()


def test_seed_for_install_returns_none_without_prior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    consumer = _tree_at(tmp_path, "sha_new", {"package.json": '{"deps":1}'})
    fp = prepare.dep_fingerprint(consumer.root)
    assert prepare._seed_for_install(consumer, fp, model="claude-haiku-4-5") is None


def test_seed_for_install_skips_exact_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"package.json": '{"deps":1}'}
    producer = _tree_at(tmp_path, "sha_old", deps)
    _seed_prepared(producer, ["."])
    fp = prepare.dep_fingerprint(producer.root)
    prepare._publish(producer, fp, ["."])

    # A checkout with the SAME fingerprint would be reused, not seeded -- so the only
    # available prior (the exact match) is skipped and there is nothing to seed.
    consumer = _tree_at(tmp_path, "sha_new", deps)
    assert prepare._seed_for_install(consumer, fp, model="claude-haiku-4-5") is None


def test_build_prompt_prepends_hint_only_when_present() -> None:
    assert prepare._build_prompt(None) == prepare._AGENT_PROMPT
    withhint = prepare._build_prompt("do X first")
    assert withhint.startswith("PRIOR PREPARATION CONTEXT")
    assert "do X first" in withhint and prepare._AGENT_PROMPT in withhint


def test_verify_requires_at_least_one_language(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    ok, detail = prepare._verify(tree, {"roots": [], "notes": "nothing installed"})
    assert ok is False
    assert "neither" in detail


def test_verify_python_only_passes_with_real_venv(tmp_path: Path) -> None:
    # A Python-only prep is ready once the venv verifies -- no typescript required.
    tree = _tree(tmp_path)
    venv_rel = _build_real_venv(tree.root)
    ok, detail = prepare._verify(tree, {"typescript_dir": None, "python_venv": venv_rel})
    assert ok is True
    assert detail == ""


def test_verify_fails_when_a_claimed_language_is_broken(tmp_path: Path) -> None:
    # The agent claims a Python venv, but it was never built -- the whole run fails
    # rather than reporting a half-working "ready".
    tree = _tree(tmp_path)
    ok, detail = prepare._verify(
        tree, {"python_venv": f"{prepare.PREP_DIRNAME}/venv"}
    )
    assert ok is False
    assert "python_venv" in detail


def test_verify_python_rejects_venv_outside_tree(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    ok, detail = prepare._verify_python(tree, "../escape/venv")
    assert ok is False
    assert "not a directory inside the tree" in detail


def test_verify_typescript_passes_when_typescript_resolves(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    ts_rel = _fake_typescript(tree.root)
    ok, detail = prepare._verify_typescript(tree, ts_rel)
    assert ok is True
    assert detail == ""


def test_verify_typescript_rejects_dir_outside_tree(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    ok, detail = prepare._verify_typescript(tree, "../elsewhere")
    assert ok is False
    assert "not a directory inside the tree" in detail


def test_verify_both_languages_all_must_pass(tmp_path: Path) -> None:
    # TypeScript resolves but the claimed Python venv is missing: a run that claims
    # both languages fails unless every claimed one verifies.
    tree = _tree(tmp_path)
    ts_rel = _fake_typescript(tree.root)
    ok, detail = prepare._verify(
        tree, {"typescript_dir": ts_rel, "python_venv": f"{prepare.PREP_DIRNAME}/venv"}
    )
    assert ok is False
    assert "python_venv" in detail


def test_log_tail_reads_recent_lines(tmp_path: Path) -> None:
    tree = _tree(tmp_path)
    prep = tree.root / prepare.PREP_DIRNAME
    prep.mkdir(parents=True)
    (prep / "prepare.log").write_text("\n".join(f"line {i}" for i in range(100)))
    tail = prepare.log_tail(tree, lines=5)
    assert tail.splitlines() == ["line 95", "line 96", "line 97", "line 98", "line 99"]
