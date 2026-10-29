# ClipForge 要約マップ

最新の確認待ち制作経路は [HUMAN_REVIEW_WORKFLOW.md](docs/HUMAN_REVIEW_WORKFLOW.md)。描画前に利用者の明示的な承認が必要。
確認済みの範囲と未確認事項は [確認待ち経路の点検](docs/REVIEW_ROUTE_AUDIT_2026-10-28.md)。

再開時はまずこのファイルだけを読む。本文はタスクに必要なファイルだけ、`rg -n` で位置を決めて `sed -n` で範囲を読む。テスト・監査は通常制作の前提にしない。

## 構成
- Python 3（uv）。依存は `pyproject.toml`、テストは `uv run --no-sync python -m pytest -q -p no:cacheprovider`、lint は `ruff`。
- 新しい Windows 11 PC（WSL2 / Ubuntu）のセットアップと非公開データの引き継ぎ: `docs/PC_MIGRATION.md`。基本セットアップの入口は `bash setup.sh`。
- 複数のリポジトリを安全に一覧・取得: `docs/PC_MULTIPLE_REPOSITORIES.md` と `tools/prepare_repositories.py`（一覧が既定、取得は `--clone`）。
- 現行の制作と保存境界: `docs/ARCHITECTURE.md`。旧経路・試作の扱いと整理方針: `docs/REPOSITORY_INVENTORY.md`。
- 2026-10-29 の移行準備・テスト確認と未解決事項: `docs/REPOSITORY_VERIFICATION_2026-10-29.md`（その時点の記録）。
- 日本語字幕は Noto Sans CJK JP が必須（無いと字幕系テストが失敗する）。Colab 制御のテストは SDK が要る（`tools/dev_setup.sh` が入れる）。
- 規約: `AGENTS.md`（共通）・`CLAUDE.md`（Claude 向け）。運用文書は `docs/`。
- 開発環境の準備: `bash tools/dev_setup.sh`／説明: `docs/DEVELOPMENT.md`。

## 現在の通常経路

URL → 音声取得・再利用 → 3.8（Antigravity）が話題・見出し選定 → 3.5 Transcribe（Vertex直接）が採用部分だけ文字起こし → 3.8 Flash/HIGHが話題ごとに清書 → 話題別の確認用動画と候補一覧を非公開で回収 → 利用者が確認・承認 → Colab GPU/NVENCで描画 → 非公開保存・回収。追加のモデル時刻照合・監査・strict品質検査は行わない。

- 最短入口・接続・再接続: `docs/QUICK_START.md`。納品前の見直し: `docs/REVIEW_POLICY.md`。スキルの導入方針と候補（Claude Code・Codex）: `docs/SKILLS.md`。Antigravity経路の起動入力と確認: `docs/ANTIGRAVITY_ROUTE.md`。修正・再利用: `docs/PRODUCTION_AND_REVISION.md`。音声経路の詳細: `docs/GEMINI_AUDIO_REVIEW.md`。履歴（現行の規則ではない）: `docs/HANDOFF_*_2026-10-26.md`・`docs/incidents/`・`docs/REVIEW_*.md`・`docs/HEALTH_REVIEW_2026-10-27.md`。
- 通常入口: `tools/produce_with_gemini.py`。実装: `src/clipforge/audio_understanding.py`。
- Actions: `.github/workflows/clipforge-stages.yml`（`audio_stage`: understanding / render / merge）。clip別の並列描画は `tools/dispatch_parallel_render.py`。
- Colab制御: `tools/colab_actions.py`・`tools/colab_remote_job.py`・`tools/colab_bundle.py`。
- 旧全編ASRの手順は復旧用に維持。新規制作に適用しない。

## src/clipforge
- 話題・文字起こし・清書: `audio_understanding.py`（工程本体）・`antigravity_client.py`（Antigravity SDK）・`transcribe_pipeline.py`（Transcribe）・`transcribe_trial.py`（試行）・`gemini_audio_editor.py`（音声の仮切り出し）。プロンプトは `src/clipforge/prompts/`。
- 人による確認: `review_package.py`（確認動画・候補・字幕編集ページ）・`review_local_server.py`（localhost 限定の試作。スマホ向け公開や自動描画は未実装）。
- 時刻・設定の型: `models.py`／保存・ハッシュ: `storage.py`
- 取得: `media_sources.py`（振り分けと yt-dlp）・`kick_media.py`（Kick）
- 段階: `worker_audio.py`（worker での実行と結合）・`editorial_revision.py`（明示修正パッチ）・`release_transfer.py`（Release回収）・`audio_first.py`（旧段階。復旧用）
- 旧の話題分け（現行の話題選定は `audio_understanding.py`）: `topics.py`・`topic_hints.py`・`review_draft.py`・`chat_activity.py`・`audio_volume.py`
- 描画: `pipeline.py`（1クリップ）・`media.py`（ffmpeg）・`auto_layout.py`・`kick_chat.py`（チャット欄）・`streaming_render.py`・`gpu_slots.py`（同時5枠）・`hardware.py`（NVENC）
- 字幕: `captions.py`・`caption_breaks.py`・`caption_sentences.py`・`asr_cleanup.py`・`fonts.py`／品質: `quality.py`

## tools（主要）
- `produce_with_gemini.py`（通常入口）・`collect_production_clip.py`（完成クリップの回収）・`dispatch_parallel_render.py`（clip別の描画Run＋merge）・`run_audio_stage.py`・`colab_actions.py`・`check_github_api.sh`（GitHub API確認）・`check_antigravity_vertex.py`（Antigravity/Vertex確認）。

## tests
`tests/test_<モジュール名>.py` が基本。共通の fixture は `tests/conftest.py`。

## 読まないもの
`.venv/`・生成物（`outputs/`・`artifacts/`・キャッシュ）・動画・音声・画像は `.claude/settings.json` の `permissions.deny` で Read を禁止している。`uv.lock` は `grep` で該当行だけ見る。
