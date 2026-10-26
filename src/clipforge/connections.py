"""Small, read-only connection map; never fetch credential values or start work."""

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess

from .github_cli import run


def describe_connections(repository="nyu1791-collab/ClipForge"):
    def names(endpoint, collection):
        try:
            data = json.loads(
                run(
                    "api",
                    endpoint,
                    "--jq",
                    f"[.{collection}[].name]",
                    timeout=20,
                    include_http_status=True,
                )
            )
            return {"status": "available", "names": data}
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            # Lack of permission to list secret names does not establish that a
            # runner cannot use the secrets. Do not block production on this view.
            return {"status": "unknown", "names": []}

    endpoints = [
        (f"repos/{repository}/actions/secrets?per_page=100", "secrets"),
        (f"repos/{repository}/actions/variables?per_page=100", "variables"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        secrets, variables = list(pool.map(lambda args: names(*args), endpoints))
    settings = f"https://github.com/{repository}/settings"

    def entry(listing, name, purpose, setup):
        return {
            "name": name,
            "purpose": purpose,
            "registration": (
                "unknown"
                if listing["status"] == "unknown"
                else "registered"
                if name in listing["names"]
                else "missing"
            ),
            "setup": setup,
        }

    return {
        "repository": repository,
        "connections": [
            entry(
                secrets,
                "CLIPFORGE_PAT",
                "非公開成果物の保存・回収",
                settings + "/secrets/actions",
            ),
            entry(
                variables,
                "CLIPFORGE_VERTEX_PROJECT_NUMBER",
                "指定VertexのOIDC接続",
                "docs/QUICK_START.md#初回接続",
            ),
            entry(
                secrets,
                "COLAB_ADC_JSON",
                "GPU文字起こし・描画",
                "tools/configure_colab_oauth.py",
            ),
            entry(
                secrets,
                "GOOGLE_DRIVE_ADC_JSON",
                "指定時だけDrive納品",
                "docs/TARGET_DRIVE.md",
            ),
            entry(
                secrets,
                "YOUTUBE_COOKIES_B64",
                "認証が必要なYouTube素材",
                settings + "/secrets/actions",
            ),
        ],
        "note": "登録名だけを参照。unknownは一覧権限を確認できない状態。推論・GPU確保・納品の成功を示しません。",
        "model_requests": 0,
        "gpu_allocations": 0,
    }


if __name__ == "__main__":
    print(json.dumps(describe_connections(), ensure_ascii=False, indent=2))
