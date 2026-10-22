# 音声から話題を割り振り、候補の映像を判定する

対象は同じ配信から3〜5本の16:9・1920×1080横動画。Shortsは対象外。
順序は音声取得 → 全編ASRを共有 → 話題候補 → 候補映像の確認 → 採用区間だけ高画質取得 → 字幕・描画・品質検査。
音声だけで最終採用せず、映像を確認する停止点を持つ。外部LLM・Gemini・OpenCodeの推論は、この処理から呼び出さない。

## 段階と私有ファイル

| stage | 入力・出力 | 実施しない処理 |
| --- | --- | --- |
| `audio`（既定） | 公開Kick VOD → `audio.m4a`、取得receipt | ASR、話題選定、候補映像、高画質、描画 |
| `topics` | 保存音声 → 共有ASR、`topics.json`、ASR再利用receipt | 全編映像取得・場面検出、動画生成 |
| `previews` | 音声話題候補 → 上位候補の低解像度映像、`previews.json` | 高画質取得、最終採用の自動承認 |
| `render` | 分析と対応するレビュー済み区間JSON → 採用区間の高画質映像・横動画3〜5本 | 全編ASRや全編高画質の再取得、別モデルfallback |

`topics.json`は、発話の間と語彙の変化で割り振った区間とASR本文を含む。
意味的な話題の一致・完結や面白さを承認した結果ではない。プレビュー候補の優先順位は発話秒数による。
`tools/topic_hints.py` は本文を出力せず、NFKC正規化・空白/句読点/記号除去後の文字3-gramのJaccard類似度が0.5以上の相手IDを `near_duplicates`（類似度順・最大3件）、全相手との最大値を `max_similarity`（小数3桁）で示す。
ほぼ重複のペアでは発話密度 `speech_ratio` が低い方（同率なら後ろ）に `duplicate` の目印と0.2の減点を一度だけ付ける。自動除外はせず、採否は担当者が判断する。
長い話題は現行上限180秒で区切る。20分等への尺拡張は行っていない。
音声専用renditionがあれば優先する。なければ最軽量HLS（映像を含む）を一時受信するため、通信量が音声のみになる保証はない。
音声を確定した後、一時的なTSとその取得キャッシュを削除し、以後は音声ファイルとASRを再利用する。

`workspace`全体、音声、ASR、区間本文、プレビュー、詳細品質記録は非公開に置く。公開Gitや公開Artifactsへ追加しない。
同じworkspaceの排他ロックで重複起動を防ぐ。異なる配信URLのworkspace共用を拒否する。
取得済みファイルはSHA-256に加え、元URL・開始・終了・音声専用フラグを照合し、不一致や片方だけ残ったキャッシュで先へ進まない。

## 音声取得と非公開workerでの話題分割

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
runnerの音声取得は固定済み依存のPyAVで連結TSを復号し、16 kHz・モノラル・AAC 48 kbpsのm4aへ変換する。長さの確認もPyAVで行い、ffmpeg/ffprobe CLI・apt・`.deb`キャッシュは使わない。候補映像・高画質区間の取得と描画は従来どおりffmpeg CLIを使う。
Kick HTTP取得は429・500〜504・接続/タイムアウト例外を初回に加えて最大3回、5・15・45秒待って再試行する。`Retry-After`（秒またはHTTP日時）があれば最大60秒で従う。403は10秒待って1回だけ再試行し、その他の4xxは即失敗する。待機前に各試行の予算予約を解放し、他スレッドの取得を妨げない。最終エラーには最後のHTTPステータス（応答がなければunavailable）と試行回数を含め、URLのクエリ文字列は含めない。
`clipforge.yml`の全ステップに個別の上限を設ける。依存導入・release操作は10分、音声取得と入力転送は30分、Colab実行は既存の5,100秒上限を収める90分。URL取得を含むジョブ全体は150分とし、後処理の時間を確保する。
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

待機上限は既定18,000秒（5時間）で、`--wait-timeout SECONDS` に正の秒数を指定して変更できる。上限到達時もrunを再投入せず、受付票のIDで状態確認・回収を続ける。
前段は `clipforge-analysis-REQUEST_ID`、最終結果は従来名の `clipforge-result-REQUEST_ID` として非公開artifactへ7日保存する。CPUジョブは同じ `source_ref` と投入時の設定を使用し、同じrun・依頼ID・元コミット・ハッシュを照合する専用経路で中間artifactを読む。通常の別run経路では引き続き前段の成功完了を要求する。
最終回収は `submit_audio_stage.py` と共通の検証で、全ファイルのハッシュ・ASR再利用・前段の系譜を確認する。下書きは未承認のままなので、映像・話題・境界の確認後に別の承認ファイルを作る。

