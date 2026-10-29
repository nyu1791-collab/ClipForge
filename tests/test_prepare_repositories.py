"""Safe discovery and cloning of several GitHub repositories."""

import json
from types import SimpleNamespace

import pytest

from tools import prepare_repositories as prepare


def row(owner, name, private=False):
    return {
        "owner": {"login": owner},
        "name": name,
        "full_name": f"{owner}/{name}",
        "private": private,
        "archived": False,
    }


def test_discovery_paginates_and_only_selects_requested_owners():
    calls = []

    def fetch(page):
        calls.append(page)
        if page == 1:
            return [row("unrelated", f"repo{i}") for i in range(99)] + [
                row("Owner", "One")
            ]
        return [row("other", "Two", True), row("OWNER", "One")]

    found = prepare.discover_repositories(["owner", "other"], fetch_page=fetch)
    assert calls == [1, 2]
    assert {item["full_name"].casefold() for item in found} == {
        "owner/one",
        "other/two",
    }
    assert next(item for item in found if item["name"] == "Two")["private"] is True


@pytest.mark.parametrize("name", ["..", "../private", "bad\nname", "a/b", ""])
def test_discovery_rejects_unsafe_repository_names(name):
    with pytest.raises(RuntimeError, match="安全"):
        prepare.discover_repositories(
            ["owner"], fetch_page=lambda _: [row("owner", name)]
        )


def test_preview_does_not_create_files_or_run_clone(tmp_path, monkeypatch):
    target = tmp_path / "new"
    monkeypatch.setattr(
        prepare, "clone_repository", lambda *a, **kw: pytest.fail("unexpected clone")
    )
    assert (
        prepare.execute(
            [
                {
                    "owner": "owner",
                    "name": "project",
                    "full_name": "owner/project",
                    "private": False,
                    "archived": False,
                }
            ],
            target,
            ["owner"],
        )
        == 0
    )
    assert not target.exists()


def test_preview_does_not_emit_control_characters_from_branch_name(tmp_path, capsys):
    repository = row("owner", "project")
    repository["default_branch"] = "main\x1b[2J"
    repos = prepare.discover_repositories(["owner"], fetch_page=lambda _: [repository])
    assert prepare.execute(repos, tmp_path, ["owner"]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "既定 要確認" in output


def test_existing_and_broken_symlink_destinations_are_never_overwritten(
    tmp_path, monkeypatch
):
    owner = tmp_path / "owner"
    existing = owner / "existing"
    existing.mkdir(parents=True)
    marker = existing / "work.txt"
    marker.write_text("my unsaved work")
    (owner / "linked").symlink_to(tmp_path / "missing")
    repos = [
        {
            "owner": "owner",
            "name": name,
            "full_name": f"owner/{name}",
            "private": False,
            "archived": False,
        }
        for name in ("existing", "linked", "new")
    ]
    calls = []

    def clone(repo, destination, *, with_lfs):
        calls.append(repo["full_name"])
        destination.mkdir()
        return True

    monkeypatch.setattr(prepare, "clone_repository", clone)
    assert prepare.execute(repos, tmp_path, ["owner"], apply=True) == 0
    assert marker.read_text() == "my unsaved work"
    assert (owner / "linked").is_symlink()
    assert calls == ["owner/new"]


def test_symlink_owner_directory_is_not_followed(tmp_path, monkeypatch):
    protected = tmp_path / "protected"
    protected.mkdir()
    (tmp_path / "owner").symlink_to(protected, target_is_directory=True)
    monkeypatch.setattr(
        prepare, "clone_repository", lambda *a, **kw: pytest.fail("unexpected clone")
    )
    repo = {
        "owner": "owner",
        "name": "project",
        "full_name": "owner/project",
        "private": True,
        "archived": False,
    }
    assert prepare.execute([repo], tmp_path, ["owner"], apply=True) == 1
    assert list(protected.iterdir()) == []


def test_clone_uses_github_https_and_defers_lfs_without_prompting(
    tmp_path, monkeypatch
):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(prepare.subprocess, "run", run)
    assert prepare.clone_repository(
        {"full_name": "owner/project"}, tmp_path / "project"
    )
    args, kwargs = calls[0]
    assert args == [
        "git",
        "clone",
        "--no-single-branch",
        "--",
        "https://github.com/owner/project.git",
        str(tmp_path / "project"),
    ]
    assert kwargs["env"]["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_lfs_download_is_explicit_and_failure_is_reported(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, dict(kwargs["env"])))
        return SimpleNamespace(returncode=0 if len(calls) == 1 else 1)

    monkeypatch.setattr(prepare.subprocess, "run", run)
    destination = tmp_path / "project"
    assert not prepare.clone_repository(
        {"full_name": "owner/project"}, destination, with_lfs=True
    )
    assert calls[0][1]["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert calls[1][0] == ["git", "-C", str(destination), "lfs", "pull"]
    assert "GIT_LFS_SKIP_SMUDGE" not in calls[1][1]


def test_api_error_does_not_echo_sensitive_provider_output(monkeypatch):
    monkeypatch.setattr(
        prepare.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="HTTP 403 token=secret", stdout=""
        ),
    )
    with pytest.raises(RuntimeError) as error:
        prepare.github_page(1)
    assert "403" in str(error.value)
    assert "secret" not in str(error.value)


def test_invalid_api_response_is_not_treated_as_an_empty_list(monkeypatch):
    monkeypatch.setattr(
        prepare.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps({"error": "unknown"})
        ),
    )
    with pytest.raises(RuntimeError, match="形式"):
        prepare.github_page(1)
