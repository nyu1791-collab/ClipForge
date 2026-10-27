import json
from pathlib import Path
import stat
import zipfile

import pytest

from clipforge import release_transfer as transfer

RUN = "40187168082"
NAME = "clipforge-analysis-" + "a" * 32
SHA = "b" * 40


@pytest.fixture
def stage(tmp_path):
    root = tmp_path / "stage"
    root.mkdir()
    (root / "transcripts").mkdir()
    (root / "transcripts/asr.json").write_text('{"text":"検証"}')
    receipt = {
        "request_id": "a" * 32,
        "source_ref": SHA,
        "workflow_run": RUN,
        "sha256": {"transcripts/asr.json": transfer.sha(root / "transcripts/asr.json")},
    }
    (root / "request-receipt.json").write_text(json.dumps(receipt))
    return root


def manifest(root):
    return {
        "schema": 1,
        "run_id": RUN,
        "name": NAME,
        "source_ref": SHA,
        "files": transfer.inventory(root),
    }


def test_verify_stage_and_identity(stage):
    transfer.verify_manifest(stage, manifest(stage))
    with pytest.raises(transfer.TransferError):
        transfer.validate_identity(stage, RUN, NAME, "c" * 40)


@pytest.mark.parametrize("change", ["missing", "tamper", "extra", "symlink"])
def test_reject_changed_stage(stage, change):
    record = manifest(stage)
    path = stage / "transcripts/asr.json"
    if change == "missing":
        path.unlink()
    elif change == "tamper":
        path.write_text("changed")
    elif change == "extra":
        (stage / "extra").write_text("unexpected")
    else:
        path.unlink()
        path.symlink_to(stage / "request-receipt.json")
    with pytest.raises(transfer.TransferError):
        transfer.verify_manifest(stage, record)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../b", "a\\b", "a//b"])
def test_reject_unsafe_archive_names(tmp_path, name):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr(name, "unsafe")
    with pytest.raises(transfer.TransferError):
        transfer.safe_extract(archive, tmp_path / "unpacked")


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO])
def test_reject_archive_links_and_devices(tmp_path, mode):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as out:
        entry = zipfile.ZipInfo("link")
        entry.external_attr = (mode | 0o600) << 16
        out.writestr(entry, "request-receipt.json")
    with pytest.raises(transfer.TransferError):
        transfer.safe_extract(archive, tmp_path / "unpacked")


def test_duplicate_and_existing_destination(tmp_path):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("same", "first")
        with pytest.warns(UserWarning):
            out.writestr("same", "second")
    with pytest.raises(transfer.TransferError):
        transfer.safe_extract(archive, tmp_path / "unpacked")
    with pytest.raises(transfer.TransferError):
        transfer.safe_extract(archive, tmp_path)


def test_no_parent_directory_guessing(stage, tmp_path):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as out:
        for path in stage.rglob("*"):
            if path.is_file():
                out.write(path, "parent/" + path.relative_to(stage).as_posix())
    destination = tmp_path / "unpacked"
    transfer.safe_extract(archive, destination)
    with pytest.raises(transfer.TransferError):
        transfer.verify_manifest(destination, manifest(stage))


