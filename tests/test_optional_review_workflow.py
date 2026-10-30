import json
from pathlib import Path


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
