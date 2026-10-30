import json
from pathlib import Path

import pytest


def test_automatic_render_does_not_require_review_package():
    source = Path('.github/workflows/clipforge-stages.yml').read_text()
    assert source == Path('deploy/audio-stages.yml').read_text()
    workflow = json.loads(source)
    steps = workflow['jobs']['audio-understanding']['steps']
    assert not any(s.get('name') == 'Require human review before rendering' for s in steps)
    assert not any(s.get('name') == 'Prepare private topic videos and review checklist' for s in steps)
    for job in ('audio-stage', 'colab-render'):
        assert not any(s.get('name') == 'Require explicit review approval for rendering' for s in workflow['jobs'][job]['steps'])


def test_supplied_editorial_hashes_remain_checked():
    workflow = json.loads(Path('deploy/audio-stages.yml').read_text())
    steps = workflow['jobs']['colab-render']['steps']
    code = next(s['run'] for s in steps if s.get('name') == 'Validate the clip render request')
    assert 'if expected:' in code
    assert 'hashlib.sha256(data).hexdigest()==expected' in code


def test_resume_downloads_private_editorial_without_missing_helper():
    workflow = json.loads(Path('deploy/audio-stages.yml').read_text())
    for job, name in (
        ('audio-understanding', 'Restore saved editorial responses without resubmitting them'),
        ('colab-render', 'Load saved selected-only subtitles for render-only continuation'),
    ):
        step = next(s for s in workflow['jobs'][job]['steps'] if s.get('name') == name)
        assert 'gh release download' in step['run']
        assert 'tools/download_editorial.py' not in step['run']
    assert 'selected-transcripts.json' in next(s['run'] for s in workflow['jobs']['colab-render']['steps']
                                                  if s.get('name') == 'Load saved selected-only subtitles for render-only continuation')


def test_render_request_accepts_no_review_hash_and_checks_a_supplied_hash(tmp_path, monkeypatch):
    workflow = json.loads(Path('deploy/audio-stages.yml').read_text())
    steps = workflow['jobs']['colab-render']['steps']
    run = next(s['run'] for s in steps if s.get('name') == 'Validate the clip render request')
    code = run.split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    for key, value in {
        'AUDIO_STAGE': 'render', 'CLIPFORGE_SOURCE_REF': 'a' * 40,
        'REQUEST_ID': 'b' * 32, 'PREVIOUS_REQUEST': 'c' * 32,
        'INPUT_TAG': 'clipforge-input-' + 'b' * 32, 'PREVIOUS_RUN': '1',
        'CLIP_INDEX': '1', 'CONFIG_FILE': 'config.landscape.example.json',
        'RUNNER_TEMP': str(tmp_path), 'REVIEW_JSON': '{"clips": []}',
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('CLIPFORGE_AUDIO_REVIEW_RELEASE', raising=False)
    monkeypatch.delenv('CLIPFORGE_APPROVED_SELECTION_SHA256', raising=False)
    monkeypatch.delenv('CLIPFORGE_APPROVED_TRANSCRIPTS_SHA256', raising=False)
    exec(compile(code, 'validate-render', 'exec'), {})
    editorial = tmp_path / 'editorial-render'
    editorial.mkdir()
    (editorial / 'final-selection.json').write_text('{}')
    monkeypatch.setenv('CLIPFORGE_APPROVED_SELECTION_SHA256', '0' * 64)
    with pytest.raises(AssertionError):
        exec(compile(code, 'validate-render', 'exec'), {})
