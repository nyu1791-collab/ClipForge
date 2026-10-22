# Colab GPU

対象は `n.yu17919191@gmail.com`。ColabでこのアカウントとPro表示を確認し、GPUランタイムを選びます。Pro契約、計算ユニット残高、GPU割り当ては別です。T4の割り当て・処理時間は保証しません。

## 起動

`notebooks/colab_gpu.ipynb` をColabで開き、GPUをT4にしてセットアップセルを実行します。`nvidia-smi`、cuBLAS/cuDNNのロード、CTranslate2のCUDA/float16を確認してから進みます。Colab用ツールはGPUがなければ停止します。

### Self-hosted Runner

登録先は**非公開の `nyu1791-collab/ai-model-workers`**。公開ClipForgeには登録しません。登録トークンは非公開workerの Settings → Actions → Runners → New self-hosted runner から取得し、ノートブックの非表示入力へ渡します。通常のGitHub PATは使いません。短時間で失効するので起動直前に取得し、Git・保存セル・チャットに貼らないでください。

```bash
bash tools/colab_setup.sh
bash tools/colab_runner.sh  # トークンは非表示入力
```

公式Runnerの最新リリースを公式SHA-256で照合し、`colab-gpu` ラベル、固有名、`--ephemeral` で登録します。既存ランナーは置換しません。`Listening for Jobs` と非公開GitHub側の `Idle` が揃うと接続完了です。それ以前は接続完了ではありません。

`deploy/clipforge.yml` を非公開workerへ配置すると `worker=colab-gpu` の依頼をGPUへ振り分けられます。VM入口では次を指定します。

```bash
python tools/submit_job.py --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID --ref REVIEWED_GPU_COMMIT_SHA \
  --worker colab-gpu --wait --output /path/to/new-result
```

GPUジョブはCUDA/float16を必須とし、GPU不在でCPUへ切り替えません。依頼前にIdleを確認します。オフラインならジョブは待機します。1ジョブ後は自動登録解除され、次の依頼には新規登録が必要です。待機中に切断した場合はGitHub側のOfflineランナーを削除します。Colabのセッション維持や制限回避は行いません。

[公式FAQ](https://research.google.com/colaboratory/faq.html)では、正の計算ユニット残高がない無料ランタイムで分散計算ワーカーなどを制限しています。Self-hosted方式は利用が認められる契約・残高で使ってください。無料枠ではノートブック内の直接実行を使います。

### ノートブック内の直接実行

Driveをマウントし、動画と明示的なGeminiモデルIDを指定します。

```bash
source tools/colab_cuda_env.sh
.venv/bin/python tools/colab_run.py --input /content/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID --config config.example.json \
  --output /content/clipforge-output/job-001 \
  --drive-output /content/drive/MyDrive/ClipForge_Output/job-001
```

`--input` の代わりに `--url` で直接動画ファイルのHTTPS URLを取得できます。YouTube視聴ページ・HLSには非対応です。署名付きURLやAPIキーを保存セルへ残さないでください。GeminiキーはColab Secretsの `GEMINI_API_KEY` から読みます。指定Geminiモデルへ候補動画・音声・ASRを送る点は従来と同じです。

処理はローカルディスクで行い、完了後DriveへコピーしてSHA-256を照合します。マウントしても転送・同期時間は発生し、アップロード時間はゼロではありません。コピー失敗時もローカル出力は保持します。再開には同じ入力・設定・コードとローカル出力が必要です。

## 選択設定

- `whisper_device=auto`: CTranslate2のCUDA/float16とcuBLAS/cuDNNが利用できればGPU、なければCPU/int8。PyTorchは不要です。
- `--device cuda --compute-type float16`: GPU必須。モデルロード・推論途中の失敗ではCPUや別モデルで再試行しません。
- `threads=0`: 使用可能なCPUコア数を自動取得。正の値ならその数を使用します。
- `video_encoder=auto`: 短いNVENCエンコードが成功すれば `h264_nvenc/p1/VBR/CQ`、利用不可なら `libx264/veryfast/CRF`。
- `--video-encoder h264_nvenc`: NVENC必須。スケーリング、libass字幕、場面検出などは引き続きCPU処理です。

構文・依存ロック・wheel作成と、GPU実接続・推論・実動画の速度や品質は別の確認状態です。元の「92分を2分」などは実測していません。
