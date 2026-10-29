#!/usr/bin/env python3
"""List accessible repositories, then optionally clone them without overwriting work."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
PAGE_SIZE = 100
MAX_PAGES = 100


def valid_repository(owner, name):
    return bool(
        isinstance(owner, str)
        and OWNER_PATTERN.fullmatch(owner)
        and isinstance(name, str)
        and REPO_PATTERN.fullmatch(name)
        and name not in {".", ".."}
    )


def github_page(page):
    endpoint = (
        f"user/repos?per_page={PAGE_SIZE}&page={page}"
        "&affiliation=owner,collaborator,organization_member&sort=full_name"
    )
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", endpoint],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            "GitHub の一覧を取得できません。接続を確認してください。"
        ) from error
    if result.returncode:
        match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", result.stderr)
        suffix = f"（HTTP {match.group(1)}）" if match else ""
        raise RuntimeError(
            f"GitHub の一覧を取得できません{suffix}。gh のログインと権限を確認してください。"
        )
    try:
        rows = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError("GitHub の一覧を読み取れません。") from error
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
        raise RuntimeError("GitHub の一覧の形式が不正です。")
    return rows


def discover_repositories(owners, *, fetch_page=github_page):
    requested = {owner.casefold() for owner in owners}
    repositories = {}
    for page in range(1, MAX_PAGES + 1):
        rows = fetch_page(page)
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("owner"), dict):
                raise RuntimeError("GitHub のリポジトリ情報の形式が不正です。")
            owner, name = row["owner"].get("login"), row.get("name")
            if not valid_repository(owner, name):
                raise RuntimeError("GitHub のリポジトリ名を安全に扱えません。")
            if owner.casefold() not in requested:
                continue
            full_name = f"{owner}/{name}"
            if str(row.get("full_name", "")).casefold() != full_name.casefold():
                raise RuntimeError("GitHub のリポジトリ情報が一致しません。")
            repositories[full_name.casefold()] = {
                "owner": owner,
                "name": name,
                "full_name": full_name,
                "private": row.get("private"),
                "archived": row.get("archived") is True,
                "default_branch": row.get("default_branch"),
                "size_kb": row.get("size")
                if type(row.get("size")) is int and row["size"] >= 0
                else None,
            }
        if len(rows) < PAGE_SIZE:
            return sorted(
                repositories.values(), key=lambda row: row["full_name"].casefold()
            )
    raise RuntimeError("GitHub の一覧が上限を超えたため、途中で停止しました。")


def clone_repository(repo, destination, *, with_lfs=False):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Clone without implicit LFS downloads. An explicit request fetches the
    # checked-out branch's LFS objects afterwards and reports a failure.
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    try:
        result = subprocess.run(
            [
                "git",
                "clone",
                "--no-single-branch",
                "--",
                f"https://github.com/{repo['full_name']}.git",
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            env=env,
        )
        if result.returncode:
            return False
        if with_lfs:
            env.pop("GIT_LFS_SKIP_SMUDGE", None)
            result = subprocess.run(
                ["git", "-C", str(destination), "lfs", "pull"],
                capture_output=True,
                text=True,
                timeout=1800,
                env=env,
            )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def execute(repositories, root, owners, *, apply=False, with_lfs=False):
    root = root.expanduser().absolute()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError("取得先は既存のシンボリックリンクやファイルにできません。")
    visible_owners = {row["owner"].casefold() for row in repositories}
    missing = [owner for owner in owners if owner.casefold() not in visible_owners]
    print(f"取得先: {root}")
    print(f"確認できたリポジトリ: {len(repositories)} 件")
    for repo in repositories:
        visibility = (
            "非公開"
            if repo["private"] is True
            else "公開"
            if repo["private"] is False
            else "不明"
        )
        archived = "・アーカイブ済み" if repo["archived"] else ""
        branch = repo.get("default_branch")
        branch_display = (
            branch
            if isinstance(branch, str)
            and re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch)
            else "要確認"
        )
        branch_info = f"・既定 {branch_display}"
        size = (
            f"・GitHub 表示 {repo['size_kb']} KB"
            if repo.get("size_kb") is not None
            else ""
        )
        print(f"  {repo['full_name']}（{visibility}{archived}{branch_info}{size}）")
    print("GitHub 表示のサイズは、取得後の使用容量や追加データ量を保証しません。")
    if missing:
        print("一覧に表示されない指定先: " + ", ".join(missing), file=sys.stderr)
        print(
            "存在しないとは限りません。GitHub の接続と閲覧権限を確認してください。",
            file=sys.stderr,
        )
    if not apply:
        print(
            "\n一覧の確認のみです。取得するには同じコマンドに --clone を付けてください。"
        )
        return 1 if missing else 0

    root.mkdir(parents=True, exist_ok=True)
    created = skipped = failed = 0
    for repo in repositories:
        parent = root / repo["owner"]
        destination = parent / repo["name"]
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            print(f"取得先が安全でないため停止: {repo['full_name']}", file=sys.stderr)
            failed += 1
            continue
        if destination.exists() or destination.is_symlink():
            print(f"既存のため変更せずにスキップ: {repo['full_name']}")
            skipped += 1
            continue
        parent.mkdir(exist_ok=True)
        print(f"取得中: {repo['full_name']}", flush=True)
        if clone_repository(repo, destination, with_lfs=with_lfs):
            print(f"取得完了: {repo['full_name']}")
            if (destination / ".gitmodules").is_file():
                print(
                    "  サブモジュールは未取得です。必要な場合は手順を確認してください。"
                )
            created += 1
        else:
            print(
                f"取得失敗: {repo['full_name']}。GitHub の権限・Git の認証・通信・空き容量を確認してください。",
                file=sys.stderr,
            )
            print(
                "途中のフォルダがあれば、消さずに中身を確認してください。",
                file=sys.stderr,
            )
            failed += 1
    print(f"\n取得 {created} 件 / 既存のためスキップ {skipped} 件 / 失敗 {failed} 件")
    print("既存フォルダ、ブランチのマージ、デプロイ、制作は変更していません。")
    if not with_lfs:
        print("Git LFS の実ファイルとサブモジュールは別途確認が必要です。")
    return 1 if failed or missing else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="GitHub で見える指定所有者のリポジトリを一覧し、必要なら取得します。"
    )
    parser.add_argument(
        "--owner", action="append", required=True, help="GitHub の所有者。複数指定可"
    )
    parser.add_argument(
        "--root", type=Path, default=Path.home() / "projects", help="取得先"
    )
    parser.add_argument(
        "--clone", action="store_true", help="一覧だけでなく未取得のリポジトリを取得"
    )
    parser.add_argument(
        "--with-lfs", action="store_true", help="取得時に Git LFS の実ファイルも要求"
    )
    args = parser.parse_args(argv)
    if args.with_lfs and not args.clone:
        parser.error("--with-lfs には --clone が必要です")
    if any(not OWNER_PATTERN.fullmatch(owner) for owner in args.owner):
        parser.error("所有者の名前が不正です")
    if not shutil.which("gh") or not shutil.which("git"):
        parser.error("GitHub CLI の gh と git を先にインストールしてください")
    if args.with_lfs:
        try:
            result = subprocess.run(
                ["git", "lfs", "version"], capture_output=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            parser.error(
                "Git LFS を確認できません。インストールしてから再実行してください"
            )
        if result.returncode:
            parser.error("Git LFS が必要です。インストールしてから再実行してください")
    try:
        repos = discover_repositories(args.owner)
        return execute(
            repos, args.root, args.owner, apply=args.clone, with_lfs=args.with_lfs
        )
    except (OSError, RuntimeError) as error:
        print(f"準備を中断しました: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
