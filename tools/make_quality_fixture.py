#!/usr/bin/env python3
"""Generate a 36-second synthetic video with three labelled scenes and speech."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

SCENES = [
    (
        "blue",
        "INTRODUCTION",
        "Welcome to Clip Forge. This is a synthetic quality review video. The first scene introduces our topic, and the screen is blue.",
    ),
    (
        "red",
        "IMPORTANT MOMENT",
        "Now the screen turns red. Here is the important moment. The answer is forty two. Remember this highlight and keep the full explanation.",
    ),
    (
        "green",
        "CONCLUSION",
        "The screen is green now. We have reached the conclusion. Accurate captions and complete context are more important than fast but confusing cuts.",
    ),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "source.mp4").exists():
        raise SystemExit("Choose a new fixture directory; existing fixture preserved.")
    voice = shutil.which("espeak-ng") or shutil.which("espeak")
    if not voice:
        raise SystemExit("Install espeak-ng to generate synthetic speech.")
    for index, (_, _, text) in enumerate(SCENES):
        subprocess.run(
            [
                voice,
                "-v",
                "en-us",
                "-s",
                "155",
                "-w",
                str(output / f"speech-{index}.wav"),
                text,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-n"]
    for index in range(3):
        command += ["-i", str(output / f"speech-{index}.wav")]
    audio_filters = ";".join(
        f"[{i}:a]apad,atrim=duration=12,aformat=sample_rates=16000:channel_layouts=mono[a{i}]"
        for i in range(3)
    )
    audio_filters += ";[a0][a1][a2]concat=n=3:v=0:a=1[a]"
    subprocess.run(
        command
        + [
            "-filter_complex",
            audio_filters,
            "-map",
            "[a]",
            "-c:a",
            "pcm_s16le",
            str(output / "speech.wav"),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-n"]
    for color, label, _ in SCENES:
        command += [
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:r=15:d=12,drawtext=text=SCENE {label}:fontcolor=white:fontsize=18:x=15:y=15",
        ]
    command += [
        "-i",
        str(output / "speech.wav"),
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map",
        "[v]",
        "-map",
        "3:a:0",
        "-t",
        "36",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "16000",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(output / "source.mp4"),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    (output / "fixture.json").write_text(
        json.dumps(
            {
                "kind": "synthetic_quality_fixture",
                "duration": 36,
                "scenes": [
                    {
                        "start": i * 12,
                        "end": (i + 1) * 12,
                        "color": c,
                        "label": label,
                        "speech": text,
                    }
                    for i, (c, label, text) in enumerate(SCENES)
                ],
            },
            indent=2,
        )
    )
    print("Synthetic quality fixture generated (36 seconds).")


if __name__ == "__main__":
    main()
