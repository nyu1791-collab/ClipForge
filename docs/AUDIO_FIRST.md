# 音声から話題を割り振り、候補の映像を判定する

対象は同じ配信から3〜5本の16:9・1920×1080横動画。Shortsは対象外。
主な順序はメディアURL → runnerで音声取得 → Colab GPUで2分割ASR → 結合・話題分割 → 同じrunでpreviews・未承認の下書き → 映像確認・承認 → Colab CPUで映像取得・描画 → 制御VMで `merge --local`。
音声だけで最終採用せず、映像を確認する停止点を持つ。外部LLM・Gemini・OpenCodeの推論は、この処理から呼び出さない。

## 段階と私有ファイル

| stage | 入力・出力 | 実施しない処理 |
| --- | --- | --- |
| `audio`（既定） | 対応URL → `audio.m4a`、取得receipt | ASR、話題選定、候補映像、描画 |
| `topics` | 保存音声 → 共有ASR、`topics.json`、ASR再利用receipt | 全編映像取得・場面検出、動画生成 |
| `previews` | 音声話題候補 → 上位候補の低解像度映像、`previews.json` | 高画質取得、最終採用の自動承認 |
| `render` | 分析と対応するレビュー済み区間JSON → 映像取得・採用区間の横動画3〜5本 | 全編ASR、別モデルfallback |

`topics.json`は、発話の間と語彙の変化で割り振った区間とASR本文を含む。
意味的な話題の一致・完結や面白さを承認した結果ではない。プレビュー候補の優先順位は発話秒数による。
`tools/topic_hints.py` は本文を出力せず、NFKC正規化・空白/句読点/記号除去後の文字3-gramのJaccard類似度が0.5以上の相手IDを `near_duplicates`（類似度順・最大3件）、全相手との最大値を `max_similarity`（小数3桁）で示す。
ほぼ重複のペアでは発話密度 `speech_ratio` が低い方（同率なら後ろ）に `duplicate` の目印と0.2の減点を一度だけ付ける。自動除外はせず、採否は担当者が判断する。
音声が手元にある previews では、1秒RMSの dB 系列を前後60秒の非無音窓の中央値と比べる（-50dB未満は無音として基準に使わず、自身の突出も0）。話題の区間内の窓のうち上位3窓の平均突出（6dB未満は0、15dB以上は1）を求め、重み0.05の加点（`volume_score`・`volume_bump_db`）とする。音声を読めない場合は加点せず、diagnostics の `volume_hints.data` が `not_available`、`reason` が `decode_failed`（復号失敗）または `no_audio`（音声サンプルなし）になる。
長い話題は現行上限180秒で区切る。20分等への尺拡張は行っていない。
音声専用renditionがあれば優先する。なければ最軽量HLS（映像を含む）を一時受信するため、通信量が音声のみになる保証はない。
音声を確定した後、一時的なTSとその取得キャッシュを削除し、以後は音声ファイルとASRを再利用する。

`workspace`全体、音声、ASR、区間本文、プレビュー、詳細品質記録は非公開に置く。公開Gitや公開Artifactsへ追加しない。
同じworkspaceの排他ロックで重複起動を防ぐ。異なる配信URLのworkspace共用を拒否する。
取得済みファイルはSHA-256に加え、元URL・開始・終了・音声専用フラグを照合し、不一致や片方だけ残ったキャッシュで先へ進まない。

## 音声取得と非公開workerでの話題分割

Kickに加え、YouTubeの単一動画、Twitchのアーカイブ・クリップ、直接HTTPS mp4・m4a・mp3・webm・HLS m3u8に対応する。**あなたが権利を持つファイルを置いた場所の URL** を指定する。署名クエリを含む元URLは非公開の同一性検証へ保持する。YouTube・Twitchと直接ファイルはyt-dlpを使い、HLSは終了済みのmedia playlistが必要。MP3・M4Aなど音声のみの入力は解析までで、`--auto-previews` は指定しない。

