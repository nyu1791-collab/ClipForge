#!/usr/bin/env python3
"""Start long-form, TikTok, or both with separate resumable production states."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


CONFIGS = {
    "long": "config.landscape-long-preview.example.json",
    "tiktok": "config.tiktok.example.json",
    "tiktok-capcut": "config.tiktok-capcut.example.json",
}


def commands(url, output, mode, source_ref, clips=None, capcut=False):
    formats = ("long", "tiktok") if mode == "both" else (mode,)
    result = []
    for name in formats:
        destination = output / name
        if (destination / "production-state.json").exists():
            result.append(
                (
                    name,
                    [
                        sys.executable,
                        str(Path(__file__).with_name("produce_with_gemini.py")),
                        url,
                        "--resume",
                        "--output",
                        str(destination),
                    ],
                )
            )
            continue
        config = (
            CONFIGS["tiktok-capcut"] if name == "tiktok" and capcut else CONFIGS[name]
        )
        command = [
            sys.executable,
            str(Path(__file__).with_name("produce_with_gemini.py")),
            url,
            "--output",
            str(destination),
            "--config-file",
            config,
            "--source-ref",
            source_ref,
        ]
        if clips is not None:
            command.extend(["--clips", str(clips)])
        result.append((name, command))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=["long", "tiktok", "tiktok-capcut", "both"], default="tiktok"
    )
    parser.add_argument(
        "--source-ref", help="Pinned ClipForge commit; defaults to current main"
    )
    parser.add_argument("--clips", type=int, choices=range(1, 11))
    parser.add_argument(
        "--capcut-drafts-dir",
        type=Path,
        help="Explicit CapCut draft store on this computer",
    )
    parser.add_argument(
        "--capcut-cli-js", type=Path, help="Installed capcut-cli/dist/index.js"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    capcut = args.capcut_drafts_dir is not None or args.capcut_cli_js is not None
    capcut_ready = bool(
        args.capcut_drafts_dir and args.capcut_cli_js and shutil.which("node")
    )
    if capcut and not capcut_ready:
        print(
            "CapCut設定が不足しているため、動画を先に制作し、ドラフト出力は保留します。",
            flush=True,
        )
    source_ref = args.source_ref
    formats = ("long", "tiktok") if args.mode == "both" else (args.mode,)
    needs_new = any(
        not (args.output / name / "production-state.json").exists() for name in formats
    )
    if not source_ref and needs_new:
        source_ref = subprocess.run(
            [
                "gh",
                "api",
                "repos/nyu1791-collab/ClipForge/commits/main",
                "--jq",
                ".sha",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    if source_ref is not None and not re.fullmatch("[a-f0-9]{40}", source_ref):
        parser.error("source-ref must be an exact commit SHA")
    items = commands(args.url, args.output, args.mode, source_ref, args.clips, capcut)
    new_productions = sum("--resume" not in command for _, command in items)
    print(
        json.dumps(
            {
                "formats": [name for name, _ in items],
                "source_ref": source_ref,
                "outputs": {name: str(args.output / name) for name, _ in items},
                "new_productions": new_productions,
                "separate_model_requests": new_productions > 1,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.dry_run:
        return 0

    def finish(name, process):
        status = process.wait()
        record = {"production": status, "capcut": None}
        if capcut and name in ("tiktok", "tiktok-capcut"):
            if not capcut_ready:
                record["capcut"] = "pending"
                return record
            clips_dir = args.output / name / "clips" / "clips"
            clips = sorted(clips_dir.glob("clip-*"))
            if not clips:
                print(
                    f"No collected clips found for CapCut at {clips_dir}",
                    file=sys.stderr,
                    flush=True,
                )
                record["capcut"] = 1
                return record
            record["capcut"] = 0
            cache_path = args.output / name / "capcut-exports.json"
            cached = (
                json.loads(cache_path.read_text(encoding="utf-8"))
                if cache_path.is_file()
                else []
            )
            for clip in clips:
                try:
                    receipt = json.loads(
                        (clip / "render.json").read_text(encoding="utf-8")
                    )
                    key = [
                        clip.name,
                        receipt.get("key"),
                        str(args.capcut_drafts_dir.resolve()),
                        str(args.capcut_cli_js.resolve()),
                    ]
                    if any(
                        r["key"] == key
                        and (Path(r["draft"]) / "draft_content.json").is_file()
                        for r in cached
                    ):
                        continue
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("export_capcut_draft.py")),
                            str(clip),
                            "--drafts-dir",
                            str(args.capcut_drafts_dir),
                            "--cli-js",
                            str(args.capcut_cli_js),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    print(result.stdout, end="", flush=True)
                    if result.stderr:
                        print(result.stderr, file=sys.stderr, end="", flush=True)
                    if result.returncode:
                        record["capcut"] = result.returncode
                    else:
                        exported = json.loads(result.stdout)
                        cached.append({"key": key, "draft": exported["draft"]})
                        temporary = cache_path.with_suffix(".tmp")
                        temporary.write_text(
                            json.dumps(cached, ensure_ascii=False), encoding="utf-8"
                        )
                        temporary.replace(cache_path)
                except (OSError, ValueError, KeyError) as error:
                    print(f"{clip.name}: {error}", file=sys.stderr, flush=True)
                    record["capcut"] = 1
        return record

    def produce(name, command):
        try:
            process = subprocess.Popen(command)
        except OSError as error:
            print(f"{name}: {error}", file=sys.stderr, flush=True)
            return {"production": 1, "capcut": None}
        return finish(name, process)

    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        futures = [
            (name, pool.submit(produce, name, command)) for name, command in items
        ]
        statuses = {name: future.result() for name, future in futures}
    print(
        json.dumps(
            {
                "statuses": statuses,
                "note": "Resume a saved production instead of resubmitting it.",
            }
        ),
        flush=True,
    )
    return (
        0
        if all(
            row["production"] == 0 and row["capcut"] in (None, 0, "pending")
            for row in statuses.values()
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
