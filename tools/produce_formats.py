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
    'long': 'config.landscape-long-preview.example.json',
    'tiktok': 'config.tiktok.example.json',
    'tiktok-capcut': 'config.tiktok-capcut.example.json',
}


def commands(url, output, mode, source_ref, clips=None, capcut=False):
    formats = ('long', 'tiktok') if mode == 'both' else (mode,)
    result = []
    for name in formats:
        destination = output / name
        if (destination / 'production-state.json').exists():
            raise ValueError(f'{name} already has a saved production; resume it instead of submitting again')
        config = CONFIGS['tiktok-capcut'] if name == 'tiktok' and capcut else CONFIGS[name]
        command = [sys.executable, str(Path(__file__).with_name('produce_with_gemini.py')),
                   url, '--output', str(destination), '--config-file', config,
                   '--source-ref', source_ref]
        if clips is not None:
            command.extend(['--clips', str(clips)])
        result.append((name, command))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['long', 'tiktok', 'tiktok-capcut', 'both'], default='tiktok')
    parser.add_argument('--source-ref', help='Pinned ClipForge commit; defaults to current main')
    parser.add_argument('--clips', type=int, choices=range(1, 11))
    parser.add_argument('--capcut-drafts-dir', type=Path, help='Explicit CapCut draft store on this computer')
    parser.add_argument('--capcut-cli-js', type=Path, help='Installed capcut-cli/dist/index.js')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    capcut = args.capcut_drafts_dir is not None or args.capcut_cli_js is not None
    if capcut and (args.capcut_drafts_dir is None or args.capcut_cli_js is None or args.mode == 'long'):
        parser.error('CapCut export needs both CapCut paths and a TikTok mode')
    if capcut:
        if not args.capcut_drafts_dir.is_dir() or not args.capcut_cli_js.is_file() or not shutil.which('node'):
            parser.error('CapCut export needs an existing draft directory, CLI entry point, and Node.js')
        try:
            package = json.loads((args.capcut_cli_js.resolve().parent.parent / 'package.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            parser.error('Cannot read the capcut-cli package metadata')
        if package.get('name') != 'capcut-cli':
            parser.error('The specified CLI is not capcut-cli')
    source_ref = args.source_ref
    if not source_ref:
        source_ref = subprocess.run(
            ['gh', 'api', 'repos/nyu1791-collab/ClipForge/commits/main', '--jq', '.sha'],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    if not re.fullmatch('[a-f0-9]{40}', source_ref):
        parser.error('source-ref must be an exact commit SHA')
    try:
        items = commands(args.url, args.output, args.mode, source_ref, args.clips, capcut)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({'formats': [name for name, _ in items], 'source_ref': source_ref,
                      'outputs': {name: str(args.output / name) for name, _ in items},
                      'separate_model_requests': len(items) > 1}, ensure_ascii=False), flush=True)
    if args.dry_run:
        return 0
    def finish(name, process):
        status = process.wait()
        record = {'production': status, 'capcut': None}
        if status == 0 and capcut and name in ('tiktok', 'tiktok-capcut'):
            clips_dir = args.output / name / 'clips' / 'clips'
            clips = sorted(clips_dir.glob('clip-*'))
            if not clips:
                print(f'No collected clips found for CapCut at {clips_dir}', file=sys.stderr, flush=True)
                record['capcut'] = 1
                return record
            record['capcut'] = 0
            for clip in clips:
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('export_capcut_draft.py')),
                                         str(clip), '--drafts-dir', str(args.capcut_drafts_dir),
                                         '--cli-js', str(args.capcut_cli_js)], check=False)
                if result.returncode:
                    record['capcut'] = result.returncode
                    return record
        return record

    processes = [(name, subprocess.Popen(command)) for name, command in items]
    with ThreadPoolExecutor(max_workers=len(processes)) as pool:
        futures = [(name, pool.submit(finish, name, process)) for name, process in processes]
        statuses = {name: future.result() for name, future in futures}
    print(json.dumps({'statuses': statuses, 'note': 'Resume a saved production instead of resubmitting it.'}), flush=True)
    return 0 if all(row['production'] == 0 and row['capcut'] in (None, 0) for row in statuses.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
