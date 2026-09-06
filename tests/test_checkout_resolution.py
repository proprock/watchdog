"""The filesystem checkout fast path must match `git rev-parse` byte-for-byte.

WD-022a Part 2.2: `discover()` resolves the toplevel and git-common-dir from
filesystem reads and only spawns git for layouts it does not recognize. These
tests pin the fast path to real git output across every layout in the plan.
"""

import os
import subprocess
from pathlib import Path

import pytest

from agent_watchdog.registry import Checkout, _fast_checkout, discover


def run_git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def git_checkout(path: Path) -> tuple[Path, Path]:
    toplevel, common = run_git(
        path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"
    ).splitlines()
    return Path(toplevel).resolve(strict=True), Path(common).resolve(strict=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    root = tmp_path / "repo with spaces"
    root.mkdir()
    run_git(root, "init")
    run_git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    return root


def test_repo_root_matches_git(repo):
    toplevel, common = git_checkout(repo)
    assert discover(repo) == Checkout(toplevel, common)
    assert discover(repo).git_common_dir == (repo / ".git").resolve(strict=True)


def test_repo_subdirectory_matches_git(repo):
    child = repo / "package" / "nested"
    child.mkdir(parents=True)
    checkout = discover(child)
    toplevel, common = git_checkout(child)
    assert (checkout.root, checkout.git_common_dir) == (toplevel, common)
    assert checkout.root == repo.resolve(strict=True)


def test_linked_worktree_root_matches_git(repo, tmp_path):
    worktree = tmp_path / "worktree with spaces"
    run_git(repo, "worktree", "add", "--detach", str(worktree))
    checkout = discover(worktree)
    toplevel, common = git_checkout(worktree)
    assert (checkout.root, checkout.git_common_dir) == (toplevel, common)
    assert checkout.root == worktree.resolve(strict=True)
    assert checkout.git_common_dir == (repo / ".git").resolve(strict=True)


def test_linked_worktree_subdirectory_matches_git(repo, tmp_path):
    worktree = tmp_path / "worktree with spaces"
    run_git(repo, "worktree", "add", "--detach", str(worktree))
    child = worktree / "src" / "deep"
    child.mkdir(parents=True)
    checkout = discover(child)
    toplevel, common = git_checkout(child)
    assert (checkout.root, checkout.git_common_dir) == (toplevel, common)


def test_non_git_directory_returns_no_common_dir(tmp_path):
    plain = tmp_path / "plain dir"
    plain.mkdir()
    checkout = discover(plain)
    assert checkout.root == plain.resolve(strict=True)
    assert checkout.git_common_dir is None


def test_fast_path_does_not_spawn_git(repo, tmp_path, monkeypatch):
    worktree = tmp_path / "worktree with spaces"
    run_git(repo, "worktree", "add", "--detach", str(worktree))
    calls: list = []
    real_run = subprocess.run

    def spy(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr("agent_watchdog.registry.subprocess.run", spy)
    for target in (repo, repo / "sub", worktree):
        (repo / "sub").mkdir(exist_ok=True)
        discover(target)
    assert calls == []


def test_unrecognized_git_file_falls_back_to_git(tmp_path):
    (tmp_path / ".git").write_text("gitdir: nowhere-real\n", encoding="utf-8")
    assert _fast_checkout(tmp_path / ".git") is None