```bash
uv run --no-sync python tools/submit_job.py --status REQUEST_ID
uv run --no-sync python tools/submit_job.py --collect REQUEST_ID --output /private/previews-RECOVERED
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

この分岐はGemini設定・キーの登録を要求せず、選定APIへ到達しない。既存workflowの環境変数にGemini Secretが渡される場合も、その値を読み取らない。
一時音声は非公開releaseへ送信し、既存workerが処理後に削除する。失敗時も同一依頼を自動再投入しない。
転送で401等が返った場合は、登録済みGitHub接続の認証を利用者側で復旧してから進める。APIキーをチャットへ貼らず、別アカウント・別経路で拒否を回避しない。
`STAGING_FAILED`はASRやworkflowが起動したことを意味しない。一時releaseや同じ依頼IDを確認してから再開を判断する。
依頼IDから `tools/submit_job.py --status ID`、成功後に `--collect ID --output PRIVATE_NEW_DIRECTORY` で回収する。
ローカル記録（`~/.local/state/clipforge/ID/`）がない別環境では、期待値を渡す状態なし回収を使う。runの成功完了・依頼ID・元コミットSHA・音声SHA-256・ASR・話題・manifestを検査し、新規の出力先だけへ保存する。記録は書き込まない。

```bash
uv run --no-sync python tools/submit_job.py --collect REQUEST_ID --run-id RUN_ID \
  --source-ref REVIEWED_COMMIT_SHA --audio-sha256 AUDIO_SHA256 --output PRIVATE_NEW_DIRECTORY
