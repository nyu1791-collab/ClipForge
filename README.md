# ClipForge

Faster-Whisper → チャット／場面転換から候補抽出 → **映像と音声をGeminiに渡して選定** → FFmpegで切り抜き・ASS字幕、というCPU向けパイプラインです。

実装と実行基盤を用意しました。CIと同じ条件のローカル実行で、単体・合成動画・音声認識のテストが通ることを確認しています。**Geminiの応答、字幕の見た目と文字起こし精度は、実際の動画で確認してください。**

## できること

- 動画ファイル1本から、CPU/int8のFaster-Whisperで単語時刻付き文字起こし。
- PySceneDetectの場面転換、発話、任意のチャット反応を使って候補を絞る。チャットなしでも動作します。
- 候補の低解像度動画をGeminiへ送り、映像・音声・文字起こしから区間・タイトル・理由をJSONで受け取る。
- 元動画の時刻へ戻し、範囲・長さ・重複を検査して、縦／横／正方形のMP4を生成。
- 日本語のASS字幕を焼き込み、編集用SRT・ASS、選定結果と生成記録も保存。カラオケ字幕／通常字幕を選択。
- 完了した段階をチェックサム付きで再利用。モデルは明示指定し、自動再試行・別モデルへのfallbackを行いません。
- 話題の区切り（間と語彙の変化）ごとに候補を作り、長すぎる話題は分割します。
- 任意で長い無音を詰めます。字幕と映像は同じ時刻に合わせて再計算します。
- 縦型のぼかし背景、顔と画面の2段配置、題名の見出し、2パスの音量調整（-16 LUFS）。
- 出典つきの画像、独自の表・棒グラフ・手順図、背景音楽（声の間は自動で下げる）、効果音、サムネイル。

初期値は候補8件、各最大75秒、最大3本出力、Whisper `small`、720px高。Geminiが見るのは候補動画だけです。動画全体の全場面を視聴・評価する設計ではありません。

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

## 編集オプション

設定は `config.example.json` にまとめています。

| 設定 | 内容 |
|---|---|
| `layout` | `fit`（従来）、`blur`（ぼかし背景）、`panels`（顔と画面の2段。`face_box` が必要） |
| `heading` | 題名を画面上部に表示 |
| `topics`, `topic_min_seconds`, `topic_max_seconds` | 話題ごとに候補を作る。既定は25〜180秒 |
| `silence_cut`, `silence_pad`, `silence_min_gap` | 文字起こしに基づき長い無音を詰める |
| `visuals` | 手元の画像・SVG。`source_credit`、`source_url`、`license` が必須 |
| `diagrams` | 表、棒グラフ、手順図。ClipForge作成の独自図解 |
| `image_search`, `image_sources` | 題名で画像を検索。Wikimedia CommonsまたはOpenverseのうち、CC0・パブリックドメイン・CC BY・CC BY-SAだけを使用 |
| `bgm`, `bgm_gain_db`, `ducking`, `sound_effects` | 背景音楽（声の間は自動で下げる）と効果音 |
| `thumbnail` | 切り抜きの1フレームに題名を載せた1280×720のサムネイル |

使った画像と出典は `visuals/` と `ledger.json` に保存されます。画像の出典は画面の下部に表示されます（ClipForge作成の図解は除く）。
SVGは外部参照やDTDを含むものを拒否します。`visuals`・`bgm`・`sound_effects` はローカルのファイルを指すため、非公開Actionsへの依頼では使えません。
画像検索は題名の語で行うため、関係のない画像が選ばれることがあります。出力前に必ず確認してください。

## 出力

```text
manifest.json              選定内容・生成結果・モデルと使用量の記録
transcript.json            元動画の秒数での文字起こし
candidates.json            候補と抽出スコア
selection.json             選定区間・理由・モデル呼び出し記録
clips/clip-001/video.mp4    字幕付きMP4
clips/clip-001/captions.ass
clips/clip-001/captions.srt
clips/clip-001/ledger.json  使った画像・図解の出典とSHA-256
clips/clip-001/visuals/      使った画像・図解のファイル
clips/clip-001/thumbnail.png 任意：サムネイル
clips/clip-001/render.json  長さ・画面サイズ・各ファイルのSHA-256
```

出力の `complete` は処理とファイル検査の完了です。人による内容・画面確認や、公開の完了を意味しません。
顔追跡、音源分離、自動SNS投稿、URLからの動画取得は含みません。

詳細：[構成](docs/ARCHITECTURE.md)／[運用・データの扱い](docs/OPERATIONS.md)／[採用ライブラリ](docs/THIRD_PARTY.md)。
