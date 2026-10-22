# ClipForge

現在の横動画経路は、**全編の軽量音声取得 → CPU文字起こし → 話題の割り振り → 候補映像だけ取得・確認 → 採用区間の高画質取得 → 横動画・ASS字幕**です。
全編の高画質映像を先に取得・走査しません。既存のローカル動画＋Gemini選定経路も残っています。
Faster-Whisper → チャット／場面転換から候補抽出 → **映像と音声をGeminiに渡して選定** → FFmpegで切り抜き・ASS字幕、というCPU/CUDA対応パイプラインです。

実装と実行基盤を用意しました。CIと同じ条件のローカル実行で、単体・合成動画・音声認識のテストが通ることを確認しています。**Geminiの応答、字幕の見た目と文字起こし精度は、実際の動画で確認してください。**

現在の開発対象は、同じ配信から話題別の**横動画（16:9）を3〜5本**作る運用です。
現状の確認、外部APIなしの最小経路、読み速度と実素材の受入条件は[横動画の実装提案](docs/LANDSCAPE_PLAN.md)を参照してください。
この計画ではShortsの開発・生成は行いません。

## 話題別の横動画：音声を先に取得する

[音声先行の運用手順](docs/AUDIO_FIRST.md)を参照してください。モデルAPIは呼びません。
音声取得、話題分割、候補映像、レビュー済み区間の生成は別の段階です。
話題分割は発話の間と語彙変化による候補作成であり、意味的な完結・面白さの確認とは区別します。

```bash
uv sync --locked --extra media-fetch --extra chat
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --workspace inputs/private-vod --stage audio
```

`audio`で止まり、ASR・高画質取得・描画は開始しません。音声専用のHLSがない配信では最軽量のHLSを一時受信し、映像を除いた音声を保存します。
重いASR・描画は制御VMで行わず、非公開CPU workerを使います。
横用設定は `config.landscape.example.json`（1920×1080、3本、各20〜180秒、CPU/int8/2スレッド、読み速度7字/秒。2026-10-22、利用者の判断で4字/秒から変更）です。

既にローカル動画と有効なASRキャッシュがある場合、以下は事前検査だけで止まります。
`--execute`を付けた場合だけ、十分なCPUのある環境で手動選定3〜5本を生成・検証します。

```bash
uv run --no-sync python tools/e2e_landscape.py \
  --input /private/source.mp4 --selection-file /private/selection.json \
  --transcript-cache /private/transcript-cache --output outputs/landscape
```

合成の区間例は `data/examples/landscape-selection.json`。実配信へそのまま適用しないでください。
解析のみは `clipforge analyze INPUT --config config.landscape.example.json --output outputs/analysis`。
これはASR・場面検出を行う既存ローカル動画経路なので、音声先行のKick経路とは入口を分けています。

## できること

- 動画ファイル1本から、CUDA/float16またはCPU/int8のFaster-Whisperで単語時刻付き文字起こし。GPUを自動検知します。
- PySceneDetectの場面転換、発話、任意のチャット反応を使って候補を絞る。チャットなしでも動作します。
- 候補の低解像度動画をGeminiへ送り、映像・音声・文字起こしから区間・タイトル・理由をJSONで受け取る。
- 元動画の時刻へ戻し、範囲・長さ・重複を検査して、縦／横／正方形のMP4を生成。
- 日本語のASS字幕を焼き込み、編集用SRT・ASS、選定結果と生成記録も保存。カラオケ字幕／通常字幕を選択。
- 完了した段階をチェックサム付きで再利用。モデルは明示指定し、自動再試行・別モデルへのfallbackを行いません。
- 話題の区切り（間と語彙の変化）ごとに候補を作り、長すぎる話題は分割します。
- 任意で長い無音を詰めます。字幕と映像は同じ時刻に合わせて再計算します。
- 縦型のぼかし背景、顔と画面の2段配置、題名の見出し、2パスの音量調整（-16 LUFS）。
- 出典つきの画像、独自の表・棒グラフ・手順図、背景音楽（声の間は自動で下げる）、効果音、サムネイル。