追加経路のメタデータに長さがない場合は、runnerで取得・実測してからGPU割り当ての可否を決める。直接ファイルは音声抽出のためにも全体転送が必要な場合がある。追加経路の映像描画では元ファイル全体をremuxし、元時刻を保ったまま採用区間を描画する。Kickの区間取得だけに適用される通信量の説明と区別する。

URLだけの投入は既定で2分割。境界前後5秒の文脈を付けたFLACを独立した2つのColabランタイムへ渡し、単語時刻・依頼ID・ハッシュを検証して結合してから話題分割を一度だけ行う。`--asr-parts 1` または設定 `asr_parts: 1` で連続ASRを使う。キャッシュの識別と終了処理は [ASR_SPLIT.md](ASR_SPLIT.md)。直接CLIの `topics` は連続ASR用で、2分割は非公開workerへのURL投入で使う。

```bash
uv sync --locked --extra media-fetch --extra chat
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --workspace inputs/private-vod --stage audio
```

URLだけで音声取得・ASR・話題分割を投入できる。対応する `deploy/clipforge.yml` と実装を非公開workerへ配置して使う。
投入側はreleaseを作らず、設定をJSON文字列のdispatch入力 `settings_json`（UTF-8で最大16 KB）で渡す。依頼ID・設定はローカルの `request.json` にも保存する。`--output` で完了待機と検証付き回収を行う。
URLだけの投入（`--input` なし）は、投入側チェックアウトの `config.landscape.example.json` を土台に `--config` の指定キーを上書きし、CLIの指定を反映した設定を保存・送信する。許可キーと16 KB制限を投入前に検証する。土台を含めた設定は16 KB未満で、差分送信は不要。runner取得とColab解析は依頼内の設定だけを読み、各環境のexample設定を読み直さない（既存のCUDA必須指定時のdevice/compute type上書きは継続）。
dispatch前に制御側でKickVODのメタデータと最低画質プレイリストを読み、終了済みか・配信長が上限内かを確認する。この確認ではTSを取得せず、ASRや描画も開始しない。上限超過は配信長と設定上限（秒）を示して停止し、配信中・接続再試行の最終失敗も理由を示して停止する。runner・GPUは起動しない。制御側にも `media-fetch` の依存が必要。
GitHub-hosted `ubuntu-24.04` runnerが、GPU割り当て前に `uv sync --locked --extra media-fetch --no-dev` の環境で音声を取得する。
runnerは `GITHUB_TOKEN` で一時releaseを作り、`tools/acquire_audio_url.py` が `tools/submit_job.py` の共通関数で設定のサイズ・JSON・許可キーを検証する。KickVODの既定6並列取得と `audio_first.acquire` を使い、音声を `input-video.bin`、設定・SHA-256・サイズ確定済みの依頼を `request.json` としてrunnerがアップロードしてからGPUを割り当てる。一時releaseは既存の `always()` ステップで削除する。
runnerの音声取得は固定済み依存のPyAVで復号し、16 kHz・モノラル・AAC 48 kbpsのm4aへ変換する。Kickの長さ確認はPyAV、追加経路の取得ファイルの確認はffprobeを使う。2分割用のPCM・FLAC作成にもffmpegを使うため、URLジョブはGPU起動前にffmpeg/ffprobeとNode.js 22を用意する。Node.jsはyt-dlpのYouTube取得に使用する。
Kick HTTP取得は429・500〜504・接続/タイムアウト例外を初回に加えて最大3回、5・15・45秒待って再試行する。`Retry-After`（秒またはHTTP日時）があれば最大60秒で従う。403は10秒待って1回だけ再試行し、その他の4xxは即失敗する。待機前に各試行の予算予約を解放し、他スレッドの取得を妨げない。最終エラーには最後のHTTPステータス（応答がなければunavailable）と試行回数を含め、URLのクエリ文字列は含めない。
`clipforge.yml`の各ステップに個別の上限を設ける。依存導入・release操作は10分、音声取得と入力転送は30分。ジョブ・Colab実行・待機の上限は [OPERATIONS.md](OPERATIONS.md#動画ジョブの実行場所) を参照する。
URL形式の検証はGoogleへの接続前に行う。Colabへ `AUDIO_URL` は渡さず、既存の `run_request.py prepare → run` の `audio_source_url` 分岐でASR・話題分割し、出力回収後すぐruntimeを停止する。
workflowが一時releaseの `request.json` にある `audio_source_url` を確認し、音声解析では `CLIPFORGE_NO_MODEL_API=1` を制御コードへ渡す。URL投入・取得済み音声投入ともGeminiキーを要求せず、Colabへ送らない。音声分岐はGeminiクライアントを作らず、モデルAPIを呼ばない。
URL投入では手元に音声SHA-256がないため、結果の `request-receipt.json` の `sha256["audio.m4a"]` を基準に `audio.m4a`・manifestを照合し、receiptの `audio_source_url` とmanifestの `source_url` を投入URLと照合する。

```bash
uv run --no-sync python tools/submit_job.py \
  --audio-analysis-url 'https://kick.com/example/videos/00000000-0000-0000-0000-000000000001' \
  --worker colab-auto --ref REVIEWED_COMMIT_SHA --output /private/audio-analysis-NEW
```

### 同じrunで候補映像と採用候補の下書きまで作る

URLだけの投入に `--auto-previews` を付けると、ColabのASR・話題分割後にruntimeを解放し、同じrunのGitHub-hosted CPUジョブで候補映像・静止画シート・`review-draft.json`・diagnosticsを作る。`colab-auto` 専用で、受付票に `target_stage: previews` を記録する。

```bash
uv run --no-sync python tools/submit_job.py \
  --audio-analysis-url 'https://kick.com/example/videos/00000000-0000-0000-0000-000000000001' \
  --worker colab-auto --ref REVIEWED_COMMIT_SHA --auto-previews \
  --wait-timeout 18000 --output /private/previews-NEW
```

待機上限・通常の状態確認と回収は [OPERATIONS.md](OPERATIONS.md#結果の回収と再開) を参照する。
前段は `clipforge-analysis-REQUEST_ID`、最終結果は従来名の `clipforge-result-REQUEST_ID` として非公開artifactへ7日保存する。CPUジョブは同じ `source_ref` と投入時の設定を使用し、同じrun・依頼ID・元コミット・ハッシュを照合する専用経路で中間artifactを読む。通常の別run経路では引き続き前段の成功完了を要求する。
最終回収は `submit_audio_stage.py` と共通の検証で、全ファイルのハッシュ・ASR再利用・前段の系譜を確認する。下書きは未承認のままなので、映像・話題・境界の確認後に別の承認ファイルを作る。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage render \
  --previous-run CHAINED_RUN_ID --previous-request REQUEST_ID \
  --source-ref REVIEWED_COMMIT_SHA --review-file /private/reviewed-topics.json
```

前段だけ成功して候補映像ジョブが失敗・キャンセルされた場合、受付票は `status: PARTIAL_FAILED` とし、`analysis_status` と `previews_status` を分けて記録する。通常の最終回収は失敗を返す。保存済みのローカル受付票がある環境では、中間解析だけを明示して回収できる。

```bash
uv run --no-sync python tools/submit_job.py --collect REQUEST_ID \
  --collect-stage analysis --output /private/analysis-RECOVERED
```

この回収は `clipforge-analysis-REQUEST_ID` の元コミット・run・依頼ID・音声・ASR・話題のハッシュを検証し、`analysis_collection_status` と `analysis_output` に結果を保存する。最終段階の失敗状態は保持する。`--collect-stage previews` は最終結果を明示する指定としても使える。中間回収は新たなGPU・候補映像生成を開始しない。

音声取得済みなら、重いASRだけを非公開 `clipforge.yml` workerに渡す従来の方法も使える。
`--ref`はレビュー済みの完全なコミットSHAを指定する。

```bash
uv run --no-sync python tools/submit_job.py \
  --input inputs/private-vod/audio.m4a \
  --audio-analysis-url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --ref REVIEWED_COMMIT_SHA --config config.landscape.example.json
```

この分岐もGemini設定・キーを要求せず、選定APIへ到達しない。ASRはColab GPUでCUDA/float16、CPU workerでは設定に従う。別配信やハッシュ不一致のASRは流用しない。認証失敗時の復旧、同じ依頼の再開、ローカル受付票なしの回収、途中保存は [OPERATIONS.md](OPERATIONS.md#結果の回収と再開) を正とする。

### 長い配信の予算とメモリ

- `max_source_seconds` の設定可能上限は43,200秒（12時間）。landscape設定は21,600秒（6時間）なので、URLだけの投入も既定で6時間まで受け付ける。モデルの既定3,600秒はローカル動画の `pipeline` / `landscape` の長さ制限にも使われるため維持する。6時間を超える終了済み配信は、必要な上限を `--config` で明示する。
- 音声取得時の `max_transport_bytes` は既定6 GiB、設定可能範囲1 KiB〜16 GiB。最低画質に映像が含まれる場合の一時TS取得予算であり、保存音声の上限とは別。保存音声には `max_input_bytes`（既定1 GiB、上限2 GiB）を適用する。AAC 48 kbpsの6時間音声は概算130 MB（コンテナ等を除く）。最低画質のビットレート次第では6時間未満でも転送予算に達するので、必要なら16 GiB以内で明示する。
- 後段workerのプレビュー・1080p区間取得は従来の2 GiB予算。直接CLIの `previews` / `render` は従来の `max_input_bytes` 予算を維持し、`audio` / `topics` の音声取得にだけ `max_transport_bytes` を使う。選定区間のみの映像取得に全編音声用の予算を広げない。
- 6時間音声のメモリ見積もりと実測の未確認事項は [品質記録](QUALITY_REVIEW.md) の「長時間音声のメモリ見積もり（2026-10-22）」を参照する。

十分なCPUのある環境なら、同じworkspaceに対して `--stage topics` でも実行できる。
制御VMで `topics`・`previews`・`render`を実行しない。回収済み音声と `transcripts/` を対応付けて、十分なCPUのある私有環境へ渡す。
workerから回収したファイルをworkspaceへ統合する際は、`audio.m4a`のSHAと `source_url` の一致を先に確認する。

## 非公開workerで候補映像・横動画を続ける

`deploy/audio-stages.yml`は、非公開workerの`main`へ`.github/workflows/clipforge-stages.yml`として配置する原本。
投入先は`clipforge-stages.yml`・refは`main`。既存ASR用の`clipforge.yml`と併設する。
後段workflowはColab転送release用の`contents: write`と`actions: read`を使い、モデルAPI用Secretを渡さない。
CPUの`merge`ではOSパッケージ導入を省略する。他のCPU段階はImageOS・ImageVersion・パッケージ一覧をキーに`.deb`を公式`actions/cache@v4`で保存する。
未導入分だけキャッシュから`apt-get install --no-download`し、不足時だけupdateと通常installを実施する。このaptステップの上限は10分で、描画に必要なffmpegとフォントの導入は維持する。

前段の成功artifactをworker内で回収し、元音声・ASR・話題の全ハッシュを確認する。
有効なASRがなければ、取得・認識や別モデルへのfallbackで補わず停止する。
後段では元音声の再アップロードも不要。候補映像は、必要ならASR本文を確認して指定した話題IDだけを取得できる。

```json
{"topic_ids": ["topic-001", "topic-005", "topic-009"]}
```

これは書式例であり、実データの話題IDを確認して私有JSONへ保存する。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage previews \
  --previous-run SUCCESSFUL_ASR_RUN --previous-request ASR_REQUEST_ID \
  --source-ref REVIEWED_COMMIT_SHA --review-file /private/audio-topic-shortlist.json
```

候補ごとの低解像度映像と、10%・50%・90%地点の静止画を並べた`preview-contact-sheet.jpg`を私有artifactへ保存する。
静止画のサンプリングは動画全体を視聴したことを意味しない。話題の本文とサンプル映像で確認できた範囲を選定理由へ記す。
その後、候補映像の成功runを指定して、確認済みの非重複3〜5区間を描画する。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage render \
  --previous-run SUCCESSFUL_PREVIEW_RUN --previous-request PREVIEW_REQUEST_ID \
  --source-ref REVIEWED_COMMIT_SHA --review-file /private/reviewed-topics.json
```

描画の設定は `--config-file NAME`（既定 `config.landscape.example.json`）で選ぶ。NAMEは `config.英小文字・数字・ハイフン.example.json` の形で、`--source-ref` のコミットに存在するものだけ受け付ける。依頼IDに設定名を含むため、同じ選定でも設定が違えば別依頼になる。`merge` は部分の設定名がすべて同じことを確認し、混在すれば停止する。

話題を長めに残す設定例は `config.long-topics.example.json`。最大15分、左上の見出し、通常の1.6倍の字幕を指定する。見出しの位置は `heading_position`（`top-center`／`top-left`）、字幕の大きさは `caption_scale` で設定する。長尺でも開始・終了は話題と発話の境界を確認して選び、短い別の話題を本数合わせでつなげない。`max_seconds` は最大1800秒まで指定できるが、解析フレーム数6000枚の上限は維持されるため、設定例では `analysis_fps=0.5`・`candidates=3` とする。
例: `--config-file config.landscape-chat.example.json` を上の `--stage render` に追加する。

同一stage・前段run・選定内容・設定名・コードSHAは同一依頼IDになる。同じ依頼の二重dispatchや、応答不明の要求の自動再送は行わない。
生成物は非公開`clipforge-result-REQUEST_ID`artifactに7日保存する。公開側のActionsで実配信を処理しない。

完了後の回収・検証は [OPERATIONS.md](OPERATIONS.md#結果の回収と再開) を参照する。描画はColab CPUで実行する。

### 単一クリップ描画と制御VMでの結合

`--stage render --clip-index N`（またはColabの`--clips N`）の成果物は、描画完了後に`ranges/`・`audio.m4a`（取得receipt含む）・`previews/`・`transcripts/`・作業用topics/previews JSONを除去する。
`delivery/clips/clip-00N/`、両manifestと`asr-receipt.json`は残す。結合はASR本文でなく、そのreceiptのキーを分析の同一性確認に使う。
SHA-256一覧は除去後に作成し、manifestのクリップ別一覧も残ったクリップ内のファイルを指す。

結合は制御VMで `merge --local` を使い、ASRや描画を再実行せず検証・コピーする。実行例・期待値の固定・receiptの扱いは [OPERATIONS.md](OPERATIONS.md#単一クリップ成果物のローカル結合) を参照する。

## チャット欄

設定 `chat_panel`（既定 false）を true にすると、横向き16:9の出力の右側に `chat_panel_width`（既定 480 px、出力幅の20〜40%、偶数）のチャット欄を置く。
映像は左の欄に `chat_panel_fit` で収める。既定の `fill` は映像を高さ全体（1080 p なら 1440×1080）に拡大し、左右を切って欄の左に置く。字幕は映像の上に重ね、大きさは72 px 程度（1行約18字）。`fill` では `auto_layout` が映像の左右位置を決め、字幕は下（中央下）に置く。下側に HUD・ホットバーなど、時間が経っても動かない細部（字幕の前後3秒の中央値の勾配）が混んだ行のまとまりとしてあるときは、その上まで字幕を持ち上げる（上限は高さの32%。2秒未満の間隔で隣り合う字幕は、高い方にそろえる（隣の字幕だけ））。窓の全体と左右の半分のどちらかで混んだまとまりを採る。右下のワイプは、枠などの動かない細部が見えるときだけ拾い、動く映像そのものは拾えないことがある。失敗時は全て下に出す（`render.json` の `layout` に `lift_min`・`lift_max`・`lifted`・`reason` などを記録）。チャット欄のない16:9の横画面（fit・blur）も同じ規則で持ち上げる（既定は `caption_lift` が true。`panels` 配置は判定せず下に出す）。判定の詳細と既知の限界は [QUALITY_DECISIONS.md](QUALITY_DECISIONS.md) の「字幕の位置」を正とする。`top` は16:9の映像を左上に置き、その下の帯（欄と同じ色）に字幕を出す（1080 p では帯270 px）。`crop` は中央から左右を削り、`letterbox` は縮めて上下に帯を置く（どちらも字幕は中央下）。
`caption_scale`（0.8〜2.0、既定 1.0）は字幕の大きさに掛ける（既定は 1080 p で 52 px）。横用の設定例は `config.landscape-chat.example.json`（チャット欄 fill、無音カットなし）。
取得は Kick VOD の URL 経路だけで、各クリップの元の時刻 [start, end] のチャットを読む。
無音カット（`silence_cut`）と縦型（9:16・1:1）は設定検証で拒否する。チャットは元動画の時刻で合わせるため、無音カットで時刻がずれると成立しない。
取得に失敗したクリップは失敗にせず、欄は空のまま描き、`render.json` の `chat.reason` に `chat_unavailable` を記録する。`chat_anonymize` が true なら発言者名を匿名化する。

## 候補映像の判定と採用

`--stage previews`で音声の話題候補（既定最大8件）の区間だけ低解像度映像を取得する。
映像の内容・発話との一致・画面の見やすさ・話題の始まりと結びを確認し、採用区間JSONを私有領域に作る。
`video_reviewed: true`はその確認を行った担当者の申告。プログラム自身が人の視聴を証明する値ではない。

### 自動下書きから承認ファイルを作る

非公開workerの `previews` 完了時に `review-draft.json` と `review-draft-diagnostics.json` を作成し、両方を `request-receipt.json` のSHA-256一覧へ含める。成功した候補映像のある話題だけを `hint_topics()` の順位で並べ、異なるID・非重複・設定の尺に収まる元の話題区間から、既定で `settings.clips` 件（3〜5件）を下書きにする。
ヒント順位は意味・面白さ・映像の判定ではない。全クリップは `video_reviewed: false`、`confidence: 0.0`、タイトルは `要確認：<topic-id>`、理由は未確認である旨の定型文となる。
共有ASRの単語途中切断と、単語時刻のない発話の途中切断を既存の境界検証で除外する。安全な候補が足りなくても補充・時刻調整は行わず、diagnostics の `requested_count`・`selected_count`・`shortfall` と `skipped` に不足・除外理由を記録する。diagnostics に発話本文は含めない。文字起こしを渡さず関数を利用した場合は `transcript_boundary_check: not_available` となる。

1. 成功した候補映像runを `--collect` で私有領域へ回収し、下書きとdiagnosticsを確認する。
2. 対応する `topics.json`・共有ASRと候補映像を確認し、意味・映像・始まりと結びを判断する。静止画シートだけで視聴済みとはしない。
3. 下書きを別名の `/private/reviewed-topics.json` にコピーし、採否・時刻・タイトル・理由・confidenceを修正する。分析キーを維持し、各区間を対応する話題内、設定尺内、単語の切れ目に収める。不足があれば別の成功済み候補を確認し、要求本数の異なる話題・非重複区間を揃える。
4. 確認して採用する全クリップだけを `video_reviewed: true` に変更し、その承認ファイルを `--stage render --review-file` に渡す。未確認・欠落・真偽値以外のフラグを含むファイルと空の選定は、CPU・Colab・単一/並列クリップいずれも投入前に拒否する。描画側でも分析・映像・境界を引き続き検証する。

下書き作成後も `semantic_topic_review: required`、`awaiting_video_review`、`human_review_performed: false` の停止点を維持する。自動作成は人の判断・承認の完了を意味しない。

```json
{
  "analysis_key": "topics.jsonのanalysis_key",
  "clips": [
    {
      "candidate_id": "topic-001",
      "start": 120.0,
      "end": 240.0,
      "title": "実際に確認した話題名",
      "reason": "話題と映像を確認した根拠",
      "confidence": 0.8,
      "video_reviewed": true,
      "crop_anchor": "auto"
    }
  ]
}
```

上は1区間の書式例。`crop_anchor`（任意、`auto`・`left`・`center`・`right`、既定 `auto`）は fill のチャット欄で映像の左右位置を固定する。`auto` 以外は字幕の持ち上げ判定だけを行う。値が不正なら投入前に拒否する。`caption_floor`（任意、0〜0.32、映像の高さに対する字幕の下余白の最小値）は、その区間の字幕をこの高さ以上に置く。自動判定が拾えないワイプの対策に使う。値が不正なら投入前に拒否する。実行時は要求本数と一致する、異なる話題の非重複3〜5区間が必要。
時刻は配信全体の秒数で、対応する音声話題内に収める。
確認していない候補、別ASRの分析キー、改変されたプレビュー、重複した話題を拒否する。

```bash
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' --workspace inputs/private-vod \
  --stage render --review-file inputs/reviewed-topics.json
```

採用区間だけ最大1080pをremux（再符号化なし）で取得し、全編ASRの単語時刻を区間内へ移す。区間ごとのASRは実行しない。字幕焼き込みを含む最終描画は別の再エンコードで、`encode_threads=0` により全コアを使う。
ASR単語や単語時刻のない発話の途中切断は推測で補わず拒否する。全採用区間の字幕境界を確認してから、高画質取得を開始する。
字幕7字/秒・音量・全復号等の品質規則は [QUALITY_DECISIONS.md](QUALITY_DECISIONS.md) を正とする。strict通過は警告0件や人の読みやすさの承認を意味しない。
共有用 `delivery/e2e-summary.json` は数値・件数とコード/設定/フォント等の識別情報のみ。詳細警告中の発話例や素材パスをコピーしない。

### YouTube の Cookie 入力と匿名診断

ローカル取得は `tools/acquire_audio_url.py --cookies-file /private/cookies.txt ...`
または `python -m clipforge.audio_first --cookies-file /private/cookies.txt ...` を使う。
`YOUTUBE_COOKIES_B64` に Netscape 形式ファイルを Base64 化した値を渡す方法も使える。
明示したファイルを優先し、YouTube/Google ドメイン以外を含むファイル、壊れた形式、1 MiB 超を拒否する。
取得中だけ権限600のコピーを作り、終了・失敗時に削除する。元ファイルは変更しない。

Actions では **非公開 worker リポジトリ** の Secret `YOUTUBE_COOKIES_B64` を設定する。
例: `base64 -w0 /private/cookies.txt | gh secret set YOUTUBE_COOKIES_B64 --repo nyu1791-collab/ai-model-workers`。
Cookie の内容をコマンド引数、request/settings JSON、Release、Artifact に入れない。
音声取得と後段の映像取得にだけ渡し、ASR のランタイムには渡さない。
Colab 描画時は既存の非公開ジョブペイロード経由で転送し、ペイロードと一時 Cookie を削除する。
`submit_job.py --cookies-file` はローカル事前確認専用。Actions にファイルをアップロードしないため、
remote 実行用 Secret は別途必要。Secret の有効期限や IP・アカウント判定によっては取得が拒否される。

`probe_only` は Cookie を使わず、Docker 版 bgutil 2.0.2 と、その時点で取得した
動作確認済みの固定版 yt-dlp（実バージョンも結果に記録）で `web,mweb` を検証する。
既定の `fetch_pot=auto` と、`fetch_pot=always;webpage_skip=player_response` を比較し、
後者は player API を再要求して Token の生成・付与を確かめる。Token の値は保存しない。
固定版の公式 `warp-cli` を使い、`warp=on` の確認後に生成サーバーを再起動して、
IP 変更前のキャッシュを破棄してから同じ取得を比較する。
WARP は使い捨て GitHub-hosted runner のみで使用し、終了時に切断してコンテナを削除する。
メタデータ比較後に実際の全音声取得・ASR用変換も検証し、成功時のみ音声を private Artifact に7日間保存する。
GPU 割当や ASR は行わない。成功・失敗、UTC 時刻、経過秒も private Artifact に残る。
PO Token や Cookie、WARP による成功を保証しない。
過去の匿名8条件の結果と追加検証は [YOUTUBE_DIAGNOSTICS.md](YOUTUBE_DIAGNOSTICS.md) を参照。

通常のURL音声取得へWARP経路を明示する場合は、
`submit_job.py --youtube-audio-transport warp`を使う。
固定環境、失敗時停止、終了確認、音声Artifact再利用は
[YOUTUBE_TRANSPORT.md](YOUTUBE_TRANSPORT.md) を参照。
