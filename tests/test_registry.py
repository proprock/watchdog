import os
import subprocess

import pytest

from agent_watchdog.config import Config, load_config, save_config
from agent_watchdog.registry import Registry, RegistryError


def git(path, *args):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    root = tmp_path / "repo with spaces"
    root.mkdir()
    git(root, "init")
    git(
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


def test_worktrees_share_project_but_clones_and_checkouts_are_isolated(repository, tmp_path):
    worktree = tmp_path / "worktree"
    clone = tmp_path / "clone"
    git(repository, "worktree", "add", "-b", "test-worktree", str(worktree))
    git(tmp_path, "clone", "--no-hardlinks", str(repository), str(clone))
    registry = Registry()
    project = registry.add(repository)
    assert registry.add(worktree).id == project.id
    assert registry.add(clone).id != project.id
    main = registry.resolve(repository)
    linked = registry.resolve(worktree)
    assert main is not None and linked is not None
    assert main.project_id == linked.project_id
    assert main.checkout_id != linked.checkout_id
    assert registry.resolve(repository) == main


def test_non_git_resolution_remove_and_persistence(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    sibling = tmp_path / "plain-other"
    sibling.mkdir()
    registry = Registry()
    project = registry.add(root)
    resolution = registry.resolve(child)
    assert resolution is not None and resolution.project_id == project.id
    assert registry.resolve(sibling) is None
    with pytest.raises(RegistryError):
        registry.add(child)
    path = tmp_path / "config.toml"
    save_config(path, registry.config)
    restored = Registry(load_config(path))
    assert restored.resolve(child) == registry.resolve(child)
    restored.remove(project.id)
    assert restored.resolve(root) is None
    assert root.exists()


def test_relocation_is_explicit_and_preserves_project_uuid(tmp_path):
    root = tmp_path / "before"
    root.mkdir()
    target = tmp_path / "after"
    registry = Registry()
    project = registry.add(root)
    root.rename(target)
    assert registry.resolve(target) is None
    relocated = registry.relocate(project.id, target)
    assert relocated.id == project.id
    resolution = registry.resolve(target)
    assert resolution is not None and resolution.project_id == project.id
    assert relocated.root == target.resolve()


def test_relocation_rejects_existing_source_and_registered_destination(tmp_path):
    roots = [tmp_path / name for name in ("one", "two")]
    for root in roots:
        root.mkdir()
    registry = Registry()
    one, two = [registry.add(root) for root in roots]
    with pytest.raises(RegistryError):
        registry.relocate(one.id, roots[1])
    roots[0].rmdir()
    with pytest.raises(RegistryError):
        registry.relocate(one.id, roots[1])
    assert registry.config.projects == (one, two)


def test_symlink_alias_resolves_to_same_project(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks unavailable: {error}")
    registry = Registry()
    assert registry.add(alias).id == registry.add(root).id
    assert registry.resolve(alias) == registry.resolve(root)


def test_case_alias_uses_host_filesystem_semantics(tmp_path):
    root = tmp_path / "CaseProject"
    root.mkdir()
    alias = tmp_path / "caseproject"
    registry = Registry()
    project = registry.add(root)
    if alias.exists():
        assert registry.add(alias).id == project.id
        assert registry.resolve(alias) == registry.resolve(root)
    else:
        alias.mkdir()
        assert registry.add(alias).id != project.id


def test_unregistered_nested_git_repository_is_not_collected(repository):
    registry = Registry()
    registry.add(repository.parent)
    assert registry.resolve(repository) is None


def test_trusted_git_repository_is_auto_added_once(repository, tmp_path):
    trusted = tmp_path / "repos"
    trusted.mkdir()
    repository.rename(trusted / "project")
    repository = trusted / "project"
    nested = repository / "nested"
    nested.mkdir()
    registry = Registry(Config(auto_add_projects=True, trusted_projects_dir=trusted))

    resolution, added = registry.resolve_or_auto_add(nested)
    assert resolution is not None and added
    assert registry.config.projects[0].root == repository.resolve()

    repeated, added = registry.resolve_or_auto_add(repository)
    assert repeated == resolution and not added

    worktree = trusted / "project-worktree"
    git(repository, "worktree", "add", "-b", "auto-add-worktree", str(worktree))
    linked, added = registry.resolve_or_auto_add(worktree)
    assert linked is not None and not added
    assert linked.project_id == resolution.project_id
    assert linked.checkout_id != resolution.checkout_id


def test_auto_add_rejects_non_git_and_outside_trusted_directory(repository, tmp_path):
    trusted = tmp_path / "repos"
    trusted.mkdir()
    registry = Registry(Config(auto_add_projects=True, trusted_projects_dir=trusted))
    plain = trusted / "plain"
    plain.mkdir()

    assert registry.resolve_or_auto_add(plain) == (None, False)
    assert registry.resolve_or_auto_add(repository) == (None, False)
    assert registry.config.projects == ()


def test_git_environment_cannot_redirect_project_identity(repository, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init")
    registry = Registry()
    project = registry.add(repository)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    resolution = registry.resolve(repository)
    assert resolution is not None and resolution.project_id == project.id


def test_broken_git_metadata_is_not_silently_registered_as_non_git(tmp_path):
    (tmp_path / ".git").write_text("gitdir: missing\n")
    with pytest.raises(RegistryError):
        Registry().add(tmp_path)


def test_git_relocation_keeps_uuid_and_persists_new_common_dir(repository, tmp_path):
    registry = Registry()
    project = registry.add(repository)
    target = tmp_path / "moved"
    repository.rename(target)
    assert registry.resolve(target) is None
    moved = registry.relocate(project.id, target)
    assert moved.id == project.id
    assert moved.git_common_dir == (target / ".git").resolve()
    path = tmp_path / "config.toml"
    save_config(path, registry.config)
    resolution = Registry(load_config(path)).resolve(target)
    assert resolution is not None and resolution.project_id == project.id
