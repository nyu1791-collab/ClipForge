#!/usr/bin/env python3
"""Create an experimental CapCut draft from an explicitly chosen clean clip."""

import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import uuid


def command(node, cli, *args):
    result = subprocess.run([node, str(cli), *map(str, args)], check=True,
                            capture_output=True, text=True, timeout=180)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError('CapCut CLI returned an unreadable result') from None
    if isinstance(data, dict) and data.get('ok') is False:
        raise RuntimeError('CapCut CLI reported a draft error')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('clip', type=Path, help='ClipForge clip directory: video.mp4, captions.srt and render.json')
    parser.add_argument('--drafts-dir', type=Path, required=True, help='Explicit CapCut drafts directory')
    parser.add_argument('--cli-js', type=Path, required=True, help='Installed capcut-cli/dist/index.js')
    parser.add_argument('--name', help='New draft name; existing projects are never overwritten')
    args = parser.parse_args()
    node = shutil.which('node')
    if not node:
        parser.error('Node.js is required')
    cli = args.cli_js.resolve()
    package = cli.parent.parent / 'package.json'
    try:
        meta = json.loads(package.read_text(encoding='utf-8'))
        receipt = json.loads((args.clip / 'render.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        parser.error(f'Cannot read CLI package or render receipt: {type(error).__name__}')
    if meta.get('name') != 'capcut-cli' or not cli.is_file():
        parser.error('--cli-js must point to the installed capcut-cli entry point')
    if receipt.get('captions', {}).get('caption_mode') != 'editable':
        parser.error('Use a tiktok-capcut clean export; this clip may already have burned-in captions')
    media = receipt.get('media', {})
    duration, width, height = media.get('duration'), media.get('width'), media.get('height')
    if (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0
            or type(width) is not int or type(height) is not int or width <= 0 or height <= 0):
        parser.error('Render receipt has invalid media dimensions or duration')
    video, srt = (args.clip / 'video.mp4').resolve(), (args.clip / 'captions.srt').resolve()
    if not video.is_file() or not srt.is_file():
        parser.error('video.mp4 and captions.srt are required')
    root = args.drafts_dir.resolve()
    if not root.is_dir():
        parser.error('The explicit drafts directory must already exist')
    name = args.name or 'ClipForge-' + args.clip.name + '-' + uuid.uuid4().hex[:8]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
        parser.error('Draft name must use only letters, numbers, hyphens or underscores')
    project = root / name
    if project.exists():
        parser.error('Draft already exists; choose a new name')
    command(node, cli, 'init', name, '--template', 'auto', '--drafts', root, '--ratio', '9:16')
    try:
        command(node, cli, 'add-video', project, video, '0', str(duration),
                '--width', str(width), '--height', str(height), '--no-probe')
        command(node, cli, 'import-srt', project, srt)
    except (RuntimeError, subprocess.SubprocessError):
        # Preserve the created draft for inspection; never overwrite or retry it.
        print(json.dumps({'status': 'incomplete', 'draft': str(project)}, ensure_ascii=False), flush=True)
        raise
    print(json.dumps({'status': 'created_unverified_in_capcut', 'draft': str(project),
                      'cli_version': meta.get('version'), 'video': str(video),
                      'subtitle_track': 'srt', 'media_must_remain_in_place': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