初期値は候補8件、各最大75秒、最大3本出力、縦9:16は1080×1920（スマートフォンでそのまま鮮明に見える大きさ）。文字起こしは Whisper `large-v3-turbo`（日本語の認識精度を優先）、符号化は `slow`・CRF 18、背景はぼかし、品質ゲートは `strict` です。Geminiが見るのは候補動画だけです。動画全体の全場面を視聴・評価する設計ではありません。

処理時間の目安（この環境での実測、15分の動画）：文字起こしは5〜12分、1080×1920の切り抜きは60秒あたり2.5〜4分です（2本を並列に処理）。同じ入力でも計測ごとに時間が変わるため、幅で示しています。60分の入力では文字起こしだけで30分以上かかり、描画を合わせるとGitHub Actionsの上限（90分）に近づくことがあるため、長い入力は分けて依頼してください。

## ActionsからColab Pro GPUを自動起動

Google公式Colab CLIのSDKで、動画ジョブに合わせてT4を起動し、処理後に解放する経路を用意しました。初回だけ指定GoogleユーザーのOAuthを非公開workerの `COLAB_ADC_JSON` に登録します。依存・モデルは非公開キャッシュから再利用します。指定アカウントのT4起動、Whisper CUDA/float16の合成音声処理、キャッシュ保存、解放確認は実機検証済みです。実動画での品質・所要時間は未確認です。詳細は [Colab設定・検証手順](docs/COLAB.md)。

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

推奨は `gemini-3.8-flash` です（2026-10-22 の公式の料金ページで、動画入力に対応し、無料枠の対象と記載）。`config.example.json` の `gemini_model` にも同じ値を入れています。上位の `gemini-4-argon` は無料枠の対象外（有料）のため、既定にしていません。無料枠の上限（RPM・RPD）は、公式の制限ページにモデル別の数値がなく、Google AI Studio の表示で確認してください。
`YOUR_VIDEO_MODEL_ID` は実在する識別子に置き換えてください。未指定のモデルや有料モデルへの自動切替はありません。
`--wait` を外すとActionsのURLを表示して終了します。Geminiの無料tier・利用枠・料金はキーの所属プロジェクトによります。

チャットを使う場合は `--chat-file /path/to/chat.json` または `--chat-url '配信アーカイブURL'` を追加。
後者は公開チャットの取得だけに使い、動画本体はダウンロードしません。

## CPU環境で直接使う

Colab GPUでの直接実行・非公開Actionsへのランナー接続は [Colab起動手順](docs/COLAB.md) と `notebooks/colab_gpu.ipynb` を参照してください。

Linux、Python 3.11以上、FFmpeg/FFprobe（libass入り）、日本語フォントが必要です。1GBの制御VMでは実行しません。

```bash
sudo apt-get install -y ffmpeg fontconfig fonts-noto-cjk libcairo2
uv sync --locked --extra chat

uv run --no-sync clipforge run /path/to/source.mp4 \
  --config config.example.json --gemini-model YOUR_VIDEO_MODEL_ID \
  --output outputs/job-001
```

`GEMINI_API_KEY` は保護された環境変数として渡します。チャット・コマンド履歴・Gitへ値を貼らないでください。
同じ入力・設定・コードで続行する場合は `--resume`。入力や設定が変わった場合は新しい出力先にします。

複数の動画をまとめて処理するには `batch` を使います。動画ごとに別のフォルダへ出力し、1本が失敗しても他は続きます。

```bash
uv run --no-sync clipforge batch a.mp4 b.mp4 c.mp4 \
  --config config.example.json --gemini-model YOUR_VIDEO_MODEL_ID \
  --output-root outputs/batch-001
```

同時に処理する本数は、既定ではCPUコア数を各動画のスレッド数と描画ワーカー数で割った値です（4コアで既定設定なら1本）。`--concurrency` で変えられます。結果は `batch.json` にまとまります。

