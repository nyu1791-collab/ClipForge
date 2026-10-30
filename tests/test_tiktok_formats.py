import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from clipforge import media, pipeline
from clipforge.models import SelectedClip, Settings, Transcript, Segment
from clipforge.worker_audio import supports_audio_stage


def producer():
    spec = importlib.util.spec_from_file_location('produce_formats', Path('tools/produce_formats.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_formats_are_independent_and_refuse_to_resubmit(tmp_path):
    commands = producer().commands('https://example.test/video', tmp_path, 'both', 'a' * 40, 3)
    assert [name for name, _ in commands] == ['long', 'tiktok']
    assert 'config.landscape-long-preview.example.json' in commands[0][1]
    assert 'config.tiktok.example.json' in commands[1][1]
    assert str(tmp_path / 'long') in commands[0][1]
    assert str(tmp_path / 'tiktok') in commands[1][1]
    (tmp_path / 'tiktok').mkdir()
    (tmp_path / 'tiktok' / 'production-state.json').write_text('{}')
    with pytest.raises(ValueError, match='resume'):
        producer().commands('https://example.test/video', tmp_path, 'both', 'a' * 40)


def test_short_config_is_advisory_and_renderable():
    settings = Settings.model_validate_json(Path('config.tiktok.example.json').read_text())
    assert (settings.min_seconds, settings.max_seconds) == (15, 60)
    assert settings.aspect == '9:16' and media.frame_size(settings) == (720, 1280)
    assert settings.quality_gate == 'off'
    assert supports_audio_stage(settings)
    assert supports_audio_stage(Settings(aspect='16:9', height=360))
    assert not supports_audio_stage(Settings(aspect='1:1', height=720))


def test_capcut_mode_selects_clean_video_config(tmp_path):
    commands = producer().commands('https://example.test/video', tmp_path, 'both', 'a' * 40, capcut=True)
    assert 'config.tiktok-capcut.example.json' in commands[1][1]
    settings = Settings.model_validate_json(Path('config.tiktok-capcut.example.json').read_text())
    assert settings.caption == 'editable'


def test_real_vertical_render_with_audio(tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'color=blue:s=320x180:r=10:d=2', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=2', '-shortest', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
    config = json.loads(Path('config.tiktok.example.json').read_text())
    config.update(video_encoder='libx264', encode_preset='superfast', encode_threads=2)
    settings = Settings.model_validate(config)
    clip = SelectedClip(start=0, end=2, candidate_id='one', title='test', reason='test', confidence=1)
    result = media.render(source, tmp_path / 'vertical.mp4', clip, settings)
    assert (result['width'], result['height']) == (720, 1280)
    assert result['audio'] and abs(result['duration'] - 2) < 0.3


def test_editable_output_saves_subtitles_without_burning_them(tmp_path, monkeypatch):
    settings = Settings.model_validate_json(Path('config.tiktok-capcut.example.json').read_text())
    root = tmp_path / 'clips'
    root.mkdir()
    clip = SelectedClip(start=0, end=2, candidate_id='one', title='test', reason='test', confidence=1)
    transcript = Transcript(language='en', segments=[Segment(start=0, end=1, text='hello')])
    passed = []

    def captions(_transcript, _clip, directory, _settings, **kwargs):
        (directory / 'captions.ass').write_text('ass')
        (directory / 'captions.srt').write_text('srt')
        return {'caption_mode': 'editable'}

    def render(_source, destination, _clip, _settings, **kwargs):
        passed.append(kwargs['ass'])
        destination.write_bytes(b'video')
        return {'duration': 2, 'width': 720, 'height': 1280, 'audio': True}

    monkeypatch.setattr(pipeline, 'create_captions', captions)
    monkeypatch.setattr(pipeline.media, 'render', render)
    pipeline.render_clip(tmp_path / 'source.mp4', root, 1, clip, transcript, settings, 'job')
    assert passed == [None]
    assert (root / 'clip-001' / 'captions.srt').is_file()
    receipt = json.loads((root / 'clip-001' / 'render.json').read_text())
    assert receipt['captions']['caption_mode'] == 'editable'


def test_capcut_export_uses_new_project_and_editable_tracks(tmp_path, monkeypatch):
    import sys
    spec = importlib.util.spec_from_file_location('export_capcut_draft', Path('tools/export_capcut_draft.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / 'capcut-cli'
    (package / 'dist').mkdir(parents=True)
    (package / 'package.json').write_text('{"name":"capcut-cli","version":"0.32.0"}')
    cli = package / 'dist' / 'index.js'
    cli.write_text('')
    clip = tmp_path / 'clip-001'
    clip.mkdir()
    (clip / 'render.json').write_text(json.dumps({'captions': {'caption_mode': 'editable'},
                                                   'media': {'duration': 2, 'width': 720, 'height': 1280}}))
    (clip / 'video.mp4').write_bytes(b'video')
    (clip / 'captions.srt').write_text('subtitle')
    drafts = tmp_path / 'drafts'
    drafts.mkdir()
    calls = []
    monkeypatch.setattr(module.shutil, 'which', lambda _: '/node')
    monkeypatch.setattr(module, 'command', lambda node, cli, *args: calls.append(args))
    monkeypatch.setattr(sys, 'argv', ['export_capcut_draft', str(clip), '--drafts-dir', str(drafts),
                                      '--cli-js', str(cli), '--name', 'test-draft'])
    module.main()
    assert [call[0] for call in calls] == ['init', 'add-video', 'import-srt']
    assert '--ratio' in calls[0] and '9:16' in calls[0]
    assert str(clip / 'video.mp4') in str(calls[1])
    assert str(clip / 'captions.srt') in str(calls[2])
