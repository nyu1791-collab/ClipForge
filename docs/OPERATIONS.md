# 回収・再開・後始末・権限（現行）

通常の入口は `tools/produce_with_gemini.py`。接続・制作・再接続の手順は [QUICK_START.md](QUICK_START.md)、修正・完成クリップの再利用・納品だけの操作は [PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)。

## 実行先と保存

- 実行先は公開 ClipForge（コードと Actions）。制作データは非公開 `nyu1791-collab/ai-model-workers` の Release に保存する。
- 公開 Artifacts へ音声・動画・字幕・モデル応答を出さない。
- workflow は `workflow_dispatch` で起動する。通常の push や PR では制作は始まらない。自動マージはしない。
- `deploy/` の workflow は配布用の原本で、公開 Actions の定義と揃えて管理する。

## Secrets と変数

- `CLIPFORGE_PAT`: 公開 ClipForge の Actions から非公開保存庫の Contents を読み書きする。
- `COLAB_ADC_JSON`: 指定ユーザーの Colab 自動起動用。初回設定は [COLAB.md](COLAB.md)。
- `YOUTUBE_COOKIES_B64`（任意）: YouTube の取得に使う Cookie。音声取得と描画時の映像取得にだけ渡し、文字起こしと清書には渡さない。
- `GOOGLE_DRIVE_ADC_JSON`: Drive 納品を指定したときだけ必要。[TARGET_DRIVE.md](TARGET_DRIVE.md)。
- 変数 `CLIPFORGE_VERTEX_PROJECT_NUMBER`: Vertex の Workload Identity 用（[VERTEX_GEMINI.md](VERTEX_GEMINI.md)）。
- Secrets は必要なステップにだけ渡す。値は文書・チャット・Git・公開 Artifacts に出さない。

## 待機と上限

- 制作の監視は `--timeout` 秒（既定 7200 秒）を上限に待つ。超えた場合は `monitoring_paused` として記録し、遠隔の制作失敗とは区別する。Run は止めない。
- 描画は render-only の 1 Run につき 1 つの Colab runtime を使い、Run の中では選んだクリップを順に描画する。並列化は clip_index 別の Run と merge Run（`dispatch_parallel_render.py`）で行う。
- 同じ制作内の GPU 枠は最大 5。

## 再開と回収

- 切断後は `--resume --output DIR`。保存済みの依頼 ID と Run ID を照合して、監視と回収だけを続ける。新しい投入はしない。
- 完成クリップは `tools/collect_production_clip.py --clip N` で、全体が終わる前でも 1 本ずつ回収できる。
- 制作の状態は `production-state.json`、修正版の状態は `revision-state.json`、音声理解の受領記録は `audio-editor-receipt.json` に残る。

## 失敗したとき

- 失敗した Run は自動で再投入しない。段階とエラー種別を確かめ、必要なら同じ依頼を利用者が明示してから再開する。
- 描画のエラー詳細は、取得できた場合だけ作業出力の `diagnostics/render/` に保存する（URL と認証情報は伏せる）。
- モデルの拒否・割り当て超過・認証の拒否を、別アカウントや別サービスで回避しない。

## 旧経路の運用

- 旧の投入と回収（`submit_job.py`・`submit_audio_stage.py`）と全編 ASR の部分結果は、[ASR.md](ASR.md) と [AUDIO_FIRST.md](AUDIO_FIRST.md) の復旧用。現行の制作には使わない。
