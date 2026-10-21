# ClipForge

動画ファイルやKickのVODから、CPUの文字起こしとFFmpeg編集で字幕付き動画を生成します。
Kickバッチは全編の軽量音声解析を共有し、Shortsと話題別の通常動画を各3〜5本作成します。
既存のローカル動画向けには、候補の映像・音声を明示指定したGeminiモデルへ渡して選定する経路もあります。

## Kick配信からShortsと話題別の通常動画

1配信の全編音声とCPU文字起こしを共有し、**縦型Shorts 3〜5本**と**16:9の話題別動画3〜5本**を作れます。
通常動画の既定目標は5分、Shortsは45秒です。ローカルの語彙変化・発話境界・音量を使う経路で、外部LLMを呼びません。
背景ブラー、箱付き見出し、日本語の語・文節を使う2行字幕、BGMダッキング、開始SE、透過枠に対応します。

重い処理は非公開workerへ渡します。`tools/render_rich_videos.py --output-kind both`の使い方、
キャッシュによる再開、検証済みの範囲は[運用手順](docs/RICH_BATCH.md)と[品質確認記録](docs/QUALITY_REVIEW.md)を参照してください。
自動選定のスコアは意味的な面白さや話題の完結を保証しません。
文脈を確認して調整した区間は、`tools/submit_rich_batch.py`で私有の入力としてworkerへ渡せます。

## できること

- 動画ファイル1本から、CPU/int8のFaster-Whisperで単語時刻付き文字起こし。
- PySceneDetectの場面転換、発話、任意のチャット反応を使って候補を絞る。チャットなしでも動作します。
- 候補の低解像度動画をGeminiへ送り、映像・音声・文字起こしから区間・タイトル・理由をJSONで受け取る。
- 元動画の時刻へ戻し、範囲・長さ・重複を検査して、縦／横／正方形のMP4を生成。
- 日本語のASS字幕を焼き込み、編集用SRT・ASS、選定結果と生成記録も保存。カラオケ字幕／通常字幕を選択。
- 完了した段階をチェックサム付きで再利用。モデルは明示指定し、自動再試行・別モデルへのfallbackを行いません。

設定例は30〜45秒、ぼかし背景、太字・黒縁字幕、黄色の見出しです。
ライブラリの既存デフォルト（20〜75秒、fit）は保持。候補8件、最大3本出力、Whisper `small`、720px高。
Geminiが見るのは候補動画だけです。動画全体の全場面を視聴・評価する設計ではありません。

## 配信API・認証情報

Twitchを優先し、Kick Developer Portalの**公式API**、ツイキャス公式API、YouTubeのRSSと
Data APIに対応。認証情報は `.env.example` の名前で環境へ設定し、実値はSecretsで管理します。

```bash
uv sync --locked --extra platforms
uv run --no-sync clipforge-platforms credentials
uv run --no-sync clipforge-platforms discover twitch CHANNEL_LOGIN
```

Twitch/Kickの署名付きWebhookを軽量VMで受信し、最小限のイベントをSQLiteへ保存できます。
Webhookからの自動ジョブ投入は未接続です。Kick URLからの取得・制作は上記のバッチ入口を使います。
実装済み・未設定の一覧は[自動取り込み確認](docs/INTEGRATION_AUDIT.md)。
Kick VODのブラウザ互換通信による短いMP4サンプル取得は
[取得方法と実測結果](docs/KICK_VOD_DIAGNOSIS.md)を参照してください。
詳しい発行・登録・受信手順は[配信API設定](docs/PLATFORM_SETUP.md)、
YouTubeのキャッシュ・予算・拒否時の対応は[YouTube運用](docs/YOUTUBE_OPERATIONS.md)へ。

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
Kick URLを入力する場合は、`tools/render_rich_videos.py`のバッチ入口を使います。
顔追跡、音源分離、自動SNS投稿は未実装です。

## ショート動画の見た目

`--framing blur-fit` は元映像を拡大してガウスぼかしをかけた背景へ、全体が見える映像を重ねます。
字幕は白太字＋黒縁。`--headline --hook-title '見出し'` で黄色の見出しを上部へ表示。
`run` の見出しは選定されたタイトルを使用し、必要なら `--hook-title` で上書きできます。

```bash
uv run --no-sync clipforge cut /path/to/source.mp4 \
  --start 120 --end 159 --config config.example.json \
  --hook-title '親になった途端に教育を語り出す' \
  --transcript /path/to/transcript.json --output outputs/short-001
```

顔を大きくする場合は `config.facecam.example.json` の矩形を**実素材で計測して**変更します。
値は元映像の幅・高さに対する0〜1の比率。例の左下位置は仮値です。
`two-panel` は上段にゲーム、下段に顔を拡大表示します。自動で顔の位置を推測しません。

詳細：[構成](docs/ARCHITECTURE.md)／[運用・データの扱い](docs/OPERATIONS.md)／[採用ライブラリ](docs/THIRD_PARTY.md)。
