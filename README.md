# ClipForge

同じ配信から、話題別の**横動画（16:9・1920×1080）を3〜5本**作るパイプラインです。現在の開発対象は横動画です。

主な流れは **メディアURL → runnerで音声取得 → Colab GPUで2〜4分割ASR → 結合・話題分割 → 同じrunでpreviewsと未承認の下書き → 映像確認・承認 → Colab CPUで描画 → 制御VMで `merge --local`** です。URL経路ではGeminiを呼びません。Kickは採用区間の映像を取得します。追加経路では形式により元ファイル全体の転送が必要です。

ローカル動画は `submit_job.py --input` から依頼し、文字起こし・候補抽出・Gemini選定・描画を行います。workerは既定の `colab-auto`、または明示した `cpu` です。制御VMは依頼・回収・結合だけを担当し、重いASRや描画を行いません。

## メディアURLから解析・横動画を作る

初回の認証・Colab設定は [COLAB.md](docs/COLAB.md)、段階ごとの詳細は [AUDIO_FIRST.md](docs/AUDIO_FIRST.md) を参照してください。

```bash
uv sync --locked --extra media-fetch --extra chat
uv run --no-sync python tools/submit_job.py \
  --audio-analysis-url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --worker colab-auto --ref REVIEWED_COMMIT_SHA --auto-previews \
  --output /private/previews-NEW
```

`--ref` はレビュー済みの完全なコミットSHAへ置き換えます。話題分割は発話の間と語彙変化による候補作成です。下書きは未承認のため、候補映像・話題・境界を確認して別の承認ファイルを作ります。

GPUを使うURL経路（上の手順）の対象はKickの配信動画です。YouTube・Twitchのアーカイブ・クリップと直接のHTTPS **mp4・m4a・mp3・webm・HLS m3u8** URLは追加の経路で、実機での検証は完了していません。**あなたが権利を持つファイルを置いた場所の URL** を指定してください。署名付きURLも使えます。YouTube・Twitchはyt-dlpとNode.jsで取得し、ライブ・プレイリスト一括取得は受け付けません。

MP3・M4Aなど音声だけの入力は `--auto-previews` を外して解析します。映像のプレビューや描画には映像を含む入力が必要です。URLだけの投入は既定で4つのColabランタイム（L4 2台とT4 2台）へ音声を分け、元の時刻へ結合してから話題を分割します。`--asr-parts` で2〜4部分を選べます。`--asr-parts 1` で従来の連続ASRを指定できます。詳細は [2分割ASR](docs/ASR_SPLIT.md)。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage render \
  --previous-run SUCCESSFUL_PREVIEW_RUN --previous-request PREVIEW_REQUEST_ID \
  --source-ref REVIEWED_COMMIT_SHA --review-file /private/reviewed-topics.json
```

描画はColab CPUで行います。単一クリップごとに描画した成果物は、制御VMで検証・コピーして結合できます。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage merge --local \
  --part-runs RUN1:REQUEST1,RUN2:REQUEST2,RUN3:REQUEST3 \
  --source-ref RENDER_SOURCE_SHA --output /private/merged-NEW
```

`--local` の結合は、描画に使ったコミット（`--source-ref` と同じ SHA）を制御VMでチェックアウトした状態で実行する。手元のコードが違うと `local merge code differs from source_ref` で停止する。例: `git worktree add --detach /tmp/merge-SHA SHA` で別の作業ツリーを作って、その中で実行する。