def test_remote_roundtrip_and_tamper(stage, tmp_path, monkeypatch):
    files = transfer.inventory(stage)
    archive = tmp_path / "remote.zip"
    with zipfile.ZipFile(archive, "w") as out:
        for name in files:
            out.write(stage / name, name)
    record = {**manifest(stage), "archive_sha256": transfer.sha(archive)}
    monkeypatch.setattr(transfer, "private_repo", lambda: None)
    monkeypatch.setattr(
        transfer,
        "release",
        lambda tag: {
            "assets": [
                {"name": "result.zip", "size": archive.stat().st_size},
                {"name": "transfer.json", "size": 1000},
            ]
        },
    )

    def gh(*args):
        folder = Path(args[-1])
        (folder / "result.zip").write_bytes(archive.read_bytes())
        (folder / "transfer.json").write_text(json.dumps(record))

    monkeypatch.setattr(transfer, "gh", gh)
    destination = tmp_path / "result"
    transfer.download(RUN, NAME, destination, record)
    assert transfer.inventory(destination) == files
    record["archive_sha256"] = "0" * 64
    with pytest.raises(transfer.TransferError, match="RESULT_SHA_MISMATCH"):
        transfer.download(RUN, NAME, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_current_transfer_checks_files_and_producer_hashes_during_extraction(
    stage, tmp_path, monkeypatch
):
    receipt = json.loads((stage / "request-receipt.json").read_text())
    record = {
        "schema": 2,
        "run_id": RUN,
        "name": NAME,
        "source_ref": SHA,
        "files": transfer.inventory(stage, include_hashes=False),
        "producer_sha256": receipt["sha256"],
        "request_receipt": receipt,
        "stage_manifest": {},
        "gpu_receipt": None,
        "post_upload_audit": "not_run",
    }
    archive = tmp_path / "remote.zip"

    def write_archive(*, changed=False, extra=False):
        with zipfile.ZipFile(archive, "w") as out:
            for name in record["files"]:
                data = (stage / name).read_bytes()
                if changed and name == "transcripts/asr.json":
                    data = b"X" + data[1:]  # preserve length and ZIP CRC correctness
                out.writestr(name, data)
            if extra:
                out.writestr("unexpected", "data")

    write_archive()
    monkeypatch.setattr(
        transfer,
        "release",
        lambda tag: {
            "assets": [
                {"name": "result.zip", "size": archive.stat().st_size},
                {"name": "transfer.json", "size": 1000},
            ]
        },
    )

    def gh(*args):
        folder = Path(args[-1])
        (folder / "result.zip").write_bytes(archive.read_bytes())
        (folder / "transfer.json").write_text(json.dumps(record))

    monkeypatch.setattr(transfer, "gh", gh)
    transfer.download(RUN, NAME, tmp_path / "valid")
    write_archive(changed=True)
    with pytest.raises(transfer.TransferError, match="RESULT_SHA_MISMATCH"):
        transfer.download(RUN, NAME, tmp_path / "changed")
    assert not (tmp_path / "changed").exists()
    write_archive(extra=True)
    with pytest.raises(transfer.TransferError, match="RESULT_STRUCTURE_INVALID"):
        transfer.download(RUN, NAME, tmp_path / "extra")
    assert not (tmp_path / "extra").exists()


def test_missing_remote_is_not_success(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer, "private_repo", lambda: None)
    monkeypatch.setattr(transfer, "release", lambda tag: None)
    with pytest.raises(transfer.TransferError, match="RESULT_NOT_FOUND"):
        transfer.download(RUN, NAME, tmp_path / "missing")


def test_cli_requires_command():
    with pytest.raises(SystemExit) as error:
        transfer.main([])
    assert error.value.code != 0


def test_legacy_module_must_not_report_a_noop_as_success():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "clipforge.github_cli", "publish", "missing", NAME],
        capture_output=True,
    )
    assert result.returncode != 0


def test_corrupt_release_never_falls_back_to_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(transfer, "private_repo", lambda: None)
    monkeypatch.setattr(transfer, "release", lambda tag: {"assets": []})

    def artifact(*args):
        pytest.fail("a corrupt Release must not trigger an Artifact fallback")

    with pytest.raises(transfer.TransferError):
        transfer.download_stage(RUN, NAME, tmp_path / "out", artifact)


def test_publish_rejects_remote_failure_and_never_overwrites(stage, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", RUN)
    monkeypatch.setattr(transfer, "private_repo", lambda: None)
    monkeypatch.setattr(transfer, "release", lambda tag: {"assets": []})
    monkeypatch.setattr(transfer, "read_transfer", lambda *args: {"different": True})

    def never_upload(*args):
        pytest.fail("existing tags must never be overwritten")

    monkeypatch.setattr(transfer, "gh", never_upload)
    with pytest.raises(transfer.TransferError, match="RESULT_STRUCTURE_INVALID"):
        transfer.publish(stage, NAME)
    assert transfer.main(["publish", str(stage), NAME]) == 1


def test_publish_reuses_identical_release_without_upload(stage, monkeypatch, capsys):
    receipt = json.loads((stage / "request-receipt.json").read_text())
    record = {
        "schema": 2,
        "run_id": RUN,
        "name": NAME,
        "source_ref": SHA,
        "files": transfer.inventory(stage, include_hashes=False),
        "producer_sha256": receipt["sha256"],
        "request_receipt": receipt,
        "stage_manifest": {},
        "gpu_receipt": None,
        "post_upload_audit": "not_run",
    }
    monkeypatch.setenv("GITHUB_RUN_ID", RUN)
    monkeypatch.setattr(transfer, "private_repo", lambda: None)
    monkeypatch.setattr(transfer, "release", lambda tag: {"assets": []})
    monkeypatch.setattr(transfer, "read_transfer", lambda *args: record)
    monkeypatch.setattr(transfer, "gh", lambda *args: pytest.fail("unexpected upload"))
    transfer.publish(stage, NAME)
    assert "RESULT_REUSED" in capsys.readouterr().out


def test_archive_rejects_destination_with_symlink_parent(tmp_path):
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("file.json", "{}")
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(transfer.TransferError):
        transfer.safe_extract(archive, tmp_path / "link/unpacked")
