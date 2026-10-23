# ClipForge 要約マップ

再開時はまずこのファイルだけを読む。本文はタスクに必要なファイルだけ、`grep -n` で位置を決めて `sed -n` で範囲を読む。

## 構成
- Python 3（uv）。依存は `pyproject.toml`、テストは `uv run --no-sync python -m pytest -q`、lint は `ruff`。
- 日本語字幕は Noto Sans CJK JP が必須（無いと字幕系テストが失敗する）。
- 規約: `AGENTS.md`（共通）・`CLAUDE.md`（Claude 向け）。運用文書は `docs/`。

## 主な流れ（URL 経路）
Kick URL → runner で音声取得（`tools/acquire_audio_url.py`）→ Colab GPU で ASR・話題分割 → previews と未承認の下書き → 承認 → Colab CPU で描画 → 制御 VM で `merge --local`。
- 投入と回収: `tools/submit_job.py`（`--auto-previews`）、段階ごとの依頼: `tools/submit_audio_stage.py`
- Colab 制御: `tools/colab_actions.py`・`tools/colab_remote_job.py`・`tools/colab_bundle.py`・`tools/colab_cache.py`
- 非公開 worker の workflow 原本: `deploy/*.yml`（実行先は非公開リポジトリ）

## src/clipforge
- 時刻・設定の型: `models.py`／外部コマンド・保存・キャッシュ: `storage.py`
- 描画（ffmpeg グラフ）: `media.py`／工程の管理: `pipeline.py`／URL 経路の段階: `audio_first.py`・`worker_audio.py`
- チャット欄（Kick チャットの取得と ASS 描画）: `kick_chat.py`（設定 `chat_panel`、説明は `docs/AUDIO_FIRST.md` の「チャット欄」）
- 音声・信号: `signals.py`・`kick_media.py`・`silence.py`／話題: `topics.py`・`topic_hints.py`・`review_draft.py`
- 字幕: `captions.py`・`caption_breaks.py`・`caption_sentences.py`・`asr_cleanup.py`・`fonts.py`
- 品質検査: `quality.py`／ローカル動画の Gemini 選定: `selection.py`
- 図表・素材・サムネイル: `diagrams.py`・`tables.py`・`assets.py`・`thumbnail.py`

## tests
`tests/test_<モジュール名>.py` が基本。共通の fixture は `tests/conftest.py`。

## 読まないもの
`.venv/`・生成物（`outputs/`・`artifacts/`・キャッシュ）・動画・音声・画像は `.claude/settings.json` の `permissions.deny` で Read を禁止している。`uv.lock` は `grep` で該当行だけ見る。
