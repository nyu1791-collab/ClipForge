"""Upload completed long-form ClipForge clips without a readback audit."""

import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import zipfile
import uuid
import subprocess

from clipforge.release_transfer import download, read_transfer, validate_identity
from clipforge_drive_delivery import request


TARGET_ACCOUNTS = {"n.yu1791@gmail.com", "n.yu17919191@gmail.com"}


def authorize_target():
    print("DELIVERY_STAGE=authorize_upload", flush=True)
    raw = os.environ.get("GOOGLE_DRIVE_ADC_JSON")
    if not raw:
        raise RuntimeError("DRIVE_AUTH_NOT_CONFIGURED_FOR_TARGET_ACCOUNT")
    credential = json.loads(raw)
    body = urllib.parse.urlencode(
        {k: credential[k] for k in ("refresh_token", "client_id", "client_secret")}
        | {"grant_type": "refresh_token"}
    ).encode()
    with request(
        "https://oauth2.googleapis.com/token",
        "POST",
        raw=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as response:
        token = json.load(response)["access_token"]
    auth = {"Authorization": "Bearer " + token}
    if credential.get("quota_project_id"):
        auth["x-goog-user-project"] = credential["quota_project_id"]
    with request(
        "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)",
        headers=auth,
    ) as response:
        account = json.load(response).get("user", {}).get("emailAddress", "").lower()
    if account not in TARGET_ACCOUNTS:
        raise RuntimeError("DRIVE_TARGET_ACCOUNT_MISMATCH")
    print("DRIVE_TARGET_ACCOUNT=" + account, flush=True)
    return auth


def upload_analysis():
    run = os.environ.get("ANALYSIS_RUN", "")
    if not re.fullmatch(r"[1-9][0-9]*", run):
        raise RuntimeError("DRIVE_INVALID_ANALYSIS_RUN")
    auth = authorize_target()
    root = Path(os.environ["RUNNER_TEMP"]) / "drive-video-analysis"
    root.mkdir(mode=0o700)
    result = subprocess.run(
        [
            "gh",
            "release",
            "download",
            "clipforge-video-analysis-" + run,
            "--repo",
            "nyu1791-collab/ai-model-workers",
            "--dir",
            str(root),
        ],
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("DRIVE_ANALYSIS_DOWNLOAD_FAILED")
    receipt = json.loads((root / "video-analysis-receipt.json").read_text())
    analysis = json.loads((root / "video-analysis.json").read_text())
    if (
        receipt.get("status") != "complete"
        or receipt.get("model_version") != "gemini-3.8-flash"
    ):
        raise RuntimeError("DRIVE_ANALYSIS_NOT_COMPLETE")

    def clock(value):
        value = int(value)
        return f"{value // 60:02}:{value % 60:02}"

    lines = [
        analysis["source_title"],
        receipt["source_url"],
        "",
        analysis["summary"],
        "",
        "切り抜き候補（MP4はまだ作成していません）",
    ]
    for index, clip in enumerate(analysis["clips"], 1):
        lines += [
            f"{index}. {clip['title']}  {clock(clip['start_seconds'])}〜{clock(clip['end_seconds'])}",
            clip["reason"],
            "字幕補助: " + " / ".join(clip["subtitle_help"]),
            "",
        ]
    (root / "analysis.txt").write_text("\n".join(lines))
    with request(
        "https://www.googleapis.com/drive/v3/files?fields=id,webViewLink",
        "POST",
        payload={
            "name": "ClipForge 動画解析 " + run,
            "mimeType": "application/vnd.google-apps.folder",
        },
        headers=auth,
    ) as response:
        folder = json.load(response)
    for path in [
        root / "analysis.txt",
        root / "video-analysis.json",
        root / "video-analysis-receipt.json",
    ]:
        if path.stat().st_size > 4 * 1024**2:
            raise RuntimeError("DRIVE_ANALYSIS_FILE_TOO_LARGE")
        boundary = "clipforge_" + uuid.uuid4().hex
        mime = "text/plain" if path.suffix == ".txt" else "application/json"
        metadata = json.dumps({"name": path.name, "parents": [folder["id"]]}).encode()
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + metadata
            + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
            + path.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        with request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,size,webViewLink",
            "POST",
            raw=body,
            headers=auth | {"Content-Type": "multipart/related; boundary=" + boundary},
        ) as response:
            saved = json.load(response)
        print(
            "DRIVE_FILE_UPLOADED="
            + json.dumps(
                {
                    "name": saved["name"],
                    "link": saved["webViewLink"],
                    "bytes": int(saved["size"]),
                    "kind": "analysis_record",
                }
            ),
            flush=True,
        )
    print("DRIVE_FOLDER=" + folder["webViewLink"], flush=True)
    print(
        "DRIVE_DELIVERY_STATUS=analysis_uploaded; no MP4 was uploaded; private sharing unchanged",
        flush=True,
    )


def deliver():
    delivery_started = time.monotonic()
    transfer_started = time.monotonic()
    run_id = os.environ["RENDER_RUN"]
    request_id = os.environ["RENDER_REQUEST"]
    source = os.environ["SOURCE_REF"]
    if not re.fullmatch(r"[a-f0-9]{32}", request_id) or not re.fullmatch(
        r"[a-f0-9]{40}", source
    ):
        raise RuntimeError("Invalid render identity")
    root = Path(os.environ["RUNNER_TEMP"]) / "verified-render"
    name = "clipforge-result-" + request_id
    transfer = read_transfer(run_id, name)
    if transfer.get("source_ref") != source:
        raise RuntimeError("Render source identity differs")
    auth = authorize_target()
    resume_file = os.environ.get("DRIVE_RESUME_FROM_FILE", "")
    if resume_file:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", resume_file):
            raise RuntimeError("DRIVE_INVALID_RESUME_FILE")
        print("DELIVERY_STAGE=resolve_existing_folder", flush=True)
        with request("https://www.googleapis.com/drive/v3/files/" + resume_file
                     + "?fields=id,name,parents,size,appProperties,trashed", headers=auth) as response:
            anchor = json.load(response)
        anchor_name = "delivery/clips/clip-001/video.mp4"
        if (anchor.get("trashed") or anchor.get("name") != "clip-01.mp4"
                or len(anchor.get("parents", [])) != 1
                or anchor.get("appProperties", {}).get("sha256") != transfer["producer_sha256"].get(anchor_name)
                or int(anchor.get("size", -1)) != transfer["files"][anchor_name]["bytes"]):
            raise RuntimeError("DRIVE_RESUME_SOURCE_MISMATCH")
        with request("https://www.googleapis.com/drive/v3/files/" + anchor["parents"][0]
                     + "?fields=id,mimeType,trashed,webViewLink", headers=auth) as response:
            folder = json.load(response)
        if folder.get("trashed") or folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError("DRIVE_RESUME_FOLDER_INVALID")
    else:
        print("DELIVERY_STAGE=create_folder", flush=True)
        with request(
            "https://www.googleapis.com/drive/v3/files?fields=id,webViewLink",
            "POST",
            payload={
                "name": "ClipForge 切り抜き " + request_id[:8],
                "mimeType": "application/vnd.google-apps.folder",
            },
            headers=auth,
        ) as response:
            folder = json.load(response)
    print("DRIVE_FOLDER=" + folder["webViewLink"], flush=True)
    transfer_started = time.monotonic()
    print("DELIVERY_STAGE=fetch_outputs", flush=True)
    download(run_id, name, root)
    release_download_sec = time.monotonic() - transfer_started
    receipt = validate_identity(root, run_id, name, source, audit_hashes=False)
    manifest = json.loads((root / "manifest.json").read_text())
    if receipt.get("status") != "complete" or manifest.get("status") != "complete":
        raise RuntimeError("Render is incomplete")
    clips = sorted(root.glob("delivery/clips/clip-*/video.mp4"))
    if not 5 <= len(clips) <= 10:
        raise RuntimeError("Five to ten rendered clips are required")
    for clip in clips:
        relative = clip.relative_to(root).as_posix()
        if relative not in receipt["sha256"]:
            raise RuntimeError("Clip is not receipted")
    records = root.parent / "ClipForge-records.zip"
    with zipfile.ZipFile(records, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in {
                ".json",
                ".srt",
                ".ass",
                ".txt",
            }:
                archive.write(path, path.relative_to(root).as_posix())
    outputs = [(clip, f"clip-{i:02}.mp4") for i, clip in enumerate(clips, 1)] + [
        (records, records.name)
    ]
    existing = {}
    if resume_file:
        query = urllib.parse.urlencode({
            "q": "'" + folder["id"] + "' in parents and trashed = false",
            "fields": "files(id,name,size,webViewLink,appProperties),nextPageToken", "pageSize": 100,
        })
        with request("https://www.googleapis.com/drive/v3/files?" + query, headers=auth) as response:
            listing = json.load(response)
        if listing.get("nextPageToken"):
            raise RuntimeError("DRIVE_RESUME_FOLDER_TOO_LARGE")
        for item in listing.get("files", []):
            existing.setdefault(item["name"], []).append(item)
    for path, filename in outputs:
        file_started = time.monotonic()
        expected = (
            receipt.get("sha256", {}).get(path.relative_to(root).as_posix())
            if path.is_relative_to(root)
            else None
        )
        size = path.stat().st_size
        if filename in existing:
            matches = existing[filename]
            if (len(matches) != 1 or int(matches[0].get("size", -1)) != size
                    or (expected and matches[0].get("appProperties", {}).get("sha256") != expected)):
                raise RuntimeError("DRIVE_EXISTING_FILE_MISMATCH")
            print("DRIVE_FILE_REUSED=" + json.dumps({
                "name": filename, "link": matches[0]["webViewLink"], "bytes": size,
                "sha256": expected, "remote_readback_performed": False,
            }), flush=True)
            continue
        print("DELIVERY_STAGE=upload:" + filename, flush=True)
        mime = "video/mp4" if path.suffix == ".mp4" else "application/zip"
        with request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name,size,webViewLink",
            "POST",
            payload={
                "name": filename,
                "parents": [folder["id"]],
                "appProperties": {"sha256": expected} if expected else {},
            },
            headers=auth
            | {"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
        ) as response:
            location = response.headers["Location"]
        parsed = urllib.parse.urlsplit(location)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.googleapis.com",
            "content.googleapis.com",
        }:
            raise RuntimeError("Unexpected Drive upload endpoint")
        with path.open("rb") as stream:
            start = 0
            while start < size:
                chunk = stream.read(8 * 1024**2)
                end = start + len(chunk) - 1
                try:
                    with request(
                        location,
                        "PUT",
                        raw=chunk,
                        headers=auth
                        | {
                            "Content-Type": mime,
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                    ) as response:
                        result = json.load(response)
                except urllib.error.HTTPError as error:
                    if error.code != 308:
                        raise
                start = end + 1
        if int(result["size"]) != size:
            raise RuntimeError("Drive size mismatch")
        upload_or_reuse_sec = time.monotonic() - file_started
        print(
            "DRIVE_FILE_UPLOADED="
            + json.dumps(
                {
                    "name": filename,
                    "link": result["webViewLink"],
                    "bytes": size,
                    "sha256": expected,
                    "upload_or_reuse_sec": round(upload_or_reuse_sec, 3),
                    "remote_readback_performed": False,
                }
            ),
            flush=True,
        )
    print(
        "DELIVERY_TIMINGS="
        + json.dumps(
            {
                "release_download_sec": round(release_download_sec, 3),
                "delivery_wall_clock_sec": round(
                    time.monotonic() - delivery_started, 3
                ),
            }
        ),
        flush=True,
    )
    print("DRIVE_FOLDER=" + folder["webViewLink"], flush=True)
    print(
        "DRIVE_DELIVERY_STATUS=uploaded; no public sharing requested; no readback audit",
        flush=True,
    )


if __name__ == "__main__":
    try:
        if os.environ.get("DELIVERY_MODE") == "identity":
            authorize_target()
            print("DRIVE_AUTH_STATUS=target_account_ready", flush=True)
        elif os.environ.get("DELIVERY_MODE") == "upload-analysis":
            upload_analysis()
        else:
            deliver()
    except urllib.error.HTTPError as error:
        print("DRIVE_DELIVERY_FAILED_HTTP=" + str(error.code), flush=True)
        sys.exit(1)
    except Exception as error:
        print("DRIVE_DELIVERY_FAILED_TYPE=" + type(error).__name__, flush=True)
        if isinstance(error, RuntimeError) and str(error).startswith("DRIVE_"):
            print(str(error), flush=True)
        sys.exit(1)
