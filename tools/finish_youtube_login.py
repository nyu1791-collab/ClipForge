"""Run on the authorized control device to register a manually obtained YouTube session."""

import argparse
import base64
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    args = ap.parse_args()
    if not args.run.isdigit():
        ap.error("A numeric login run ID is required")
    statefile = Path.home() / ".local/state/clipforge-youtube-login/session.json"
    state = json.loads(statefile.read_text())
    secret = state["root_token"].encode()
    export = hmac.new(secret, b"export", hashlib.sha256).hexdigest()
    deadline = time.monotonic() + 1200
    base = None
    with tempfile.TemporaryDirectory(prefix="clipforge-login-info-") as work:
        while time.monotonic() < deadline:
            r = subprocess.run(
                [
                    "gh",
                    "release",
                    "download",
                    "clipforge-youtube-login-" + args.run,
                    "--repo",
                    "nyu1791-collab/ai-model-workers",
                    "--pattern",
                    "session-info.json",
                    "--dir",
                    work,
                    "--clobber",
                ],
                capture_output=True,
            )
            if not r.returncode:
                base = json.loads((Path(work) / "session-info.json").read_text())[
                    "base_url"
                ]
                break
            time.sleep(15)
    if (
        not base
        or not base.startswith("https://")
        or not base.endswith(".trycloudflare.com")
    ):
        raise RuntimeError("Temporary login endpoint unavailable")
    print(
        "Waiting for the user to press login complete. No cookie values are logged.",
        flush=True,
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                base + "/" + export + "/cookies", timeout=20
            ) as response:
                if response.status == 202:
                    time.sleep(10)
                    continue
                data = response.read(36001)
        except urllib.error.HTTPError as e:
            if e.code in {502, 503}:
                time.sleep(10)
                continue
            raise RuntimeError(
                "Login connection closed; no session re-created"
            ) from None
        if (
            not data.startswith(b"# Netscape HTTP Cookie File")
            or not 0 < len(data) <= 36000
        ):
            raise RuntimeError("Invalid private cookie response")
        for line in data.decode().splitlines():
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            fields = line.removeprefix("#HttpOnly_").split("\t")
            if len(fields) != 7 or not (
                fields[0].lstrip(".") == "youtube.com"
                or fields[0].lstrip(".").endswith(".youtube.com")
            ):
                raise RuntimeError("Unexpected cookie domain")
        encoded = base64.b64encode(data).decode()
        registered = []
        for repo in ["nyu1791-collab/ai-model-workers", "nyu1791-collab/ClipForge"]:
            result = subprocess.run(
                ["gh", "secret", "set", "YOUTUBE_COOKIES_B64", "--repo", repo],
                input=encoded,
                text=True,
                capture_output=True,
            )
            if result.returncode:
                raise RuntimeError("Secret registration failed; values suppressed")
            registered.append(repo)
        req = urllib.request.Request(
            base + "/" + export + "/finish", data=b"", method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as response:
            response.read()
        subprocess.run(
            [
                "gh",
                "secret",
                "delete",
                "CLIPFORGE_YOUTUBE_LOGIN_SESSION",
                "--repo",
                "nyu1791-collab/ai-model-workers",
            ],
            capture_output=True,
        )
        statefile.write_text(
            json.dumps(
                {
                    "run_id": args.run,
                    "status": "cookies_registered",
                    "registered_repositories": registered,
                    "finished_at": time.time(),
                }
            )
        )
        statefile.chmod(0o600)
        print(
            json.dumps(
                {
                    "status": "cookies_registered",
                    "repositories": registered,
                    "credential_values_printed": False,
                }
            ),
            flush=True,
        )
        return
    raise RuntimeError("Login expired; no automatic restart")


if __name__ == "__main__":
    main()
