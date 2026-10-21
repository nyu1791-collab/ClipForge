#!/usr/bin/env python3
"""Runner-only render review using generated fixtures. No user media, AI or API keys."""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

from clipforge.models import Settings
from clipforge.pipeline import cut
from clipforge.storage import PipelineError, digest, process, write_json
from clipforge.typography import resolve_font


def frame(video, target, time):
    process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-ss",
            str(time),
            "-i",
            video,
            "-frames:v",
            "1",
            target,
        ]
    )
    return Image.open(target).convert("RGB")


def mean_difference(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("review output must be new or empty")
    root.mkdir(parents=True, exist_ok=True)

    image = Image.new("RGB", (960, 540), "#14233a")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 959, 333), fill="#245f98")
    for x in range(0, 960, 80):
        draw.line((x, 0, x, 333), fill="#62bad6", width=3)
    for y in range(0, 334, 60):
        draw.line((0, y, 959, y), fill="#62bad6", width=3)
    draw.rectangle((760, 40, 910, 180), fill="#edb330")
    draw.text((340, 130), "SYNTHETIC GAME CONTENT", fill="white")
    # A generated webcam test chart. Edge marks reveal cropping at punch-in cuts.
    draw.rectangle((40, 360, 239, 519), fill="#c95c57")
    for x in range(40, 240, 20):
        draw.line((x, 360, x, 519), fill="#602322", width=2)
    draw.ellipse((90, 385, 189, 494), fill="#f2cba8")
    draw.ellipse((111, 416, 122, 429), fill="#182237")
    draw.ellipse((157, 416, 168, 429), fill="#182237")
    draw.arc((120, 438, 158, 468), 0, 180, fill="#651c25", width=4)
    image.save(root / "source.png")
    source = root / "source.mp4"
    process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-loop",
            "1",
            "-framerate",
            "15",
            "-i",
            root / "source.png",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            source,
        ]
    )
    source_sha = digest(source)
    words = [
        (2.2, 2.8, "親に"),
        (2.8, 3.4, "なった"),
        (3.4, 3.9, "途端に"),
        (4.2, 5.0, "教育を"),
        (5.0, 6.0, "語り出す。"),
        (6.4, 6.9, "リアクションが"),
        (6.9, 7.5, "大事。"),
    ]
    transcript_path = root / "transcript.json"
    write_json(
        transcript_path,
        {
            "language": "ja",
            "segments": [
                {
                    "start": 2.2,
                    "end": 7.5,
                    "text": "".join(w[2] for w in words),
                    "words": [dict(start=a, end=b, text=t) for a, b, t in words],
                }
            ],
        },
    )
    profile = Settings(
        gemini_model="manual-no-api",
        framing="stacked",
        height=1920,
        headline="親になって変わった話",
        threads=2,
        content_region=dict(x=0, y=0, width=1, height=0.62),
        presenter_region=dict(
            x=40 / 960, y=360 / 540, width=200 / 960, height=160 / 540
        ),
        punch_ins=[dict(start=4, end=6, zoom=1.4)],
    )
    write_json(root / "profile.json", profile.model_dump())
    stacked = cut(source, root / "stacked", 2, 8, profile, transcript_path)
    receipt = stacked["rendered"][0]
    video = root / "stacked/clips/clip-001/video.mp4"
    assert (receipt["media"]["width"], receipt["media"]["height"]) == (1080, 1920)
    assert receipt["media"]["audio_streams"] == 1
    assert receipt["media"]["full_decode_verified"]
    assert receipt["media"]["layout"]["punch_ins"] == [
        {"start": 2, "end": 4, "zoom": 1.4}
    ]
    original_video_sha = digest(video)
    cut(source, root / "stacked", 2, 8, profile, transcript_path, resume=True)
    assert digest(video) == original_video_sha
    snapshots = [
        frame(video, root / f"stacked-{i}.png", t)
        for i, t in enumerate([0.8, 2.8, 4.8])
    ]
    content = (120, 470, 940, 950)
    presenter = (300, 1040, 780, 1410)
    stable_content = mean_difference(
        snapshots[0].crop(content), snapshots[1].crop(content)
    )
    changed_presenter = mean_difference(
        snapshots[0].crop(presenter), snapshots[1].crop(presenter)
    )
    restored_presenter = mean_difference(
        snapshots[0].crop(presenter), snapshots[2].crop(presenter)
    )
    assert stable_content < 4, stable_content
    assert changed_presenter > 8, changed_presenter
    assert restored_presenter < 4, restored_presenter

    modified = json.loads(transcript_path.read_text())
    modified["segments"][0]["words"][0]["text"] = "別の"
    write_json(root / "modified-transcript.json", modified)
    try:
        cut(
            source,
            root / "stacked",
            2,
            8,
            profile,
            root / "modified-transcript.json",
            resume=True,
        )
    except PipelineError:
        pass
    else:
        raise AssertionError("changed transcript reused existing render")
    assert digest(video) == original_video_sha

    blur_profile = profile.model_copy(
        update={
            "framing": "blur-fit",
            "presenter_region": None,
            "content_region": None,
            "punch_ins": [],
        }
    )
    blurred = cut(source, root / "blur-fit", 2, 8, blur_profile, transcript_path)
    blur_image = frame(
        root / "blur-fit/clips/clip-001/video.mp4", root / "blur-fit.png", 0.8
    )
    assert sum(ImageStat.Stat(blur_image.crop((0, 0, 1080, 60))).mean) > 10
    reports = {"stacked": receipt, "blur-fit": blurred["rendered"][0]}
    for mode, aspect in [
        ("fit", "9:16"),
        ("center-crop", "9:16"),
        ("fit", "16:9"),
        ("blur-fit", "1:1"),
    ]:
        label = mode + "-" + aspect.replace(":", "x")
        plain_profile = Settings(
            gemini_model="manual-no-api", framing=mode, aspect=aspect, height=720
        )
        rendered = cut(source, root / label, 2, 3, plain_profile)
        reports[label] = rendered["rendered"][0]
    silent = root / "silent.mp4"
    process(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            source,
            "-an",
            "-c:v",
            "copy",
            silent,
        ]
    )
    rendered = cut(
        silent,
        root / "silent",
        1,
        2,
        Settings(gemini_model="manual-no-api", height=720),
    )
    reports["silent"] = rendered["rendered"][0]
    assert reports["silent"]["media"]["audio_streams"] == 0
    assert digest(source) == source_sha

    sheet = Image.new("RGB", (1440, 710), "#101a2c")
    font = resolve_font("Noto Sans CJK JP").at(21)
    drawer = ImageDraw.Draw(sheet)
    for i, (label, picture) in enumerate(
        zip(
            ["NORMAL / 0.8s", "PUNCH-IN / 2.8s", "RESTORED / 4.8s", "BLUR-FIT"],
            snapshots + [blur_image],
        )
    ):
        sheet.paste(picture.resize((360, 640)), (i * 360, 48))
        drawer.text((i * 360 + 14, 14), label, font=font, fill="white")
    sheet.save(root / "contact-sheet.png")
    write_json(
        root / "review.json",
        {
            "source_kind": "generated-synthetic-fixture",
            "ai_requests": 0,
            "original_streamer_video_reviewed": False,
            "checks": {
                "source_unchanged": True,
                "resume_verified": True,
                "changed_transcript_rejected": True,
                "stable_content_difference": stable_content,
                "presenter_punch_difference": changed_presenter,
                "presenter_restored_difference": restored_presenter,
            },
            "renders": reports,
        },
    )
    print(
        json.dumps(
            {
                "synthetic_renders": len(reports),
                "source_unchanged": True,
                "all_outputs_fully_decoded": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
