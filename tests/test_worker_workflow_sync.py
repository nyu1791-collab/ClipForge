from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import check_worker_workflows as sync

REF = "a" * 40


@pytest.fixture
def directories(tmp_path, monkeypatch):
    source, worker = tmp_path / "source", tmp_path / "worker"
    for worker_name, source_name in sync.WORKFLOWS.items():
        for root, name in ((worker, worker_name), (source, source_name)):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("name: example\non: workflow_dispatch\n")
    monkeypatch.setattr(
        sync.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=REF + "\n")
    )
    return worker, source


def test_workflows_must_match_both_files_and_exact_source_commit(directories):
    worker, source = directories
    result = sync.check_workflows(worker, source, REF)
    assert result["status"] == "matched"
    assert len(result["files"]) == 2
    target = worker / next(iter(sync.WORKFLOWS))
    target.write_text("name: drift\n")
    result = sync.check_workflows(worker, source, REF)
    assert result["status"] == "mismatch"
    assert sum(row["matches"] for row in result["files"]) == 1
    with pytest.raises(ValueError, match="differs"):
        sync.check_workflows(worker, source, "b" * 40)


def test_missing_or_symlink_workflow_fails_closed(directories):
    worker, source = directories
    target = worker / next(iter(sync.WORKFLOWS))
    target.unlink()
    with pytest.raises(ValueError, match="regular"):
        sync.check_workflows(worker, source, REF)
    target.symlink_to(Path(source) / next(iter(sync.WORKFLOWS.values())))
    with pytest.raises(ValueError, match="regular"):
        sync.check_workflows(worker, source, REF)


def test_comparison_rejects_branch_names_before_git(directories):
    with pytest.raises(ValueError, match="exact"):
        sync.check_workflows(*directories, "main")
