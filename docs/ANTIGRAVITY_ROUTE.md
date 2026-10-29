# Antigravity経路（Gemini 3.8 Flash）と Vertex Transcribe（Gemini 3.5）

Claude Code 以外（Codex など）が同じ制作を再現するための手順。正規の入口は公開ClipForgeのGitHub Actions（`clipforge-stages.yml`）で、制御側のローカル環境で Gemini や GPU を直接呼ばない。

## 1. 呼び出し先

| 工程 | モデル・段階 | 経路 | コード |
|---|---|---|---|
| 全体把握・話題選定 | Gemini 3.8 Flash / HIGH（`topics-N`） | Antigravity SDK（Vertex） | `audio_understanding.Client.generate` → `antigravity_client.generate_text` |
| 清書 | Gemini 3.8 Flash / HIGH（`transcript-*`）。誤字脱字と明らかな誤認識だけを直す。話者は Transcribe のラベルをそのまま使い、効果音は出さない | Antigravity SDK（Vertex） | 同上 |
| 文字起こし | Gemini 3.5 Transcribe | Vertex 直接 `generateContent` | `transcribe_pipeline.transcribe_clip`（試行は `transcribe_trial.transcribe_topic`） |

- Vertex プロジェクトは `project-fdadb4dd-cb77-4270-9cd`、ロケーションは `global`。
- 音声は `audio/ogg` のまま渡す。思考レベルは段階ごとに `GeminiModelOptions.thinking_level` で指定する。
- 3.8 の応答は本文のJSONを `extract_json_object` で読む。構造化スキーマは渡さない（従来と同じ）。
- 失敗は再試行できるもの（429、5xx、タイムアウト、空応答）と、それ以外に分ける。エラーの文面はログに出さない（音声・字幕・認証情報を含み得るため）。

## 2. 認証

- 理解ジョブ（`audio-understanding`）の「Connect the designated Vertex identity」が、ADC（`create_credentials_file: true`）とアクセストークンを作る。Antigravity SDK は ADC を使い、Transcribe は `CLIPFORGE_VERTEX_ACCESS_TOKEN` を使う。
- Vertex の Workload Identity は `refs/heads/main` と `fix/pr18-verified-transfer` からだけ認証を許可する（`tools/setup_vertex_actions.sh`）。
- そのため、ワークフロー定義は `workflow_ref=main`、コードは `source_ref` に指定したコミットから取る。ブランチのコードを試すときも、ワークフロー定義は main のものになる。
- APIキーや別アカウントへの切り替えはしない。認証が拒否されたら止める。

## 3. 起動（GPUを使わない）

`clipforge-stages.yml` を次の入力で起動する。

| 入力 | 値 |
|---|---|
| `audio_stage` | `understanding` |
| `worker` | `cpu` |
| `auto_render` | `false`（`true` だと `CLIPFORGE_STREAMING_RENDER` が有効になり、Colab GPU で描画が始まる） |
| `resume_render` | `false` |
| `result_store` | `release` |
| `config_file` | 試行は `config.landscape-transcribe-trial.example.json`（先頭1話題だけ文字起こし）。本番の設定は `config.landscape-long-preview.example.json`（既定値）を使わず、字幕・描画の方針を確認してから決める。プレーン字幕で GPU/NVENC を使う例は `config.landscape-gpu-plain.example.json`。 |
| `source_url` | 対象URL |
| `source_ref` | 実行するコミットのSHA（`git rev-parse HEAD`） |
| `request_id` / `input_tag` | 新しい32桁の16進数。`input_tag` は `clipforge-input-<request_id>` |
| `target_clips` | `5`（既定の本数） |

- `tools/produce_with_gemini.py` は `auto_render` を true に固定するため、この経路には使わない。使うと描画が走る。
- 投入前に、同じ `request_id` の Run が既にないことを確かめる。失敗した Run を自動で再投入しない。

## 4. 確認

- GitHub 接続: `bash tools/check_github_api.sh`（API の経路と、非公開保存庫の読み取りを確認する）。
- Run の工程: `audio-understanding` ジョブの「Connect the designated Vertex identity」「Process selected topics…」の結論を見る。
- 保存された受領記録 `audio-editor-receipt.json` の各要求の `status` と `error_class`（`retryable` / `non_retryable`）、`transport`（`antigravity_sdk_vertex`）を見る。
- 利用量は SDK が報告しないため、記録は `not_reported_by_antigravity_sdk` となる。推定の金額は書かない。

## 5. 既知の差と未確認

- 用語調査は経路から外した（Antigravity 経由では Google 検索の根拠が返らず、用語が採用されないため）。記録は `terminology-glossary.json` の `skipped` になる。
- Antigravity SDK の既定プロンプトと、清書ルールの system instruction の合成は未確認。実行結果で確かめる。
- 3.8 清書（Antigravity 経由の音声入力と JSON 応答）の実 Run は未確認。次の本番 Run で確かめ、結果をこのファイルの末尾に追記する。
- 並列描画は `tools/dispatch_parallel_render.py` で行う。各クリップを clip_index 指定の Run にし、全部成功したら merge Run で結合する。失敗した部分は自動で再投入せず、同じ引数で明示的に再実行する。
- render-only の 1 Run は 1 Colab runtime で、選んだクリップを順に描画する。
