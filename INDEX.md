# ClipForge 要約マップ

再開時はまずこのファイルだけを読む。本文はタスクに必要なファイルだけ、`grep -n` で位置を決めて `sed -n` で範囲を読む。

## 構成
- Python 3（uv）。依存は `pyproject.toml`、テストは `uv run --no-sync python -m pytest -q -p no:cacheprovider`、lint は `ruff`。
- 日本語字幕は Noto Sans CJK JP が必須（無いと字幕系テストが失敗する）。Colab 制御のテストは SDK が要る（`tools/dev_setup.sh` が入れる）。
- 規約: `AGENTS.md`（共通）・`CLAUDE.md`（Claude 向け）。運用文書は `docs/`。
- 開発環境の準備: `bash tools/dev_setup.sh`／説明: `docs/DEVELOPMENT.md`。

## 現在の通常経路

URL → 音声取得・再利用 → 3.8が音声から話題区分 → 採用区間を切り出し → 3.5が用語表記調査 → 3.8が区間字幕・本文を保持した時刻照合 → Colab GPU/NVENCの360p描画 → 非公開回収。

- 正本: `docs/GEMINI_AUDIO_REVIEW.md`。引き継ぎ: `docs/HANDOFF_2026-10-26.md`。
- 通常入口: `tools/produce_with_gemini.py`。実装: `src/clipforge/audio_understanding.py`。
- Actions: `.github/workflows/clipforge-stages.yml`のunderstandingとselected-audio-render。
- Colab制御: `tools/colab_actions.py`・`tools/colab_remote_job.py`・`tools/colab_bundle.py`。
- 旧全編ASRの手順は復旧用に維持。新規制作に適用しない。

## src/clipforge
- 時刻・設定の型: `models.py`／保存・ハッシュ: `storage.py`
- 取得: `media_sources.py`（振り分けと yt-dlp）・`kick_media.py`（Kick）
- 段階: `audio_first.py`（話題の割り当て・プレビュー・描画・`caption_edits`）・`worker_audio.py`（worker での実行と結合）
- 話題: `topics.py`・`topic_hints.py`・`review_draft.py`・`chat_activity.py`・`audio_volume.py`
- 描画: `pipeline.py`（1クリップ）・`media.py`（ffmpeg）・`auto_layout.py`・`kick_chat.py`（チャット欄）
- 字幕: `captions.py`・`caption_breaks.py`・`caption_sentences.py`・`asr_cleanup.py`・`fonts.py`／品質: `quality.py`

## tests
`tests/test_<モジュール名>.py` が基本。共通の fixture は `tests/conftest.py`。

## 読まないもの
`.venv/`・生成物（`outputs/`・`artifacts/`・キャッシュ）・動画・音声・画像は `.claude/settings.json` の `permissions.deny` で Read を禁止している。`uv.lock` は `grep` で該当行だけ見る。
