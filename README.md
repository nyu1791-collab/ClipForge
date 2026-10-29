# ClipForge

配信の音声から話題別の横動画（16:9）を準備し、人が字幕や内容を確認・承認してから描画する制作パイプラインです。販売・公開は配信者の許可がある素材だけに限ります。

**新しい Windows PC の準備は [PC 引き継ぎ](docs/PC_MIGRATION.md)、複数リポジトリの取得は [複数リポジトリの手順](docs/PC_MULTIPLE_REPOSITORIES.md)。** 現在の制作手順は [確認待ち経路](docs/HUMAN_REVIEW_WORKFLOW.md)、接続と再開は [最短手順](docs/QUICK_START.md) から始めます。

## 現在の流れ

| 工程 | 内容 | 場所・経路 |
| --- | --- | --- |
| 取得 | 配信の音声を取得、または保存済み音声を再利用 | GitHub Actions |
| 話題選定 | 録画の長さに応じて1・2・4体が話題と見出しを選ぶ | Gemini 3.8 Flash / HIGH（Antigravity SDK・Vertex） |
| 文字起こし | 採用された話題だけを、必要に応じて窓に分けて文字起こし | Gemini 3.5 Transcribe（Vertex 直接） |
| 清書 | 発言内容と時刻を保ち、明らかな誤りを修正。人が確認する候補も挙げる | Gemini 3.8 Flash / HIGH（Antigravity SDK・Vertex） |
| 確認 | 非公開の確認用動画と字幕・確認候補を回収し、人が確認・必要なら修正 | 非公開 Release・利用者 |
| 描画・回収 | 明示的な承認後に字幕付き動画を描画し、非公開保存・回収 | Colab GPU/NVENC・非公開 Release |

採用話題の文字起こしと清書は最大6話題を並行します。時刻・話者・清書の正確さや字幕の網羅性は保証せず、確認候補以外に誤りがないという意味ではありません。Drive 納品は指定された場合だけ行います。

公開リポジトリはコードと Actions 定義です。動画・音声・字幕・モデル応答は公開 Artifact や公開リポジトリに置かず、権限を持つ非公開保存庫または指定された非公開の納品先で扱います。

## 作業の始め方

- Windows 11 と WSL2 で新しく準備する場合: [PC 引き継ぎ](docs/PC_MIGRATION.md)。Ubuntu 上の基本セットアップは `bash setup.sh`。
- 制作前: URL・本数・設定・実行コードの**40桁 SHA**を利用者が指定しているか確認する。新規制作の指定例と確認後の描画は [確認待ち経路](docs/HUMAN_REVIEW_WORKFLOW.md)。
- 中断した制作の監視・回収: 同じ非公開出力ディレクトリを使い `uv run --no-sync python tools/produce_with_gemini.py --resume --output "$PRIVATE_OUTPUT"`。失敗や結果不明の Run を自動で再投入しない。
- 修正・描画の再利用: [制作と修正](docs/PRODUCTION_AND_REVISION.md)。
- エージェントと開発: [AGENTS.md](AGENTS.md)、[INDEX.md](INDEX.md)、[開発環境](docs/DEVELOPMENT.md)。テストと lint は利用者に依頼されたときだけ実行する。

確認用字幕の上をタップして直すローカル試作がありますが、スマホ向け公開サービス、完成版と同じ ASS 表示、自動描画開始は未完成です。詳細は [非公開確認アプリの設計](docs/PRIVATE_REVIEW_APP_DESIGN.md)。

通常制作の入口は `tools/produce_with_gemini.py`、構成と保存境界は [アーキテクチャ](docs/ARCHITECTURE.md)、共通の字幕ルールは [編集ルールブック](src/clipforge/prompts/gemini_editorial_rulebook_ja.md) です。旧の全編 GPU ASR などは過去成果物の復旧用として残し、新規制作の前提にしません。
