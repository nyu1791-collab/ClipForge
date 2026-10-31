"""Portable, offline workstation tasks. Each checkout owns its environment and data."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
MEDIA = {".mp4", ".mov", ".mkv", ".webm"}
PATH_KEYS = {
    "path",
    "file",
    "file_path",
    "media_path",
    "folder",
    "output",
    "output_dir",
    "output_directory",
    "source_path",
    "drafts_dir",
    "root",
    "project_dir",
    "manifest",
    "draft_fold_path",
    "draft_root_path",
    "draft_cover",
}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def profile(root=ROOT):
    return json.loads((root / "workstation/project.json").read_text(encoding="utf-8"))


def data_root(root=ROOT):
    return (
        Path(
            os.environ.get("AUTOMATION_DATA_ROOT", str(Path.home() / "AutomationData"))
        )
        / profile(root)["name"]
    )


def python_path(root):
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_commands(root=ROOT):
    data = profile(root)
    params = {
        "repo": str(root),
        "python": str(python_path(root)),
        "bootstrap_python": sys.executable,
        "data": str(data_root(root)),
    }
    return [[arg.format(**params) for arg in cmd] for cmd in data["setup"]]


def run_setup(root=ROOT, apply=False):
    commands = setup_commands(root)
    if apply:
        for command in commands:
            subprocess.run(command, cwd=root, check=True)
        data_root(root).mkdir(parents=True, exist_ok=True)
    return {"project": profile(root)["name"], "executed": apply, "commands": commands}


def doctor(root=ROOT):
    data = profile(root)
    tools = {name: bool(shutil.which(name)) for name in data.get("tools", [])}
    return {
        "project": data["name"],
        "platform": platform.system(),
        "python": platform.python_version(),
        "python_supported": sys.version_info >= (3, 11),
        "tools": tools,
        "environment_exists": python_path(root).is_file(),
        "data_directory": str(data_root(root)),
        "capcut_drafts_configured": bool(os.environ.get("CAPCUT_DRAFTS_DIR")),
        "credential_variable_present": {
            name: bool(os.environ.get(name))
            for name in data.get("credential_names", [])
        },
        "inference_performed": False,
        "missing_tools": [name for name, found in tools.items() if not found],
    }


def copy_and_hash(source, target):
    checksum = hashlib.sha256()
    with source.open("rb") as src, target.open("xb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
            checksum.update(chunk)
    return {"bytes": target.stat().st_size, "sha256": checksum.hexdigest()}


def package(source, destination, mode="long", title=""):
    """Copy deliverables into a relative-path publish kit; never export settings or tokens."""
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_dir():
        raise ValueError("制作済みフォルダがありません")
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("コピー先には元フォルダ外の新しいフォルダを指定してください")
    destination.parent.mkdir(parents=True, exist_ok=True)
    videos = sorted(
        p
        for p in source.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA and not p.is_symlink()
    )
    rows, inventory = [], {}
    with tempfile.TemporaryDirectory(
        prefix=".publish-kit-", dir=destination.parent
    ) as tmp:
        staging = Path(tmp) / "kit"
        staging.mkdir()
        for index, video in enumerate(videos, 1):
            folder = staging / "clips" / f"clip-{index:03}"
            folder.mkdir(parents=True)
            chosen = {"video" + video.suffix.lower(): video}
            for extension in ("srt", "ass"):
                match = next(
                    (
                        p
                        for p in [
                            video.with_suffix("." + extension),
                            video.parent / ("captions." + extension),
                            video.parent / ("subtitles." + extension),
                        ]
                        if p.is_file() and not p.is_symlink()
                    ),
                    None,
                )
                if match:
                    chosen["captions." + extension] = match
            cover = next(
                (
                    video.parent / name
                    for name in [
                        "cover.jpg",
                        "thumbnail.jpg",
                        "cover.png",
                        "thumbnail.png",
                    ]
                    if (video.parent / name).is_file()
                    and not (video.parent / name).is_symlink()
                ),
                None,
            )
            if cover:
                chosen["cover" + cover.suffix.lower()] = cover
            for name, path in chosen.items():
                inventory[(folder / name).relative_to(staging).as_posix()] = (
                    copy_and_hash(path, folder / name)
                )
            clip_title = title or video.stem
            # Generic deliveries need video.mp4. Other media remain available to editors;
            # avoid renaming a MOV/MKV to MP4 without actually remuxing it.
            rows.append(
                {
                    "directory": folder.relative_to(staging).as_posix(),
                    "mode": mode,
                    "title": clip_title,
                    "video": "video" + video.suffix.lower(),
                    "delivery_import_supported": video.suffix.lower() == ".mp4",
                }
            )
        receipt = {
            "schema": "automation.publish-kit.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "clips": rows,
            "files": inventory,
            "publication_status": "draft",
            "automatic_posting": False,
        }
        save(staging / "package.json", receipt)
        save(
            staging / "manifest.json",
            {
                "clips": [
                    {k: r[k] for k in ("directory", "mode", "title")}
                    for r in rows
                    if r["delivery_import_supported"]
                ]
            },
        )
        (staging / "投稿用メモ.txt").write_text(
            "\n".join(r["title"] for r in rows) + "\n", encoding="utf-8"
        )
        staging.rename(destination)
    return {
        "package": str(destination),
        "clips": len(rows),
        "manifest": str(destination / "manifest.json"),
        "publication_status": "draft",
    }


def replace_path(value, old_root, new_root):
    if re.match(r"^[A-Za-z]:[\\/]", old_root):
        original, prefix = PureWindowsPath(value), PureWindowsPath(old_root)
        try:
            relative = original.relative_to(prefix)
        except ValueError:
            return value
        if ".." in relative.parts:
            raise ValueError("親ディレクトリを含む素材パスは移行できません")
        return (PureWindowsPath(new_root) / relative).as_posix()
    original_value = value
    value, old_root = value.replace("\\", "/"), old_root.replace("\\", "/").rstrip("/")
    if value != old_root and not value.startswith(old_root + "/"):
        return original_value
    relative = value[len(old_root) :].lstrip("/")
    if ".." in relative.split("/"):
        raise ValueError("親ディレクトリを含む素材パスは移行できません")
    return new_root.replace("\\", "/").rstrip("/") + (
        "/" + relative if relative else ""
    )


def relink(value, old_root, new_root, key=""):
    if isinstance(value, dict):
        return {k: relink(v, old_root, new_root, k) for k, v in value.items()}
    if isinstance(value, list):
        return [relink(v, old_root, new_root, key) for v in value]
    if (
        isinstance(value, str)
        and (key.casefold() in PATH_KEYS or key.casefold().endswith(("_path", "_dir")))
        and not re.match(r"^[a-z]+://", value, re.I)
    ):
        return replace_path(value, old_root, new_root)
    return value


def postiz_draft(integration_id, content, media_id="", media_url=""):
    if bool(media_id) != bool(media_url):
        raise ValueError(
            "Postizへアップロード済みのmedia-idとmedia-urlを両方指定してください"
        )
    if media_url and not re.match(r"^https?://", media_url):
        raise ValueError("ローカル素材パスはPostizのmedia URLとして使用できません")
    images = [{"id": media_id, "path": media_url}] if media_id else []
    return {
        "type": "draft",
        "shortLink": False,
        "date": datetime.now(UTC).isoformat(),
        "tags": [],
        "posts": [
            {
                "integration": {"id": integration_id},
                "value": [{"content": content, "image": images}],
                "settings": {},
            }
        ],
    }


def workspace(manifest, root, projects=(), apply=False, install=False):
    catalog = json.loads(Path(manifest).read_text(encoding="utf-8"))
    root = Path(root).expanduser().resolve()
    selected = [
        r for r in catalog["repositories"] if not projects or r["name"] in projects
    ]
    if projects and len(selected) != len(set(projects)):
        raise ValueError("カタログにないプロジェクトです")
    commands, lock = [], []
    for row in selected:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", row["name"]) or not re.fullmatch(
            r"https://github.com/nyu1791-collab/[A-Za-z0-9_.-]+\.git", row["url"]
        ):
            raise ValueError("未承認のリポジトリ指定です")
        target = root / "repos" / row["name"]
        command = [
            "git",
            "clone",
            "--branch",
            row["revision"],
            "--single-branch",
            row["url"],
            str(target),
        ]
        commands.append(command)
        if apply:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(command, check=True)
            else:
                remote = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=target,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                if remote.removesuffix(".git") != row["url"].removesuffix(".git"):
                    raise ValueError("既存フォルダのリポジトリが異なります")
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            lock.append({"name": row["name"], "sha": sha, "directory": str(target)})
            if install:
                run_setup(target, True)
    if apply:
        save(
            root / "workspace-lock.json",
            {"repositories": lock, "contains_credentials": False},
        )
    return {"executed": apply, "commands": commands, "lock": lock}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    setup = sub.add_parser("setup")
    setup.add_argument("--apply", action="store_true")
    export = sub.add_parser("package")
    export.add_argument("source", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument(
        "--mode",
        choices=["long", "tiktok", "tiktok-capcut"],
        default=profile().get("package_mode", "long"),
    )
    export.add_argument("--title", default="")
    paths = sub.add_parser("relink")
    paths.add_argument("source", type=Path)
    paths.add_argument("destination", type=Path)
    paths.add_argument("--old-root", required=True)
    paths.add_argument("--new-root", required=True)
    post = sub.add_parser("postiz-draft")
    post.add_argument("--integration-id", required=True)
    post.add_argument("--content", required=True)
    post.add_argument("--media-id", default="")
    post.add_argument("--media-url", default="")
    post.add_argument("--output", type=Path, required=True)
    boot = sub.add_parser("workspace")
    boot.add_argument(
        "--manifest", type=Path, default=ROOT / "workstation/repositories.json"
    )
    boot.add_argument("--root", type=Path, required=True)
    boot.add_argument("--project", action="append", default=[])
    boot.add_argument("--apply", action="store_true")
    boot.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.command == "doctor":
        result = doctor()
    elif args.command == "setup":
        result = run_setup(apply=args.apply)
    elif args.command == "package":
        result = package(args.source, args.destination, args.mode, args.title)
    elif args.command == "relink":
        if (
            args.source.resolve() == args.destination.resolve()
            or args.destination.exists()
        ):
            raise ValueError(
                "元JSONを保持するため、新しい出力ファイルを指定してください"
            )
        if args.source.name.lower() in {"credentials.json", "adc.json", "cookie.json"}:
            raise ValueError("認証ファイルは移行の対象にできません")
        save(
            args.destination,
            relink(
                json.loads(args.source.read_text(encoding="utf-8")),
                args.old_root,
                args.new_root,
            ),
        )
        result = {
            "created": str(args.destination),
            "source_preserved": True,
            "media_copied": False,
            "verified_in_capcut": False,
        }
    elif args.command == "postiz-draft":
        if args.output.exists():
            raise ValueError("出力先が存在します")
        save(
            args.output,
            postiz_draft(
                args.integration_id, args.content, args.media_id, args.media_url
            ),
        )
        result = {"draft": str(args.output), "sent": False}
    else:
        result = workspace(
            args.manifest, args.root, args.project, args.apply, args.install
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