横用設定は `config.landscape.example.json`。配信長は既定6時間・最大12時間、字幕の表示延長目標と警告基準は7字/秒です。ジョブの実行上限はローカルCPU動画90分、ColabのURL取得を含む全体180分です。結果の回収・再開は [OPERATIONS.md](docs/OPERATIONS.md#結果の回収と再開)、実素材の受入条件は [LANDSCAPE_PLAN.md](docs/LANDSCAPE_PLAN.md) を参照してください。

音声取得だけで止める場合は次を使います。ASR・描画は開始しません。直接ファイルは映像を含めた全体転送になる場合があります。

```bash
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --workspace inputs/private-vod --stage audio
```

## ローカル動画を依頼する

公開リポジトリにはコードを置き、動画・文字起こし・成果物は非公開Actionsで処理します。初回設定は [COLAB.md](docs/COLAB.md)、データの扱いは [OPERATIONS.md](docs/OPERATIONS.md) を参照してください。

```bash
python tools/submit_job.py \
  --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID \
  --config "<CLONE>/config.example.json" \
  --wait --output /path/to/new-result-directory
```

`<CLONE>` はチェックアウト先へ置き換えます。`YOUR_VIDEO_MODEL_ID` は利用する動画対応モデルのAPI識別子です。`config.example.json` の `gemini_model` は `gemini-3.8-flash`。モデル選定の理由と利用枠の未確認事項は [品質の決定事項](docs/QUALITY_DECISIONS.md#6-gemini-のモデル) を参照してください。未指定のモデルや有料モデルへの自動切替はありません。

workerは既定の `colab-auto`、CPUを使う場合は `--worker cpu`。チャットを使う場合は `--chat-file /path/to/chat.json` または `--chat-url '配信アーカイブURL'` を追加します。後者は公開チャットの取得だけに使います。

## 十分なCPUのある環境で直接使う

Linux、Python 3.11以上、FFmpeg/FFprobe（libass入り）、日本語フォントが必要です。

```bash
sudo apt-get install -y ffmpeg fontconfig fonts-noto-cjk libcairo2
uv sync --locked --extra chat
uv run --no-sync clipforge run /path/to/source.mp4 \
  --config config.example.json --gemini-model YOUR_VIDEO_MODEL_ID \
  --output outputs/job-001
```

`GEMINI_API_KEY` は保護された環境変数として渡します。同じ入力・設定・コードで続行するときは `--resume`、字幕や描画だけを変える場合は `--transcript-cache DIR` で文字起こしを再利用できます。入力・設定が変わる場合は新しい出力先を使います。

手動選定で横動画を確認する例です。有効なASRキャッシュを指定し、`--execute` を付けた場合だけ生成・検証します。

```bash
uv run --no-sync python tools/e2e_landscape.py \
  --input /private/source.mp4 --selection-file /private/selection.json \
  --transcript-cache /private/transcript-cache --output outputs/landscape
```

合成の選定例は `data/examples/landscape-selection.json`。解析のみは `clipforge analyze INPUT --config config.landscape.example.json --output outputs/analysis`、横動画のCLI実行にも `--config config.landscape.example.json` を指定します。

複数入力は `clipforge batch a.mp4 b.mp4 --config config.example.json --gemini-model YOUR_VIDEO_MODEL_ID --output-root outputs/batch-001`。AIを呼ばず時刻指定で切り抜く場合は `clipforge cut INPUT --start 120 --end 165 --aspect 16:9 --output outputs/manual-001` を使います。`cut` は自動文字起こしを行わず、字幕には `--transcript transcript.json` が必要です。

## 設定と出力

設定例は `config.example.json` と `config.landscape.example.json`。品質の規則と理由は [QUALITY_DECISIONS.md](docs/QUALITY_DECISIONS.md) を正とします。

- `threads` の既定は `0`（自動）。横用設定の `2` は明示値です。描画は別の `encode_threads=0` で全コアを使います。
- `asr_batch_size` はCPU用の任意設定です。GPUでは採用せず、既定の `0`（逐次処理）を使います。CPUの計測では高速化した一方、無音の扱いと話題境界も変わりました。
- `encode_preset=slow`・CRF 18は、40秒の比較で `veryfast`・CRF 20よりPSNRが約3.9 dB高く、時間はほぼ同じでした。
- レイアウト、見出し、無音詰め、出典付き画像・図解、BGM、効果音、サムネイルは設定で指定します。ローカルの画像・音源パスは非公開Actionsへの依頼では使えません。
- 公開キャッシュは既定offです。Colabの認証・キャッシュ手順は [COLAB.md](docs/COLAB.md) を参照してください。

ローカル動画経路の主な出力は `manifest.json`、`transcript.json`、`candidates.json`、`selection.json`、`clips/clip-001/` 内のMP4・ASS・SRT・`quality.json`・`render.json` です。画像の出典は `ledger.json` と `visuals/`、音声先行の納品物は `delivery/` に保存します。`manifest.json` の `timings` は各段階の実時間（秒）です。

出力の `complete` は処理とファイル検査の完了であり、人による内容・画面確認や公開の完了ではありません。顔追跡、音源分離、自動SNS投稿は含みません。URL取得は音声先行の公開Kick VODに限定しています。

## 文書

- [構成](docs/ARCHITECTURE.md)
- [音声先行の段階と承認](docs/AUDIO_FIRST.md)
- [Colab設定・検証手順](docs/COLAB.md)／[公式資料の調査](docs/COLAB_RESEARCH.md)
- [運用・回収・再開・データの扱い](docs/OPERATIONS.md)
- [品質の決定事項](docs/QUALITY_DECISIONS.md)／[計測記録](docs/QUALITY_REVIEW.md)
- [横動画の受入条件と未決事項](docs/LANDSCAPE_PLAN.md)
- [採用ライブラリ](docs/THIRD_PARTY.md)