字幕や描画だけを変えて試すときは `--transcript-cache DIR` を付けると、同じ音声と認識設定の文字起こしを再利用し、認識のやり直しを省けます。保存された文字起こしには話の内容が含まれるため、保存先の扱いに注意してください。

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
| `layout` | `blur`（既定。ぼかした背景で画面を埋める）、`fit`（黒帯を入れて全体を残す）、`panels`（顔と画面の2段。`face_box` が必要） |
| `heading` | 題名を画面上部に表示 |
| `topics`, `topic_min_seconds`, `topic_max_seconds` | 話題ごとに候補を作る。既定は25〜180秒 |
| `silence_cut`, `silence_pad`, `silence_min_gap` | 文字起こしに基づき長い無音を詰める |
| `visuals` | 手元の画像・SVG。`source_credit`、`source_url`、`license` が必須 |
| `diagrams` | 表、棒グラフ、手順図。ClipForge作成の独自図解 |
| `image_search`, `image_sources` | 題名で画像を検索。Wikimedia CommonsまたはOpenverseのうち、CC0・パブリックドメイン・CC BY・CC BY-SAだけを使用 |
| `bgm`, `bgm_gain_db`, `ducking`, `sound_effects` | 背景音楽（声の間は自動で下げる）と効果音 |
| `thumbnail` | 切り抜きの1フレームに題名を載せた1280×720のサムネイル |
| `encode_preset` | x264の速度設定。既定は `slow`（CRF 18と組み合わせ）。`medium` と同程度の時間で、`veryfast`（CRF 20）より約4dB画質が高い。速さを優先するなら `veryfast` や `faster` |
| `table_query` | 日本語版Wikipediaの表を検索し、独自の表として描画。記事のURLとCC BY-SA 4.0の表記を画面に表示 |
| `quality_gate` | `strict`（既定）は失敗（字幕の重なり、3行以上の字幕、ASSとSRTの内容の不一致、フォントにない文字、出典・ライセンス不足、映像が最後まで復号できない、48 kHzでない音声、音量の測定不能）があるクリップを確定せず停止します。`report` は結果を `quality.json` に残して出力します |

速度を優先する場合は `asr_batch_size`（1〜16）で文字起こしを一括処理できます。実測では約2倍速くなりましたが、無音の扱いが変わり、話題の区切りも変わります（実音声では15件→10件）。既定の `0`（逐次処理）が品質の基準です。

使った画像と出典は `visuals/` と `ledger.json` に保存されます。画像の出典は画面の下部に表示されます（ClipForge作成の図解は除く）。
SVGは外部参照やDTDを含むものを拒否します。`visuals`・`bgm`・`sound_effects` はローカルのファイルを指すため、非公開Actionsへの依頼では使えません。
画像検索は題名の語で行うため、関係のない画像が選ばれることがあります。出力前に必ず確認してください。
表検索は `table_query` で指定した語で行います。Wikipediaの本文はCC BY-SA 4.0のため、その表を使った動画の扱い（継承の要否）は利用者が判断してください。

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
clips/clip-001/quality.json  字幕の読み速度・音量・真ピーク、失敗と警告
clips/clip-001/render.json  長さ・画面サイズ・各ファイルのSHA-256
```

字幕は1秒あたり10文字を目安にし、次の字幕までに無音があれば表示を延ばします。15文字を超えて残る字幕は `quality.json` の警告に出ます。語の途中で行や字幕が切れた箇所も、`word_splits` として警告に出ます（失敗にはしません）。画像・図解は `fade_seconds` の長さで出入りをフェードします。音量は EBU R128 で測り、−16 LUFS ±1 LU・真ピーク −1 dBTP 以下を目安に警告します（音量は `loudness_two_pass` の既定で合わせています）。
出力の `complete` は処理とファイル検査の完了です。人による内容・画面確認や、公開の完了を意味しません。
顔追跡、音源分離、自動SNS投稿は含みません。Colab用ツールは直接動画ファイルのHTTPS URLを取得できますが、YouTube視聴ページやHLSには対応しません。

`manifest.json` の `timings` には、各段階の実時間（秒）が入ります。
出力の `complete` は処理とファイル検査の完了です。人による内容・画面確認や、公開の完了を意味しません。
顔追跡、音源分離、自動SNS投稿は含みません。URL取得は音声先行の公開Kick VODに限定しています。

詳細：[構成](docs/ARCHITECTURE.md)／[運用・データの扱い](docs/OPERATIONS.md)／[採用ライブラリ](docs/THIRD_PARTY.md)／[品質の決定事項](docs/QUALITY_DECISIONS.md)。
