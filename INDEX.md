# ClipForge 要約マップ

入口と関連ファイルをここで選び、必要な本文だけ読む。共通ルールは [AGENTS.md](AGENTS.md)、検証は [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。過去の記録は [docs/archive/](docs/archive/README.md) に分け、通常の必読対象にしない。

## 主な経路

URL → runnerで音声取得 → Colab GPUで2分割ASR → 話題分割 → プレビュー・未承認の下書き → 映像確認・承認 → Colab CPUで描画 → 制御VMで結合。音声のみのURLは解析まで。ローカル動画の自動選定だけGeminiを使う。

| 担当 | 実装 |
| --- | --- |
| 投入・状態確認・回収 | `tools/submit_job.py` |
| 後段の投入・回収・ローカル結合 | `tools/submit_audio_stage.py` |
| URL検証・取得 | `media_sources.py`・`kick_media.py`・`tools/acquire_audio_url.py` |
| ASR・2分割 | `signals.py`・`asr_split.py`・`tools/split_asr_job.py` |
| 話題・下書き | `topics.py`・`topic_hints.py`・`review_draft.py` |
| 段階の処理・再利用・描画 | `audio_first.py`・`worker_audio.py`・`pipeline.py` |
| 設定と共通の承認検査 | `models.py`・`review.py` |
| 保存・成果物一覧・回収先・外部コマンド | `storage.py`・`github_cli.py` |
| 字幕・文節・文末・フォント | `captions.py`・`caption_breaks.py`・`caption_sentences.py`・`asr_cleanup.py`・`fonts.py` |
| FFmpeg・画面配置・音量・品質 | `media.py`・`auto_layout.py`・`quality.py`・`landscape.py` |
| チャット | `kick_chat.py`・`chat_activity.py` |
| 素材・図表・サムネイル | `assets.py`・`diagrams.py`・`tables.py`・`thumbnail.py` |
| Colab制御・転送・キャッシュ | `tools/colab_actions.py`・`tools/colab_remote_job.py`・`tools/colab_bundle.py`・`tools/colab_cache.py` |

表のファイル名だけのPythonモジュールは `src/clipforge/`。実行用の薄いラッパーは、既存CLI・workflowとの互換性のため `tools/` に残す。非公開workerのworkflow原本は `deploy/`、公開側の検査は `.github/workflows/`。依存は `pyproject.toml` と `uv.lock`、テストは `tests/`。

## 文書

[処理構成](docs/ARCHITECTURE.md)・[URLの手順](docs/AUDIO_FIRST.md)・[2分割ASR](docs/ASR_SPLIT.md)・[Colab](docs/COLAB.md)・[運用](docs/OPERATIONS.md)・[品質規則](docs/QUALITY_DECISIONS.md)・[受入条件](docs/LANDSCAPE_PLAN.md)・[YouTube取得](docs/YOUTUBE_TRANSPORT.md)。

ロックファイル・ログは検索して必要な範囲だけ確認する。生成物は依頼された確認に必要なものだけを扱い、通常のコード探索で私有動画・ASR・認証情報を読み込まない。
