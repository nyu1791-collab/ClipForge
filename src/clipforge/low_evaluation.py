"""Compare HIGH cleanup with the saved trial without drawing a video."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .audio_understanding import Client, clean_selected_clip
from .media_sources import canonical
from .models import SelectedClip
from .storage import PipelineError, digest, fingerprint, write_json


def run(url, output, resume):
    tag = 'clipforge-kick-editorial-40545180185'
    if not resume or os.environ.get('EDITORIAL_RELEASE') != tag:
        raise PipelineError('Expected the original transcription trial')
    parent = json.loads((resume / 'audio-editor-receipt.json').read_text())
    trial = json.loads((resume / 'transcribe-trial.json').read_text())
    if canonical(parent.get('source_url', '')) != canonical(url) or trial.get('status') != 'complete':
        raise PipelineError('Saved trial source or state mismatch')
    clip = SelectedClip.model_validate(trial['clip'])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = Client(output)
    client.record.update(source_url=url, timestamp_source='gemini_transcribe', evaluation='saved_trial_high', parent_editorial_release=tag)
    client.save()
    try:
        with tempfile.TemporaryDirectory(prefix='clipforge-high-comparison-') as temp:
            temp = Path(temp)
            for release, patterns in [(tag, ['transcribe-audio-000.ogg']),
                    ('clipforge-kick-editorial-40546243026', ['transcribe-selected-source.json', 'transcript-1-b739d43ca2.json', 'audio-editor-receipt.json'])]:
                command = ['gh', 'release', 'download', release, '--repo', 'nyu1791-collab/ai-model-workers', '--dir', str(temp / ('parent' if release == tag else 'medium'))]
                for name in patterns:
                    command.extend(['--pattern', name])
                (temp / ('parent' if release == tag else 'medium')).mkdir()
                if subprocess.run(command, capture_output=True, timeout=120).returncode:
                    raise PipelineError('Saved comparison data could not be retrieved')
            audio = temp / 'parent/transcribe-audio-000.ogg'
            medium = temp / 'medium'
            ref = json.loads((medium / 'audio-editor-receipt.json').read_text())
            prior = [r for r in ref.get('requests', []) if r.get('phase','').startswith('transcript-') and r.get('status') == 'received']
            if len(prior) != 1 or prior[0].get('audio_inputs', [{}])[0].get('sha256') != digest(audio):
                raise PipelineError('Saved medium response does not match trial audio')
            raw = json.loads((medium / 'transcribe-selected-source.json').read_text())
            previous = json.loads((medium / 'transcript-1-b739d43ca2.json').read_text())
            key = fingerprint({'parent': tag, 'sha256': digest(audio)})
            result = clean_selected_clip(client, raw, 1, clip, [], audio, key)
            high = [r for r in client.record.get('requests', []) if r.get('phase', '').startswith('transcript-') and r.get('status') == 'received']
            if len(high) != 1 or high[0].get('prompt_sha256') != prior[0].get('prompt_sha256'):
                raise PipelineError('HIGH and MEDIUM cleanup prompts differ')
            write_json(output / 'high-selected-result.json', result)
            write_json(output / 'medium-model-response.json', previous)
            write_json(output / 'comparison-summary.json', {'source_segments': len(raw['segments']), 'medium_segments': len(previous.get('segments', [])), 'high_segments': len(result.get('segments', [])), 'same_prompt_sha256': high[0]['prompt_sha256'], 'same_audio_sha256': digest(audio), 'human_listening': False})
        client.record['status'] = 'complete'
    except Exception:
        client.record['status'] = 'failed'
        raise
    finally:
        client.record['finished_at'] = time.time()
        client.save()
