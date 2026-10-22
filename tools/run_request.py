#!/usr/bin/env python3
"""Private GitHub runner entry point. Raw video never enters public artifacts."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PRIVATE_REPO = "nyu1791-collab/ai-model-workers"


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError("private input operation failed")
    return result.stdout


def check(tag, remote=True):
    if os.environ.get("GITHUB_REPOSITORY") != PRIVATE_REPO:
        raise RuntimeError("video execution requires the designated private repository")
    if not re.fullmatch(r"clipforge-input-[a-f0-9]{32}", tag):
        raise RuntimeError("unexpected execution repository or input tag")
    if (
        remote
        and gh("api", "repos/" + PRIVATE_REPO, "--jq", ".private").strip() != "true"
    ):
        raise RuntimeError("worker repository must remain private")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run", "cleanup"])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, default=Path("job-input"))
    parser.add_argument("--output", type=Path, default=Path("out"))
    args = parser.parse_args()
    check(args.tag, remote=args.command != "run")
    if args.command == "cleanup":
        gh(
            "release",
            "delete",
            args.tag,
            "--repo",
            PRIVATE_REPO,
            "--yes",
            "--cleanup-tag",
        )
        print("Temporary private input release deleted.")
        return
    if args.command == "prepare":
        assets = json.loads(
            gh("release", "view", args.tag, "--repo", PRIVATE_REPO, "--json", "assets")
        )["assets"]
        manifest = next((a for a in assets if a["name"] == "request.json"), None)
        if not manifest or not 0 < manifest["size"] <= 65536:
            raise RuntimeError("request manifest missing or oversized")
        args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        gh(
            "release",
            "download",
            args.tag,
            "--repo",
            PRIVATE_REPO,
            "--pattern",
            "request.json",
            "--dir",
            str(args.directory),
        )
    request = json.loads((args.directory / "request.json").read_text())
    if set(request) - {
        "request_id",
        "source_ref",
        "video_sha256",
        "video_bytes",
        "settings",
        "chat_sha256",
        "audio_source_url",
    }:
        raise RuntimeError("unknown request fields")
    if args.tag != "clipforge-input-" + request["request_id"]:
        raise RuntimeError("request identity mismatch")
    if request["source_ref"] != os.environ.get("CLIPFORGE_SOURCE_REF"):
        raise RuntimeError("source revision mismatch")
    for field, pattern in [
        ("source_ref", r"[a-f0-9]{40}"),
        ("video_sha256", r"[a-f0-9]{64}"),
    ]:
        if not isinstance(request.get(field), str) or not re.fullmatch(
            pattern, request[field]
        ):
            raise RuntimeError("invalid request checksum or revision")
    if request.get("chat_sha256") is not None and not re.fullmatch(
        r"[a-f0-9]{64}", request["chat_sha256"]
    ):
        raise RuntimeError("invalid chat checksum")
    from clipforge.models import Settings
    from clipforge.storage import digest, fingerprint, write_json

    settings = Settings.model_validate(request["settings"])
    if request.get("audio_source_url"):
        from clipforge.kick_media import canonical

        canonical(request["audio_source_url"])
        if (
            settings.image_search
            or settings.table_query
            or settings.chat_url
            or request.get("chat_sha256")
        ):
            raise RuntimeError("audio analysis cannot include external lookups or chat")
    if os.environ.get("CLIPFORGE_REQUIRE_CUDA") == "1":
        settings.whisper_device = "cuda"
        settings.whisper_compute_type = "float16"
    if (
        type(request["video_bytes"]) is not int
        or not 0 < request["video_bytes"] <= settings.max_input_bytes
    ):
        raise RuntimeError("input size exceeds the job limit")
    if args.command == "prepare":
        video = next((a for a in assets if a["name"] == "input-video.bin"), None)
        if video is None or video["size"] != request["video_bytes"]:
            raise RuntimeError("input asset size mismatch")
        gh(
            "release",
            "download",
            args.tag,
            "--repo",
            PRIVATE_REPO,
            "--pattern",
            "input-video.bin",
            "--dir",
            str(args.directory),
        )
        if request.get("chat_sha256"):
            chat = next((a for a in assets if a["name"] == "chat.json"), None)
            if not chat or not 0 < chat["size"] <= 20000000:
                raise RuntimeError("chat asset missing or oversized")
            gh(
                "release",
                "download",
                args.tag,
                "--repo",
                PRIVATE_REPO,
                "--pattern",
                "chat.json",
                "--dir",
                str(args.directory),
            )
        identity = fingerprint(
            {
                "video": request["video_sha256"],
                "settings": settings.model_dump(),
                "chat": request.get("chat_sha256"),
                "source": request["source_ref"],
            }
        )
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
            handle.write("cache_key=" + identity + "\n")
        print("Private input staged; no media paths or URLs logged.")
        return
    source = args.directory / "input-video.bin"
    if (
        digest(source) != request["video_sha256"]
        or source.stat().st_size != request["video_bytes"]
    ):
        raise RuntimeError("input checksum mismatch")
    chat_file = args.directory / "chat.json" if request.get("chat_sha256") else None
    if chat_file and digest(chat_file) != request["chat_sha256"]:
        raise RuntimeError("chat checksum mismatch")
    if request.get("audio_source_url"):
        import shutil
        from clipforge.audio_first import allocate_audio, exclusive

        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive(args.output):
            audio = args.output / "audio.m4a"
            if audio.exists() and digest(audio) != request["video_sha256"]:
                raise RuntimeError("saved audio checksum mismatch")
            if not audio.exists():
                shutil.copyfile(source, audio)
            analysis, _ = allocate_audio(
                audio, args.output, settings, request["audio_source_url"]
            )
            result = {
                **analysis,
                "rendered": [],
                "source_sha256": request["video_sha256"],
            }
            write_json(args.output / "manifest.json", result)
    else:
        from clipforge.pipeline import run

        result = run(source, args.output, settings, resume=True, chat_file=chat_file)
    result_hashes = {}
    if request.get("audio_source_url"):
        cache = json.loads((args.output / "asr-receipt.json").read_text())
        names = [
            "audio.m4a",
            "manifest.json",
            "topics.json",
            "asr-receipt.json",
            "transcripts/" + cache["key"] + ".json",
        ]
        result_hashes = {name: digest(args.output / name) for name in names}
    write_json(
        args.output / "request-receipt.json",
        {
            "request_id": request["request_id"],
            "source_ref": request["source_ref"],
            "workflow_run": os.environ["GITHUB_RUN_ID"],
            "status": result["status"],
            "rendered_clips": len(result["rendered"]),
            "audio_source_url": request.get("audio_source_url"),
            "sha256": result_hashes,
        },
    )
    print(json.dumps({"status": result["status"], "clips": len(result["rendered"])}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Private video job failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
