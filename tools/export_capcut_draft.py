#!/usr/bin/env python3
"""Create an experimental CapCut draft from an explicitly chosen clean clip.

Tracks: the video, one text segment per SRT cue on track "subtitle", and when known the
heading on its own text track "heading" for the whole clip.

Heading: --heading, else render.json selection.title, else heading.txt in the clip folder,
else a neighbouring final-selection.json. Unknown means no heading track, never a failure.

Style: a last, isolated step (--no-style skips it). It edits only keys seen in a draft saved
by CapCut 9.7, keeps *.prestyle.bak, and leaves the unstyled draft in place if it fails.
Whether CapCut draws the outline from those keys can only be seen by opening the draft.

Media: capcut-cli copies the video into <draft>/assets/video and the draft references that
copy by absolute path. The source clip may be moved afterwards; the draft folder may not
(use `capcut rename`, which keeps the paths, not a manual move).

An existing draft is never overwritten. A failed export keeps the partial folder, reports
status "incomplete" with a non-zero exit, and prints a retry command that uses a new name.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


TESTED_CLI_VERSION = "0.32.0"
CAPTION_TRACK = "subtitle"
HEADING_TRACK = "heading"
HEADING_COLOR = "#FFD228"  # clipforge.captions.YELLOW
FONT_SIZE = 15  # capcut-cli's default, the size seen in the good drafts
TIMELINE_FILES = ("draft_content.json", "draft_info.json", "template-2.tmp")
ASPECTS = {"9:16": 9 / 16, "16:9": 16 / 9}
ASPECT_TOLERANCE = 0.015
CANVAS = {"9:16": (1080, 1920), "16:9": (1920, 1080)}
# clip.transform.y: centre 0, up positive, half a canvas per unit (capcut-cli render.js).
CAPTION_Y = {"9:16": -0.45, "16:9": -0.68}
HEADING_Y = {"9:16": 0.72, "16:9": 0.8}
MAX_PATH_CHARS = 240
TIMESTAMP = re.compile(
    r"^(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)


def command(node, cli, *args):
    try:
        result = subprocess.run(
            [node, str(cli), *map(str, args)],
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()[-600:]
        if "refused [editor-open]" in detail:
            raise RuntimeError(running_message()) from None
        raise RuntimeError(f"capcut-cli {args[0]} failed: {detail}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"capcut-cli {args[0]} timed out") from None
    except OSError as error:
        raise RuntimeError(
            f"Cannot run capcut-cli: {type(error).__name__}: {error}"
        ) from None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("CapCut CLI returned an unreadable result") from None
    if isinstance(data, dict) and data.get("ok") is False:
        raise RuntimeError("CapCut CLI reported a draft error")
    return data


def running_message():
    return (
        "CapCut is running, so the draft store must not be written. Close CapCut. If no window "
        "is left, a background CapCut.exe remains: end it in Task Manager, then retry."
    )


def capcut_running():
    if os.name != "nt":
        return False
    try:
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        line.strip().lower().startswith('"capcut.exe"') for line in listing.splitlines()
    )


def is_managed(path):
    return "/com.lveditor.draft/" in str(path).replace("\\", "/").lower()


def aspect_for(width, height):
    value = width / height
    for label, target in ASPECTS.items():
        if abs(value - target) / target <= ASPECT_TOLERANCE:
            return label
    return None


def decode_srt(raw):
    """Return UTF-8 ready text and the source encoding, or raise ValueError."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16"
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(
        "captions.srt is not UTF-8, UTF-16 or Shift-JIS; convert it to UTF-8"
    )


