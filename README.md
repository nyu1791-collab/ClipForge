# ClipForge

同じ配信から、話題別の横動画（16:9・1920×1080）を3〜5本作るパイプラインです。現在の開発対象は横動画です。

URL経路は **音声取得 → 2分割ASR → 話題分割 → プレビューと未承認の下書き → 映像確認・承認 → 描画 → 成果物の結合**。Geminiは使いません。制御VMでは依頼・回収・検証・結合を行い、重いASRと描画はworkerで行います。

## URLから横動画を作る

初回設定は [COLAB.md](docs/COLAB.md)。Python 3.11以上、uv、認証済みのghが必要です。FFmpeg・日本語フォント等の実行環境はworker側で準備します。

```bash
uv sync --locked --extra media-fetch --extra chat
uv run --no-sync python tools/submit_job.py \
  --audio-analysis-url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --worker colab-auto --ref REVIEWED_COMMIT_SHA --auto-previews \
  --output /private/previews-NEW
```

`--ref` は確認した完全なコミットSHAへ置き換えます。Kick、YouTube動画、Twitchアーカイブ・クリップ、直接HTTPSのmp4・m4a・mp3・webm・m3u8に対応します。権利のある終了済みの入力を使います。取得可否はサービスの状態・認証・入力形式に依存し、ライブやプレイリスト一括取得は扱いません。

下書きは未承認です。プレビューの映像・話題・境界を確認して、別の承認ファイルを作ります。既定3本をColab CPUで個別に描画する例です。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage render \
  --previous-run SUCCESSFUL_PREVIEW_RUN --previous-request PREVIEW_REQUEST_ID \
  --source-ref REVIEWED_COMMIT_SHA --review-file /private/reviewed-topics.json \
  --worker colab-auto --clips 3
```

本数は承認ファイルと設定にそろえます。4本を超える場合は、同時描画上限を守って分けて依頼します。描画済みの各partを制御VMで検証・コピーします。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage merge --local \
  --part-runs RUN1:REQUEST1,RUN2:REQUEST2,RUN3:REQUEST3 \
  --source-ref RENDER_SOURCE_SHA --output /private/merged-NEW
```

ローカル結合は描画と同じSHAをcheckoutして実行します。音声だけのURLは `--auto-previews` を外して解析まで。連続ASRは `--asr-parts 1`。段階別の引数・承認方法は [AUDIO_FIRST.md](docs/AUDIO_FIRST.md)、回収・再開は [OPERATIONS.md](docs/OPERATIONS.md) を参照してください。

## ローカル動画を依頼する

```bash
python tools/submit_job.py --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID --config config.example.json \
  --wait --output /private/result-NEW
```

この経路は候補映像・音声・該当ASRを指定Geminiモデルへ送り、選定して描画します。モデルIDを明示し、別モデルへ自動切替しません。workerは既定 `colab-auto`、CPUを使う場合は `--worker cpu`。設定例のモデル名は現在の提供・無料枠を保証しません。

十分なCPUのある環境では `clipforge run/analyze/batch` を直接使えます。横動画には `--config config.landscape.example.json` を指定します。手動選定は `--selection-file`、ASRの再利用は `--transcript-cache`。時刻指定の `cut` は次の節を参照してください。詳細は [構成](INDEX.md#処理構成) と [運用](docs/OPERATIONS.md) を参照してください。

## 時刻指定の切り抜き

```bash
python -m clipforge.cli cut /private/source.mp4 \
  --start 120 --end 165 --output /private/raw-clip
```

通常の `cut` は再エンコードなしへ置き換えた。`--output` は新しいディレクトリで、MP4は `clips/clip-001/video.mp4`、処理結果は `manifest.json` に保存する。FFmpegを直接呼び、入力前の `-ss` と `-c copy` で先頭の映像・音声トラックを再エンコードせず保存する。ASR・Gemini・字幕描画・音量解析・入力全体のハッシュ計算は行わない。音声なしにも対応する。既存ファイルを上書きせず、MP4に非対応のコーデックはエラーとして返す。

開始点は直前のキーフレームへ寄るため、指定区間より前の映像を含み、長さもずれる場合がある。JSONの `duration` は出力の実測値、`frame_accurate=false` は境界精度を保証しない意味。速度はディスク・入力形式・区間長に依存する。`cut` の描画用引数（`--config`、`--transcript`、`--aspect`、`--video-encoder` 等）と `--resume` は削除した。切り抜きモードの選択は不要。字幕・見出しなどを制作する既存の `run` は描画が必要なため再エンコードする。再エンコード時は既存の `--video-encoder auto` が利用可能なNVENCを選び、CPU環境ではlibx264を使う。

シークとストリームコピーの仕様は [FFmpeg公式ドキュメント](https://ffmpeg.org/ffmpeg.html#Main-options) を参照。

## 設定・結果・文書

横の設定例は `config.landscape.example.json`、チャット欄付きは `config.landscape-chat.example.json`。設定の型・既定値は `src/clipforge/models.py`、品質の規則は [QUALITY_DECISIONS.md](docs/QUALITY_DECISIONS.md) が基準です。公開キャッシュは既定offです。

出力はMP4、ASS/SRT、品質値、入力・実装・成果物の識別情報を含みます。`complete` は処理とファイル検査の完了で、人による内容確認・警告0件・公開の完了ではありません。元入力は上書きしません。

| 知りたいこと | 参照先 |
| --- | --- |
| 構成・コードの入口・共通ルール | [INDEX.md](INDEX.md)・[AGENTS.md](AGENTS.md) |
| URLの段階・映像承認 | [AUDIO_FIRST.md](docs/AUDIO_FIRST.md) |
| 認証・Colab・キャッシュ | [COLAB.md](docs/COLAB.md) |
| 回収・再開・私有データ | [OPERATIONS.md](docs/OPERATIONS.md) |
| 品質規則・納品の確認 | [QUALITY_DECISIONS.md](docs/QUALITY_DECISIONS.md) |
| 開発・軽量検査・重い検査の選択 | [DEVELOPMENT.md](docs/DEVELOPMENT.md) |
| 過去の調査・計測 | [HISTORY.md](docs/HISTORY.md) |
| ライブラリとライセンス | [THIRD_PARTY.md](docs/THIRD_PARTY.md) |
