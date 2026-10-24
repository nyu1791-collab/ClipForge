# ClipForge 要約マップ

再開時はまずこのファイルだけを読む。本文はタスクに必要なファイルだけ、`grep -n` で位置を決めて `sed -n` で範囲を読む。

## 構成
- Python 3（uv）。依存は `pyproject.toml`、テストは `uv run --no-sync python -m pytest -q -p no:cacheprovider`、lint は `ruff`。
- 日本語字幕は Noto Sans CJK JP が必須（無いと字幕系テストが失敗する）。Colab 制御のテストは SDK が要る（`tools/dev_setup.sh` が入れる）。
- 規約: `AGENTS.md`（共通）・`CLAUDE.md`（Claude 向け）。運用文書は `docs/`。
- 開発環境の準備: `bash tools/dev_setup.sh`／説明: `docs/DEVELOPMENT.md`。

## 流れ（唯一の経路）
Kick / YouTube / Twitch / 直接 HTTPS の URL → runner で音声取得（`tools/acquire_audio_url.py`）→ runner で静かな位置で最大4分割 → Colab GPU（L4・T4）で Parakeet → runner で結合・話題分け → 同じ run で候補映像と未承認の下書き → 承認 → Colab CPU で描画 → 制御VMで `merge --local`。音声だけの URL は解析まで。
- 手順: `docs/GPU_ROUTE.md`（S0〜S8）／段階の詳細と承認ファイル: `docs/AUDIO_FIRST.md`／文字起こし: `docs/ASR.md`
- 投入と回収: `tools/submit_job.py`（解析）・`tools/submit_audio_stage.py`（描画・結合）・`tools/run_timings.py RUN_ID`（計測）
- 文字起こし: runner 側 `tools/split_asr_job.py`・`src/clipforge/asr_split.py`、Colab 側 `src/clipforge/asr.py`
- Colab 制御: `tools/colab_actions.py`（割り当て・解放）・`tools/colab_remote_job.py`（Colab 上のジョブ）・`tools/colab_bundle.py`（結果の受け渡し）・`tools/colab_setup.sh`（描画の環境）
- 非公開 worker の workflow 原本: `deploy/*.yml`。一致の確認: `tools/check_worker_workflows.py`
- YouTube の WARP・Cookie: `src/clipforge/youtube_transport.py`・`youtube_auth.py`、`docs/YOUTUBE_TRANSPORT.md`

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