def parse_srt(text):
    """Parse like capcut-cli's srt.js so bad input fails before a draft exists."""
    cues, lines, i = [], text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 0
    while i < len(lines):
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        if re.fullmatch(r"\d+", lines[i].strip()):
            i += 1
        if i >= len(lines):
            break
        match = TIMESTAMP.match(lines[i])
        if not match:
            raise ValueError(f"captions.srt has a bad timestamp at line {i + 1}")
        parts = [int(value) for value in match.groups()]
        start = parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000
        end = parts[4] * 3600 + parts[5] * 60 + parts[6] + parts[7] / 1000
        if end <= start:
            raise ValueError(f"captions.srt line {i + 1}: cue end is not after start")
        timing, i = lines[i], i + 1
        body = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i])
            i += 1
        cues.append((timing, "\n".join(body)))
    return cues


def prepare_srt(path, scratch):
    """Return (path for import-srt, cue count, warnings). Rewrites only when needed."""
    text, encoding = decode_srt(path.read_bytes())
    cues = parse_srt(text)
    kept = [cue for cue in cues if cue[1].strip()]
    if not kept:
        raise ValueError("captions.srt has no cues with text")
    warnings = []
    if len(kept) != len(cues):
        warnings.append(f"Skipped {len(cues) - len(kept)} empty caption cue(s)")
    if encoding == "utf-8-sig" and len(kept) == len(cues):
        return path, len(kept), warnings  # capcut-cli strips a BOM itself
    if encoding != "utf-8-sig":
        warnings.append(f"Read captions.srt as {encoding}")
    normal = Path(scratch) / "captions.srt"
    normal.write_text(
        "\n\n".join(
            f"{n}\n{timing}\n{body}" for n, (timing, body) in enumerate(kept, 1)
        )
        + "\n",
        encoding="utf-8",
    )
    return normal, len(kept), warnings


