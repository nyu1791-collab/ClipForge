# ClipForge

Faster-Whisper → チャット／場面転換から候補抽出 → **映像と音声をGeminiに渡して選定** → FFmpegで切り抜き・ASS字幕、というCPU/CUDA対応パイプラインです。

実装と実行基盤を用意しました。**実動画による通し実行、文字起こし精度、Geminiの応答、字幕の見た目は未確認です。**

## できること

- 動画ファイル1本から、CUDA/float16またはCPU/int8のFaster-Whisperで単語時刻付き文字起こし。GPUを自動検知します。
- PySceneDetectの場面転換、発話、任意のチャット反応を使って候補を絞る。チャットなしでも動作します。
- 候補の低解像度動画をGeminiへ送り、映像・音声・文字起こしから区間・タイトル・理由をJSONで受け取る。
- 元動画の時刻へ戻し、範囲・長さ・重複を検査して、縦／横／正方形のMP4を生成。
- 日本語のASS字幕を焼き込み、編集用SRT・ASS、選定結果と生成記録も保存。カラオケ字幕／通常字幕を選択。
- 完了した段階をチェックサム付きで再利用。モデルは明示指定し、自動再試行・別モデルへのfallbackを行いません。

初期値は候補8件、各最大75秒、最大3本出力、Whisper `small`、720px高。Geminiが見るのは候補動画だけです。動画全体の全場面を視聴・評価する設計ではありません。

## ActionsからColab Pro GPUを自動起動

Google公式Colab CLIのSDKで、動画ジョブに合わせてT4を起動し、処理後に解放する経路を用意しました。初回だけ指定GoogleユーザーのOAuthを非公開workerの `COLAB_ADC_JSON` に登録します。依存・モデルは非公開キャッシュから再利用します。**OAuth未登録の状態では実GPU起動は未確認です。** 詳細は [Colab設定・検証手順](docs/COLAB.md)。

## 推奨：制御VMから非公開Actionsへ依頼

公開のこのリポジトリにはコードを置き、動画・文字起こし・成果物は非公開の
[`ai-model-workers`](https://github.com/nyu1791-collab/ai-model-workers/actions/workflows/clipforge.yml)で処理します。
制御VMはファイルの送信・依頼・回収だけを担当し、文字起こしやレンダリングを行いません。

VMで、入力パスと利用する動画対応GeminiモデルのAPI識別子を指定します。

```bash
clipforge-submit \
  --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID \
  --config /home/n_yu1791/repos/ClipForge/config.example.json \
  --wait --output /path/to/new-result-directory
```

`YOUR_VIDEO_MODEL_ID` は実在する識別子に置き換えてください。未指定のモデルや有料モデルへの自動切替はありません。
`--wait` を外すとActionsのURLを表示して終了します。Geminiの無料tier・利用枠・料金はキーの所属プロジェクトによります。

チャットを使う場合は `--chat-file /path/to/chat.json` または `--chat-url '配信アーカイブURL'` を追加。
後者は公開チャットの取得だけに使い、動画本体はダウンロードしません。

## CPU環境で直接使う

Colab GPUでの直接実行・非公開Actionsへのランナー接続は [Colab起動手順](docs/COLAB.md) と `notebooks/colab_gpu.ipynb` を参照してください。

Linux、Python 3.11以上、FFmpeg/FFprobe（libass入り）、日本語フォントが必要です。1GBの制御VMでは実行しません。

```bash
sudo apt-get install -y ffmpeg fontconfig fonts-noto-cjk
uv sync --locked --extra chat

uv run --no-sync clipforge run /path/to/source.mp4 \
  --config config.example.json --gemini-model YOUR_VIDEO_MODEL_ID \
  --output outputs/job-001
```

`GEMINI_API_KEY` は保護された環境変数として渡します。チャット・コマンド履歴・Gitへ値を貼らないでください。
同じ入力・設定・コードで続行する場合は `--resume`。入力や設定が変わった場合は新しい出力先にします。

AIを呼ばず、秒数を指定して切り抜くこともできます。

```bash
uv run --no-sync clipforge cut /path/to/source.mp4 \
  --start 120 --end 165 --aspect 9:16 --output outputs/manual-001
```

`cut` は自動文字起こしを行いません。字幕を付ける場合は `--transcript transcript.json` を指定します。

## 出力

```text
manifest.json              選定内容・生成結果・モデルと使用量の記録
transcript.json            元動画の秒数での文字起こし
candidates.json            候補と抽出スコア
selection.json             選定区間・理由・モデル呼び出し記録
clips/clip-001/video.mp4    字幕付きMP4
clips/clip-001/captions.ass
clips/clip-001/captions.srt
clips/clip-001/render.json  長さ・画面サイズ・各ファイルのSHA-256
```

出力の `complete` は処理とファイル検査の完了です。人による内容・画面確認や、公開の完了を意味しません。
顔追跡、音源分離、自動SNS投稿は含みません。Colab用ツールは直接動画ファイルのHTTPS URLを取得できますが、YouTube視聴ページやHLSには対応しません。

詳細：[構成](docs/ARCHITECTURE.md)／[運用・データの扱い](docs/OPERATIONS.md)／[採用ライブラリ](docs/THIRD_PARTY.md)。
