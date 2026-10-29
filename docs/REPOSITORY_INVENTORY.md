# リポジトリの棚卸しと整理方針

移行時にコードを消すのではなく、まず現行の入口、復旧用の経路、試作、過去の記録を区別する。どのファイルも実行状況・依存先・過去成果物との関係を調べずに一括削除しない。この文書は静的にファイルと入口を読んだ整理であり、全機能の実動確認ではない。

| 区分 | 主な場所 | 役割・扱い |
| --- | --- | --- |
| 現行の規則と入口 | `AGENTS.md`、`INDEX.md`、`docs/HUMAN_REVIEW_WORKFLOW.md`、`docs/QUICK_START.md` | 新しい作業・制作・再開の前に参照 |
| 制作の入口 | `tools/produce_with_gemini.py`、`.github/workflows/clipforge-stages.yml`、`deploy/audio-stages.yml` | コード SHA、実行ワークフロー、非公開保存、承認の境界を維持 |
| 現行の音声経路 | `src/clipforge/audio_understanding.py`、`antigravity_client.py`、`transcribe_pipeline.py`、`prompts/` | 話題選定・採用部分の文字起こし・清書。プロンプトの変更はキャッシュに影響 |
| 人の確認と修正 | `review_package.py`、`editorial_revision.py`、`docs/HUMAN_REVIEW_WORKFLOW.md` | 確認パッケージ、元データ照合、修正パッチ |
| 確認 UI の試作 | `review_local_server.py`、`docs/PRIVATE_REVIEW_APP_DESIGN.md` | localhost 限定。公開ホスティング、ASS 完全再現、描画開始は未実装 |
| 描画・回収 | `pipeline.py`、`captions.py`、`streaming_render.py`、`gpu_slots.py`、`release_transfer.py`、`tools/collect_production_clip.py` | 人の承認後の描画・非公開保存・回収 |
| 初回セットアップ | `setup.sh`、`tools/dev_setup.sh`、`pyproject.toml`、`uv.lock` | 通常用と任意の開発用。OS・依存の導入は制作の実行を意味しない |
| 旧経路・復旧用 | `src/clipforge/asr.py`、`asr_split.py`、`audio_first.py`、`topics.py`、`tools/selected_gpu_asr.py`、`tools/split_asr_job.py` など | 以前の制作や復旧の参照先になり得る。新規制作に自動適用しない |
| 履歴・事後記録 | `docs/HANDOFF_*`、`docs/incidents/`、日付付き `REVIEW_*` や `*_AUDIT_*` | 作成時の経路・確認範囲を記録。現在の仕様と混同しない |
| テスト・任意ツール | `tests/`、`tools/` の調査・診断用スクリプト、`.claude/skills/` | 存在だけで常用・実行しない。テストと lint は利用者からの依頼がある場合だけ |

`.github/workflows/` の Actions や `deploy/` の定義は GitHub 上の実行や過去の復旧に関係する場合がある。現在使っていないように見えても、各入口と利用実績を確認するまでは削除しない。特に `clipforge-stages.yml` は `deploy/audio-stages.yml` と揃える。

`.venv/`、キャッシュ、生成物、認証情報は Git 管理の対象外。公開リポジトリへ動画・音声・字幕・モデル応答・秘密情報を含めない。新しい PC へのコピーでは、Git で管理されるコードとは別に、引き継ぎが必要な非公開データの保存場所・権限・完全性を確認する。