def clean_heading(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def selection_title(document, number, candidate):
    rows = document.get("clips") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return ""
    if candidate:
        for row in rows:
            if isinstance(row, dict) and row.get("candidate_id") == candidate:
                return clean_heading(row.get("title"))
    if number is not None and 1 <= number <= len(rows):
        row = rows[number - 1]
        return clean_heading(row.get("title")) if isinstance(row, dict) else ""
    return ""


def find_heading(clip, receipt):
    selection = receipt.get("selection")
    selection = selection if isinstance(selection, dict) else {}
    title = clean_heading(selection.get("title"))
    if title:
        return title
    try:
        title = clean_heading((clip / "heading.txt").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        title = ""
    if title:
        return title
    found = re.match(r"clip-(\d+)", clip.name)
    number = int(found.group(1)) if found else None
    for folder in [clip.resolve(), *list(clip.resolve().parents)[:3]]:
        for candidate in (
            folder / "final-selection.json",
            folder / "audio-review" / "final-selection.json",
        ):
            try:
                document = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            title = selection_title(document, number, selection.get("candidate_id"))
            if title:
                return title
    return ""


def style_document(document, aspect):
    """Change only keys present in a CapCut-saved text material and segment."""
    materials = {item["id"]: item for item in document["materials"]["texts"]}
    styled = 0
    for track in document["tracks"]:
        if track.get("type") != "text" or track.get("name") not in (
            CAPTION_TRACK,
            HEADING_TRACK,
        ):
            continue
        for segment in track["segments"]:
            material = materials[segment["material_id"]]
            material.update(border_color="#000000", border_width=0.08, border_alpha=1.0)
            content = json.loads(material["content"])
            for style in content["styles"]:
                style["bold"] = True
            material["content"] = json.dumps(
                content, ensure_ascii=False, separators=(",", ":")
            )
            if track["name"] == CAPTION_TRACK:
                segment["clip"]["transform"]["y"] = CAPTION_Y[aspect]
            styled += 1
    if not styled:
        raise ValueError("no text segments to style")
    return styled


def apply_style(project, aspect):
    """All timeline mirrors or none: any failure restores the unstyled files."""
    prepared = []
    for name in TIMELINE_FILES:
        path = project / name
        if path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            styled = style_document(document, aspect)
            prepared.append(
                (
                    path,
                    json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                )
            )
    if not prepared:
        raise ValueError("no readable timeline files")
    done = []
    try:
        for path, text in prepared:
            backup = path.with_name(path.name + ".prestyle.bak")
            shutil.copy2(path, backup)
            done.append((path, backup))
            temporary = path.with_name(path.name + ".style-tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
    except OSError:
        for path, backup in done:
            shutil.copy2(backup, path)
        raise
    finally:
        for path, _ in prepared:
            path.with_name(path.name + ".style-tmp").unlink(missing_ok=True)
    return styled


def same_path(first, second):
    return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(
        os.path.normpath(str(second))
    )


def count_segments(document):
    counts = {"video": 0, "captions": 0, "heading": 0}
    for track in document.get("tracks", []):
        size = len(track.get("segments", []))
        if track.get("type") == "video":
            counts["video"] += size
        elif track.get("type") == "text":
            key = "heading" if track.get("name") == HEADING_TRACK else "captions"
            counts[key] += size
    return counts


def video_listed(project, path):
    try:
        meta = json.loads(
            (project / "draft_meta_info.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    for group in meta.get("draft_materials", []):
        for entry in group.get("value", []) if isinstance(group, dict) else []:
            listed = entry.get("file_Path") if isinstance(entry, dict) else None
            if listed and same_path(project / listed, path):
                return True
    return False


def root_meta_entries(root):
    document = json.loads((root / "root_meta_info.json").read_text(encoding="utf-8"))
    entries = document.get("all_draft_store")
    if not isinstance(entries, list):
        raise ValueError("all_draft_store is missing")
    return entries


def verify_draft(project, root, aspect, cue_count, source, expect_heading):
    """Count what the draft holds. Returns (facts, problems, warnings)."""
    problems, warnings, facts = [], [], {}
    documents = {}
    for name in TIMELINE_FILES:
        path = project / name
        if path.is_file():
            try:
                documents[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                problems.append(f"{name} is unreadable: {type(error).__name__}")
    main = documents.get("draft_content.json")
    if main is None:
        problems.append("draft_content.json is missing or unreadable")
        return facts, problems, warnings
    counts = count_segments(main)
    facts.update(
        video_segments=counts["video"],
        caption_segments=counts["captions"],
        srt_cues=cue_count,
        heading_segments=counts["heading"],
    )
    if counts["video"] != 1:
        problems.append(f"expected 1 video segment, found {counts['video']}")
    if counts["captions"] != cue_count:
        problems.append(
            f"captions.srt has {cue_count} cues but the draft has {counts['captions']} caption segments"
        )
    if expect_heading and counts["heading"] != 1:
        warnings.append("The heading segment is missing from the draft")
    for name, document in documents.items():
        if count_segments(document) != counts:
            problems.append(f"{name} disagrees with draft_content.json")
    videos = main.get("materials", {}).get("videos", [])
    path = Path(videos[0].get("path", "")) if videos else None
    facts["draft_video"] = str(path) if path else None
    exists = bool(path) and path.is_file()
    facts["video_exists"] = exists
    if not exists:
        problems.append(f"the draft's video file does not exist: {path}")
    elif path.stat().st_size != source.stat().st_size:
        problems.append("the draft's video file differs in size from the source")
    listed = exists and video_listed(project, path)
    facts["video_listed"] = listed
    if not listed:
        problems.append(
            "the video is not listed in draft_meta_info.json draft_materials"
        )
    canvas = main.get("canvas_config", {})
    facts["canvas"] = [canvas.get("width"), canvas.get("height")]
    if tuple(facts["canvas"]) != CANVAS[aspect]:
        problems.append(f"canvas is {facts['canvas']}, expected {list(CANVAS[aspect])}")
    try:
        registered = any(
            same_path(entry.get("draft_fold_path", ""), project)
            for entry in root_meta_entries(root)
        )
    except (OSError, ValueError) as error:
        registered = False
        problems.append(f"root_meta_info.json is unreadable: {type(error).__name__}")
    else:
        if not registered:
            problems.append(
                "root_meta_info.json does not list the draft, so CapCut will not show it; "
                "repair with: capcut register <draft> --apply"
            )
    facts["root_meta_listed"] = registered
    return facts, problems, warnings


def retry_command(args):
    parts = [
        "python",
        str(Path(__file__).resolve()),
        str(args.clip),
        "--drafts-dir",
        str(args.drafts_dir),
        "--cli-js",
        str(args.cli_js),
    ]
    if args.heading:
        parts += ["--heading", args.heading]
    if args.no_heading:
        parts.append("--no-heading")
    if args.no_style:
        parts.append("--no-style")
    return subprocess.list2cmdline(parts)


def report_incomplete(project, step, error, args):
    print(
        json.dumps(
            {
                "status": "incomplete",
                "draft": str(project),
                "failed_step": step,
                "error": str(error),
                "retry": retry_command(args),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    print(f"CapCut draft failed at {step}: {error}", file=sys.stderr)
    if project.exists():
        print(
            f"The partial draft was kept and is not usable: {project}\n"
            "Delete it in CapCut, or remove the folder with CapCut closed.",
            file=sys.stderr,
        )
    print(f"Retry with a new draft name: {retry_command(args)}", file=sys.stderr)
    return 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "clip",
        type=Path,
        help="ClipForge clip directory: video.mp4, captions.srt and render.json",
    )
    parser.add_argument(
        "--drafts-dir",
        type=Path,
        required=True,
        help="Explicit CapCut drafts directory",
    )
    parser.add_argument(
        "--cli-js", type=Path, required=True, help="Installed capcut-cli/dist/index.js"
    )
    parser.add_argument(
        "--name", help="New draft name; existing projects are never overwritten"
    )
    parser.add_argument(
        "--heading", help="Heading text; by default read from the clip's selection"
    )
    parser.add_argument(
        "--no-heading", action="store_true", help="Do not add a heading track"
    )
    parser.add_argument(
        "--no-style",
        action="store_true",
        help="Skip the isolated style step; captions keep capcut-cli's plain look",
    )
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        parser.error("Node.js is required: install it and make sure node is on PATH")
    cli = args.cli_js.resolve()
    package = cli.parent.parent / "package.json"
    if not cli.is_file():
        parser.error(f"capcut-cli entry point not found: {cli}")
    try:
        meta = json.loads(package.read_text(encoding="utf-8"))
        receipt = json.loads((args.clip / "render.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        parser.error(
            f"Cannot read CLI package or render receipt: {type(error).__name__}"
        )
    if meta.get("name") != "capcut-cli":
        parser.error("--cli-js must point to the installed capcut-cli entry point")
    mode = (receipt.get("captions") or {}).get("caption_mode")
    if mode != "editable":
        parser.error(
            f"This clip has burned-in captions (caption_mode={mode}); editing them in CapCut "
            "would show them twice. Use a tiktok-capcut clean export"
        )
    media = receipt.get("media", {})
    duration, width, height = (
        media.get("duration"),
        media.get("width"),
        media.get("height"),
    )
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
        or type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
    ):
        parser.error("Render receipt has invalid media dimensions or duration")
    aspect = aspect_for(width, height)
    if not aspect:
        parser.error(
            f"Media is {width}x{height}; only 9:16 and 16:9 clips are supported"
        )
    video, srt = (
        (args.clip / "video.mp4").resolve(),
        (args.clip / "captions.srt").resolve(),
    )
    for path in (video, srt):
        if len(str(path)) >= 260:
            parser.error(f"Path is too long for Windows, move the clip: {path}")
    if not video.is_file() or not srt.is_file():
        parser.error("video.mp4 and captions.srt are required")
    root = args.drafts_dir.resolve()
    if not root.is_dir():
        parser.error(
            f"The drafts directory does not exist: {root}. Open CapCut once and create a project"
        )
    if args.name:
        name = args.name
    else:
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", args.clip.resolve().name).strip("-")[:60]
        name = f"ClipForge-{stem or 'clip'}-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        parser.error(
            "Draft name must use only letters, numbers, hyphens or underscores"
        )
    project = root / name
    if project.exists():
        parser.error(f"Draft already exists; choose a new name: {project}")
    if (root / "root_meta_info.json").is_file():
        try:
            entries = root_meta_entries(root)
        except (OSError, ValueError) as error:
            parser.error(
                f"root_meta_info.json in the drafts directory is unreadable: {type(error).__name__}"
            )
        if any(
            isinstance(entry, dict) and entry.get("draft_name") == name
            for entry in entries
        ):
            parser.error(
                f"CapCut already lists a draft named {name}; choose a new name"
            )
    if len(str(project / "assets" / "video" / video.name)) > MAX_PATH_CHARS:
        parser.error(f"The draft path would be too long for Windows: {project}")
    if is_managed(project) and capcut_running():
        parser.error(running_message())

    warnings = []
    if meta.get("version") != TESTED_CLI_VERSION:
        warnings.append(
            f"capcut-cli {meta.get('version')} differs from tested {TESTED_CLI_VERSION}"
        )
    if args.no_heading:
        heading = ""
    else:
        heading = clean_heading(args.heading) or find_heading(args.clip, receipt)
    with tempfile.TemporaryDirectory(prefix="capcut-srt-") as scratch:
        try:
            import_path, cue_count, srt_warnings = prepare_srt(srt, scratch)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        warnings += srt_warnings
        step = "init"
        try:
            command(
                node,
                cli,
                "init",
                name,
                "--template",
                "auto",
                "--drafts",
                root,
                "--ratio",
                aspect,
            )
            step = "add-video"
            command(
                node,
                cli,
                "add-video",
                project,
                video,
                "0",
                str(duration),
                "--width",
                str(width),
                "--height",
                str(height),
                "--no-probe",
            )
            step = "import-srt"
            command(node, cli, "import-srt", project, import_path)
        except (RuntimeError, subprocess.SubprocessError) as error:
            # Preserve the created draft for inspection; never overwrite or retry it.
            return report_incomplete(project, step, error, args)

    heading_state = "absent"
    if heading and heading.startswith("-"):
        warnings.append(
            "Heading starts with '-', which capcut-cli reads as an option; skipped"
        )
    elif heading:
        try:
            command(
                node,
                cli,
                "add-text",
                project,
                "0",
                str(duration),
                heading,
                "--track-name",
                HEADING_TRACK,
                "--font-size",
                str(FONT_SIZE),
                "--color",
                HEADING_COLOR,
                "--align",
                "1",
                "--y",
                str(HEADING_Y[aspect]),
            )
            heading_state = "added"
        except (RuntimeError, subprocess.SubprocessError) as error:
            heading_state = "failed"
            warnings.append(f"Heading was not added: {error}")

    style_state = "skipped"
    if not args.no_style:
        try:
            apply_style(project, aspect)
            style_state = "applied_unverified_in_capcut"
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            style_state = "failed"
            warnings.append(
                f"Style step failed, the unstyled draft remains: {type(error).__name__}: {error}"
            )

    facts, problems, verify_warnings = verify_draft(
        project, root, aspect, cue_count, video, heading_state == "added"
    )
    warnings += verify_warnings
    if problems:
        return report_incomplete(project, "verify", "; ".join(problems), args)
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(
        json.dumps(
            {
                "status": "created_unverified_in_capcut",
                "draft": str(project),
                "cli_version": meta.get("version"),
                "video": str(video),
                "subtitle_track": "srt",
                "media_must_remain_in_place": True,
                "draft_video": facts["draft_video"],
                "source_video_required": False,
                "aspect": aspect,
                "heading": heading if heading_state == "added" else None,
                "heading_state": heading_state,
                "style": style_state,
                "verification": facts,
                "warnings": warnings,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