```

ローカル記録がある場合は従来どおりで、これらの引数は使わない。
音声・ASR・話題・manifestのハッシュと依頼元SHAを検査する。Colab GPUではASRをCUDA/float16で実行し、CPU workerでは設定に従う。
キャッシュが一致しない音声や別配信のASRを流用しない。長いASRは完了まで時間を要し、失敗後の途中チェックポイント再開はこの経路では未実装。

### 長い配信の予算とメモリ

- `max_source_seconds` の設定可能上限は43,200秒（12時間）。landscape設定は21,600秒（6時間）なので、URLだけの投入も既定で6時間まで受け付ける。モデルの既定3,600秒はローカル動画の `pipeline` / `landscape` の長さ制限にも使われるため維持する。6時間を超える終了済み配信は、必要な上限を `--config` で明示する。
- 音声取得時の `max_transport_bytes` は既定6 GiB、設定可能範囲1 KiB〜16 GiB。最低画質に映像が含まれる場合の一時TS取得予算であり、保存音声の上限とは別。保存音声には `max_input_bytes`（既定1 GiB、上限2 GiB）を適用する。AAC 48 kbpsの6時間音声は概算130 MB（コンテナ等を除く）。最低画質のビットレート次第では6時間未満でも転送予算に達するので、必要なら16 GiB以内で明示する。
- 後段workerのプレビュー・1080p区間取得は従来の2 GiB予算。直接CLIの `previews` / `render` は従来の `max_input_bytes` 予算を維持し、`audio` / `topics` の音声取得にだけ `max_transport_bytes` を使う。選定区間のみの映像取得に全編音声用の予算を広げない。
- `signals.read_samples` は6時間 × 16,000 Hz = 345,600,000サンプルを読む。float32の保持配列は1,382,400,000 bytes（約1.38 GB / 1.29 GiB）。読み込み時には16-bit PCMの約0.69 GBと、変換・正規化のfloat32配列2本が一時的に重なり、当該処理だけのピークは概算3.46 GB（3.22 GiB）。ASR/VADのコピー・特徴量・モデル等は別途必要だが、51 GBのColab high-ramではこの音声読み込みがメモリ不足の主因となる見積もりではない。6時間の実ASRのピークRSS・所要時間・完走は未計測。

### 失敗・タイムアウト・キャンセル時の途中保存（私有）

初回workflowは、失敗・タイムアウト・キャンセル時だけ `transcripts/*.partial.jsonl` を `clipforge-partial-REQUEST_ID` 私有artifactへ7日保存する。
取り出しは同じ非公開repoで `gh run download RUN_ID --repo nyu1791-collab/ai-model-workers -n clipforge-partial-REQUEST_ID -D /private/partial-REQUEST_ID` を使う。
この中身は途中の発話本文で、完成した文字起こしではない。再開には使わず、同じ依頼を再実行しても途中保存からは続行しない。
取り出し先は私有領域に限り、公開Git・公開Artifactsへ追加しない。

十分なCPUのある環境なら、同じworkspaceに対して `--stage topics` でも実行できる。
制御VMで `topics`・`previews`・`render`を実行しない。回収済み音声と `transcripts/` を対応付けて、十分なCPUのある私有環境へ渡す。
workerから回収したファイルをworkspaceへ統合する際は、`audio.m4a`のSHAと `source_url` の一致を先に確認する。

## 転送元GitHub認証の診断

`tools/submit_job.py`の`dispatch()`は、`--input` がある場合、`gh release create`で一時release作成とファイル添付を行い、完了後にだけworkflowをdispatchする。
2026-10-22のこの作業環境では、制御側の `gh release create` はHTTP 403となる一方、dispatch・run閲覧・artifact取得は可能だった。URLだけの投入では制御側のrelease操作を行わず、runnerの `GITHUB_TOKEN` による既存のrelease作成経路を使う。以下は取得済み音声をアップロードする経路の過去の診断記録。
`gh()`は継承した環境でCLIを呼ぶ。この環境では`GH_TOKEN`が設定され、`GITHUB_TOKEN`は設定されていない。値は表示・取得しない。

2026-10-22の再診断では、同じ接続で以下を確認した。

| 操作 | 接続先 | 実測 |
| --- | --- | --- |
| 利用者・非公開repo・workflow読取 | `api.github.com` | 200 |
| 一時release作成 | `POST https://api.github.com/repos/nyu1791-collab/ai-model-workers/releases` | 201 |
| 同じreleaseへのファイル添付 | `POST https://uploads.github.com/repos/nyu1791-collab/ai-model-workers/releases/RELEASE_ID/assets` | 401 Bad credentials |

失敗は添付のアップロード段階であり、workflowのdispatchには未到達。
RESTの読み書きが成功しているため、全体のトークン失効や権限不足だけが原因とは断定できない。
アップロード先ホストへ有効な認証が届いているか、既存接続側の対応も確認する必要がある。
`gh auth status`は成功し、`GH_TOKEN`の使用を報告した。Git設定に流用可能なcredential helperは見つからず、Git push成功だけで添付APIの認証成功とは判断できない。
プロキシを外したり、注入済みの認証を無断で置き換えたりして解決しない。

復旧する場所は**転送元のこの作業環境のGitHub接続・シークレット設定**。workerリポジトリのActions SecretsにPATを追加するだけでは、転送元のCLIへは渡らない。
PATを使う場合は、利用者が安全な設定画面で登録する。Fine-grained PATは指定の非公開repoを対象とし、Contents: Read and write（release/添付）とActions: Read and write（dispatch/回収）を設定する。
classic PATは通常`repo`が必要で、workflowファイル自体を変更する場合は別途`workflow`の条件も確認する。
トークン値はチャット・コマンド履歴・文書へ貼らない。

診断で作成した私有releaseには添付がなく、ASRは未起動。同じ依頼IDの記録を`UPLOAD_FAILED_AUTH`として保全した。
復旧後は、そのreleaseの添付状態と同じIDのworkflowの有無を確認してから、不足した添付とdispatchだけを進める。
一から新しい依頼を作る、音声を再取得する、ローカルASRへ切り替える操作は行わない。

```bash
uv run --no-sync python tools/submit_job.py --status SAVED_REQUEST_ID
uv run --no-sync python tools/submit_job.py \
  --resume-audio SAVED_REQUEST_ID --input /private/original-audio.m4a
```

`--resume-audio`は、認証復旧後に同じ音声依頼を再開する実行コマンド。保存済みの設定・元コミットSHA・依頼IDを維持する。
元音声のSHA-256・サイズを照合し、添付済みのファイルはGitHub側のSHA-256・サイズが一致するときだけ再利用する。
不一致・重複添付・別の稼働中ジョブがあれば停止し、`--clobber`等で上書きしない。
すでに同じIDのworkflowがあれば新規dispatchせず、HTTP応答を失った等の不確かなdispatch状態も自動再送しない。
workflow起動前に`DISPATCHING`、応答不明なら`DISPATCH_UNCONFIRMED`を記録する。
投入ロックは同時実行数の確認・一時releaseの作成と添付・dispatchの記録まで保持し、結果の待機・回収前に解放する。`--resume-audio`もdispatch記録後に解放し、保存したreceiptで二重投入を防ぐ。

## 非公開workerで候補映像・横動画を続ける

`deploy/audio-stages.yml`は、非公開workerの`main`へ`.github/workflows/clipforge-stages.yml`として配置する原本。
投入先は`clipforge-stages.yml`・refは`main`。既存ASR用の`clipforge.yml`と併設する。
旧ブランチ`ccr-12fe9485-ugaszt`を記録したreceiptも、`--collect`では旧workflow・旧ブランチを照合して回収できる。
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

同一stage・前段run・選定内容・コードSHAは同一依頼IDになる。同じ依頼の二重dispatchや、応答不明の要求の自動再送は行わない。
生成物は非公開`clipforge-result-REQUEST_ID`artifactに7日保存する。公開側のActionsで実配信を処理しない。

完了後は、同じ依頼IDを新規の出力先へ回収する。既存の出力先は上書きせず、`request-receipt.json`と生成receiptに記録されたSHA-256・依頼ID・元コミットSHA・前段runを検査する。

```bash
uv run --no-sync python tools/submit_audio_stage.py --collect SUBMITTED_REQUEST_ID --output /private/stage-NEW_DIRECTORY
```

### 単一クリップ描画と制御VMでの結合

`--stage render --clip-index N`（またはColabの`--clips N`）の成果物は、描画完了後に`ranges/`・`audio.m4a`（取得receipt含む）・`previews/`・`transcripts/`・作業用topics/previews JSONを除去する。
`delivery/clips/clip-00N/`、両manifestと`asr-receipt.json`は残す。結合はASR本文でなく、そのreceiptのキーを分析の同一性確認に使う。
SHA-256一覧は除去後に作成し、manifestのクリップ別一覧も残ったクリップ内のファイルを指す。

```bash
uv run --no-sync python tools/submit_audio_stage.py --stage merge --local \
  --part-runs RUN1:REQUEST1,RUN2:REQUEST2,RUN3:REQUEST3 --output /private/merged-NEW_DIRECTORY
```

制御VMから各runのcompleted・success・`ClipForge REQUEST_ID`を確認してartifactを一度取得し、既存workerの`merge_parts`で検証・コピーする。GitHub runを起動せず、ffmpeg・フォントも使わない。
元コミットSHA・本数・前段依頼IDは検証済みpartから取得する。任意の`--source-ref REVIEWED_COMMIT_SHA`で元コミットの期待値も固定できる。
出力は従来mergeと同じ`delivery/`・manifest・request receiptで、`--collect`と同じ検証を通過してから新規ディレクトリへ確定する。ローカル結合のreceiptは`workflow_run: "local"`と記録する。
従来のGitHub上の結合は`--local`を付けず、`--previous-run`・`--previous-request`・`--source-ref`・`--part-runs`を指定して使える。

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
      "video_reviewed": true
    }
  ]
}
```

上は1区間の書式例。実行時は要求本数と一致する、異なる話題の非重複3〜5区間が必要。
時刻は配信全体の秒数で、対応する音声話題内に収める。
確認していない候補、別ASRの分析キー、改変されたプレビュー、重複した話題を拒否する。

```bash
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' --workspace inputs/private-vod \
  --stage render --review-file inputs/reviewed-topics.json
```

採用区間だけ最大1080pを取得し、全編ASRの単語時刻を区間内へ移す。区間ごとのASRは実行しない。
ASR単語や単語時刻のない発話の途中切断は推測で補わず拒否する。全採用区間の字幕境界を確認してから、高画質取得を開始する。
48kHz、全復号、音量・真ピーク、ASS/SRT一致等は既存strictゲートを使う。
strict通過は警告0件や人の読みやすさの承認を意味しない。
読み速度は目標7字/秒・警告7字/秒（2026-10-22、利用者の判断で4字/秒から変更）。4/7/10字超過数、実フォントでの行幅等を記録する。
共有用 `delivery/e2e-summary.json` は数値・件数とコード/設定/フォント等の識別情報のみ。詳細警告中の発話例や素材パスをコピーしない。

## アップロード失敗時の状態（再開）

- HTTP 401／403 のアップロード失敗だけを `UPLOAD_FAILED_AUTH`（認証の復旧が必要）と記録する。
- タイムアウトや 5xx などは `STAGING_PARTIAL`（再開可能）と記録する。同じ依頼IDで再開すると、チェックサム検査で不足分だけを確認する。dispatch は増やさない。
