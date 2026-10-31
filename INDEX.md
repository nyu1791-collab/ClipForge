# ClipForge 要約マップ

再開時はまずこのファイルだけを読む。本文はタスクに必要なファイルだけ、`rg -n` で位置を決めて `sed -n` で範囲を読む。テスト・監査は通常制作の前提にしない。

## 構成
- Python 3（uv）。依存は `pyproject.toml`、テストは `uv run --no-sync python -m pytest -q -p no:cacheprovider`、lint は `ruff`。
- 日本語字幕は Noto Sans CJK JP が必須（無いと字幕系テストが失敗する）。Colab 制御のテストは SDK が要る（`tools/dev_setup.sh` が入れる）。
- 規約: `AGENTS.md`（共通）・`CLAUDE.md`（Claude 向け）。運用文書は `docs/`。FFmpegの品質レシピは skill `.claude/skills/ffmpeg-quality/`。
- 開発環境の準備: `make dev`（`tools/dev_setup.sh`）／説明: `docs/DEVELOPMENT.md`。入口は `Makefile`（setup/produce/resume/collect/test/lint）。

## 現在の通常経路

URL → 音声取得・再利用 → 3.8が話題・見出し選定 → 採用区間を切り出し → 3.5が用語表記調査 → Colab GPUが採用部分だけ文字起こし → 3.5 Flash/MEDIUMが文字起こしを話題ごと1回で整文・SEを提案 → Colab GPU/NVENCで最大360p素材・標準720pキャンバス描画 → 非公開保存・回収。追加のモデル時刻照合・監査・strict品質検査は行わない。

- 最短入口・接続・再接続: `docs/QUICK_START.md`。修正・再利用: `docs/PRODUCTION_AND_REVISION.md`。音声経路の詳細: `docs/GEMINI_AUDIO_REVIEW.md`。目安: `docs/FINISHING_TARGETS.md`。過去の経緯は git 履歴を参照。
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
- 字幕: `captions.py`・`caption_breaks.py`・`caption_sentences.py`・`asr_cleanup.py`・`fonts.py`（品質検査モジュールは撤去済み）

## tests
`tests/test_<モジュール名>.py` が基本。共通の fixture は `tests/conftest.py`。

## 読まないもの
`.venv/`・生成物（`outputs/`・`artifacts/`・キャッシュ）・動画・音声・画像は `.claude/settings.json` の `permissions.deny` で Read を禁止している。`uv.lock` は `grep` で該当行だけ見る。
