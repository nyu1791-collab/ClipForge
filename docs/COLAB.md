# ActionsからColabを自動起動

対象アカウントは **n.yu17919191@gmail.com** です。Google公式 [google-colab-cli](https://github.com/googlecolab/google-colab-cli) 0.7.5のSDKを使い、Actions実行時に専用ランタイムを作り、処理・結果回収後に解放します。Colab Enterpriseや別のGPUクラウドは作成しません。

## 通常の動画ジョブ

既定のworkerは `colab-auto` です。Colabが担当する範囲は入力経路で異なります。

| 経路 | Colabの担当 | Gemini |
| --- | --- | --- |
| 対応メディアURL | runnerで取得済みの音声を既定2つのGPUランタイムで文字起こしし、解放。runnerで結合・話題分割し、映像入力なら同じrunのCPUジョブでpreviews・未承認の下書きを作る。映像確認・承認後はColab CPUで描画 | 呼ばず、キーも渡さない |
| ローカル動画 `submit_job.py --input` | `colab-auto` でASR・Gemini選定・描画を実行 | 候補映像・音声・該当ASRを指定モデルへ送る |

URL経路の段階と承認は [AUDIO_FIRST.md](AUDIO_FIRST.md)、結果の回収・再開・制御VMでの `merge --local` は [OPERATIONS.md](OPERATIONS.md) を参照してください。GPUのASRは逐次処理を使い、バッチ文字起こしは採用しません。

URLだけの投入は既定 `asr_parts=2` で、各半分の内部では従来の逐次デコーダーを使います。境界前後5秒の文脈を重ね、元時刻へ結合した後に話題分割します。`--asr-parts 1` で1つのランタイムによる連続ASRを選べます。[2分割ASR](COLAB.md#2分割asr)に識別・結合・失敗時の終了処理を記載しています。音声のみの直接URLでは映像の後段を指定しません。

自動起動の共通手順は次のとおりです。

1. ActionsのCPU制御ジョブがGoogle OAuthのメールアドレスを照合。指定ユーザー以外・サービスアカウントはGPUを起動する前に拒否します。
2. 指定ユーザーのColabに必要なランタイムを割り当て、公開ClipForgeを指定SHAで取得します。ASRはT4 high-ram → T4 standard、描画はCPU high-ram → CPU standardの順です。既定ではL4へ上げず、CPU描画をGPUへ切り替えません。候補を使い切ったら割り当て待ちの上限に従って停止します。別種の資源を使う場合は `CLIPFORGE_COLAB_POOL` の明示設定が必要です。
3. 必要な依存・モデルを準備し、経路に応じた処理を実行します。公開キャッシュは既定offです。制御ジョブにASRやCUDAパッケージは導入しません。
4. 結果をチェックサム付きで回収し、従来と同じ非公開Artifactsへ7日間保存します。動画・字幕の人による確認は別です。
5. 成功・失敗にかかわらず所有するColabを解放し、一覧から消えたことを確認します。他のノートブックのセッションは停止しません。

Googleへの認証を済ませた後は、ノートブックを開いたままにする必要はありません。Pro契約でもT4の空き・計算ユニット残高・利用制限は別で、割り当てや処理時間を保証するものではありません。GPUが拒否された場合は停止し、有料プラン購入やA100等への切替を行いません。

```bash
python tools/submit_job.py --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID --wait --output /path/to/new-result
```

CPUで実行する場合は `--worker cpu` を明示します。公開側の `Build package` はwheel作成だけなのでCPUのままです。

## 最初の1回だけ必要なGoogle OAuth

**スマホのブラウザでColabにログインしても、Actions用OAuthは登録されません。** 自動起動には、指定Googleユーザーで承認したADCを非公開workerのSecret **`COLAB_ADC_JSON`** に登録する必要があります。Google本人の認証・同意は、AIへの包括的な許可では代替できません。

認証したい端末で、Python 3.12以上と `gcloud`、所有者でログイン済みの `gh` を用意します。

```bash
uv venv /tmp/clipforge-colab-control --python 3.12
uv pip install --python /tmp/clipforge-colab-control/bin/python --require-hashes \
  -r tools/colab-control-requirements.txt
/tmp/clipforge-colab-control/bin/python tools/configure_colab_oauth.py --login
```

このコマンドはGoogleの本人用ログイン・同意を行い、メールアドレスを検査してからSecretを標準入力で登録します。トークンの値は表示せず、チャットやGitへ貼る必要はありません。既に正しいADCがある場合は `--login` を外します。必要スコープは公式CLIの案内に従い、`openid`、`cloud-platform`、`userinfo.email`、`colaboratory` です。サービスアカウントを使ってProユーザーになりすます方式ではありません。

環境変数で安全に渡した認証を調べるには次を使います。GPUは作成しません。GitHub Secretの値を読み戻すコマンドではありません。

```bash
/tmp/clipforge-colab-control/bin/python tools/colab_actions.py doctor
```

`COLAB_ADC_JSON` を実行環境へ安全に渡して使用します。未登録・期限切れ・同意取り消し時に、CIが対話ログインへ進むことはありません。

### スマホから認証コードを登録する場合

公開 `ClipForge` はソースコードの保存先です。Google認証情報と動画ジョブは非公開 `ai-model-workers` に置きます。そのため、コードの貼り付け先が `ClipForge` でなく `ai-model-workers` であるのは意図した構成です。

運用者がPKCE付きのGoogle公式SDK認証URLを発行し、対応する短期の検証情報を非公開workerの `COLAB_OAUTH_PENDING` に保存した場合は、スマホで次の操作ができます。

1. 発行された最新のURLで、指定Googleアカウントとして同意し、コードをコピー。
2. [非公開workerのNew repository secret](https://github.com/nyu1791-collab/ai-model-workers/settings/secrets/actions/new) で、Nameを `COLAB_AUTH_CODE`、Secretをコピーしたコードとして保存。
3. 運用者が `Complete private Colab Google authorization` の `mode=finish` を実行。

メールアドレスと必要スコープを確認して `COLAB_ADC_JSON` を登録し、一時コードと検証情報を削除します。コードやトークンをチャット・workflow入力・Git・Artifactsへ貼りません。新しいURLを発行した場合、前のURLのコードは使えません。期限切れなら新しい認証が必要です。

## 起動時のダウンロードを減らす

以下は公開キャッシュを明示的に有効にした場合の再利用対象です。既定offでは依存・モデルの取得が必要です。

| アセット | 保存と再利用 |
| --- | --- |
| 制御用Google SDK | ハッシュ付き独立依存ロックとActionsのuvキャッシュ |
| Python 3.11、uv、CUDA/Python・OSパッケージ | ColabのOS・アーキテクチャ・依存ロックに合わせたアーカイブ |
| Whisperモデル | モデル専用キャッシュをローカル優先で参照し、存在すればHubへの取得要求を省略 |
| 同一セッションのツール | 導入済みツールを再利用。apt updateや重複するPython/uv導入を省略 |

**公開キャッシュは既定off**（`CLIPFORGE_PUBLIC_CACHE=off`）です。offでは公開アセット用キャッシュの復元・保存を行いません。有効にした場合の保存先は非公開workerの `clipforge-cache-<互換性キー>` Releaseです。512MiB以下のチャンクに分割し、転送・復元時にSHA-256を照合します。既存キャッシュと同じ内容なら再アップロードしません。更新後の不要チャンクは、そのキャッシュRelease内だけで整理します。

依存スナップショット自体は展開しやすいtar、GitHubへの公開アセット転送は低圧縮率のgzipで通信量を減らします。元から圧縮済みのMP4成果物は再圧縮しません。古い未圧縮キャッシュの復元にも対応します。

Google OAuth情報、Colabのプロキシトークン、元動画、ASR、成果物を公開アセット用キャッシュへ含めません。Googleの認証情報は制御ジョブに保持し、Colabランタイムには渡しません。ジョブに必要なGitHubの短期トークンと、ローカル動画経路だけのGeminiキーを非公開ファイルで渡し、読み込み後にファイルを削除します。URL音声解析ではGeminiキーを要求・転送しません。依存導入プロセスにはこれらのキーを渡しません。

キャッシュoff、初回・依存変更・未保存モデルには取得が必要です。有効なキャッシュがあっても転送やモデル読み込みは残り、通信量・待ち時間が完全にゼロになる構成ではありません。

### GPUを起動する前にCPUで準備する

非公開workerへ `deploy/colab-maintenance.yml` を配置し、**Colab GPU verification and asset preparation** の `mode=prepare-cache` を実行できます。公開キャッシュを明示的に有効にした場合、CPUのColabで公開アセットと検証用Whisper `small` をキャッシュし、GPU・推論・Geminiを使わず終了します。次のT4ジョブもキャッシュを有効にし、互換性が合うものを復元します。CPU/GPUランタイムのOS等が違う場合は、互換性を優先して別のキーにします。

`CLIPFORGE_OFFLINE=1` は、セットアップの依存・モデルが不足しても自動取得せず停止する設定です。GitHub/Colabの制御通信・入力や結果の転送を止める設定ではありません。

## GPUでの実機検証

同じmaintenance workflowで `mode=verify-gpu` を選ぶと、専用T4を作成し、次を実行してから解放します。

- `nvidia-smi` で実際のGPU名・ドライバ・VRAMを取得。T4以外なら停止。
- CUDAドライバを初期化。
- Whisper `small` をCUDA/float16でロードし、VADを切って2秒の合成音声を実際に処理。
- NVENCの短い実行を試し、利用可否を記録。

キャッシュを有効にして初回保存後、同じ設定で `offline=true` にしてもう一度実行すると、新しいランタイムで依存パッケージとモデルを追加取得せずに復元・実行できるか確認できます。キャッシュのGitHubからの転送は発生します。不足アセットは自動取得せず、検証を失敗として扱います。

Geminiを呼ばず、実ユーザーの動画も使いません。結果は非公開の `colab-verification-report` に保存します。この検証はASRの文字起こし品質や実動画での所要時間の保証ではありません。

`deploy/colab-integration.yml` はGPUを作らず、キャッシュ・転送・認証ガードのテスト、非公開GitHubへの実アップロード/ダウンロードと一時データ削除、合成動画のCPU切り抜きを確認します。

## 根拠と確認状態

[調査した公式資料・ツール](HISTORY.md#colab取得依存の調査)。公式REST APIも存在しますが、2026-10-22時点でベータの許可制です。本構成は公開REST APIの許可制を迂回するコードを実装せず、Google公式CLIが提供する認証・割り当て・実行機能を使用します。

コードテスト・CPU合成動画の成功を、GPU成功や実動画の品質確認と混同しません。日付付きの実機検証と計測結果は [QUALITY_REVIEW.md](HISTORY.md#過去の品質規則と検証) を参照してください。

## 2分割ASR

URLだけの `tools/submit_job.py --audio-analysis-url URL` は既定で `asr_parts=2`。設定ファイルの `asr_parts` またはCLIの `--asr-parts 1` で連続ASRへ変更できる。2分割は `colab-auto` のURLだけの投入に対応し、ローカル動画やCPU実行では指定を拒否する。モデルとバッチ設定は変更しない。

### 処理

1. runnerが音声を取得し、サイズ・長さ・SHA-256を確認する。この段階ではGPUを割り当てない。
2. runnerで16kHz・モノラルPCMへ一度復号し、中央から±`asr_split_search_seconds` 秒（既定1800、0〜3600）の範囲で境界を決める（下記）。各半分に境界の前後5秒の文脈を加え、FLACで可逆圧縮する。
3. 親依頼IDと番号から2つの異なる子依頼IDを作り、非公開Releaseへ音声と依頼を保存する。
4. 独立した制御ディレクトリでColabを2つ起動し、それぞれCUDA/float16で担当部分だけ認識する。Geminiキーは渡さない。両処理を待ち、成功・失敗を問わず各所有ランタイムを解放する。
5. 両方の依頼ID・コードSHA・run ID・元音声SHA・部分音声SHA・成果物SHAを照合する。欠落・片側の失敗・異なる入力は結合しない。
6. 部分の単語時刻を元音声の時刻へ戻す。単語の中点が属する半分で一度だけ採用し、境界で重複した同じ単語を除く。異なる単語の曖昧な重なりや、境界をまたぐ単語時刻なしの発話は失敗とする。別言語が検出された場合も停止し、必要なら言語を明示して新規依頼する。
7. 結合済み全文をASRキャッシュへ保存し、再認識を禁止して話題分割を一度だけ実行する。通常と同じ解析artifact・回収・後段ASR再利用へ渡す。

2分割は文脈が変わるため、連続ASRと同じ文字起こしになる保証はない。キャッシュの識別子に分割数と分割実装を含め、両者を混同しない。境界が曖昧なら自動再実行せず停止する。必要に応じて `--asr-parts 1` で新規依頼する。

### 割り当てと記録

- 2つの部分は音声の長い方から順に割り当てる。各部分の候補は通常のGPU順（T4 high-ram → T4 standard）のまま。2026-10-22・23の実測ではT4 high-ramは同時に1台しか割り当てられず、2台目はT4 standardになった。high-ram（8コア）はstandard（2コア）より環境構築と入力準備が速い（実測で約37秒短い）ので、長い方の部分をhigh-ramに割り当てる。
- 各部分のログには `Runtime held:`（形状と保持秒数）と、`Colab stage:` 行の `host:`（コア数・メモリ）を出す。CU残高はアカウント全体で共有するため、部分ごとの差分は「account-wide」と表示し、その部分だけの使用量としては扱わない。合計は解放後に `Compute units used by both ASR parts:` として1回だけ出す。最後の解放時に他のランタイムが動いていれば `not determinable` とする。
- CPU描画（`audio-stages.yml` の `--mode audio-stage`）の候補は従来どおりCPU high-ramが最初。上の変更はGPUの2分割だけに関係する。

### 連続ASRとの比較

`tools/inspect_job.py compare BASE OTHER` は、2つの解析出力を数値だけで比べる（本文は表示しない）。比較対象は全体と分割点前後に分けて出す。出す項目は、話題数、境界のずれ（0.5・2・10・30秒以内の数、中央値、最大）、文字列の一致率、境界±60秒の時刻が重なる単語数、分割点をまたぐ話題、最初に食い違う文の時刻。`source_ref` が違えば警告する。分割の影響を測るには、同じコードSHAで `--asr-parts 1` と `--asr-parts 2` を流して比べる。2回目以降は `--reuse-audio-run RUN --reuse-audio-request ID` で検証済みの音声を再利用し、取得の約2分を省く。

2026-10-23の実測（Kickの4時間配信、14425秒）:

| | 連続（T4×1、8d9d98e） | 2分割（T4×2、7ffcaf5） |
|---|---|---|
| 文字起こし本体 | 612.6秒 | 290.2秒・270.8秒（high-ram・standard） |
| Colab工程 | 11分04秒 | 6分47秒 |
| run全体 | 16.5分 | 10.0分 |
| CU | 0.16 | 0.18 |
| 話題数 | 317 | 311 |

分割点は7394.8秒（10.4秒の無音の中）。分割点の前後で単語の欠落・重複は無かった。CPUのint8で同じ300秒を比べると、同じ入力の再実行と、後ろを切った音声の前半は完全に一致した。途中から始めた音声では、そこから先の区切りと表記が変わった（一致率93%）。したがって前半の差（98.1%）は分割によるものではなく、コードの版の違いかGPUの揺れ。後半の追加の差（95.7%）は、2つ目の部分が途中から認識を始めるためのもので、欠落ではない。基準はコードが異なるため、同じSHAでの比較が残っている。

2026-10-23の通し実行（`22b842b`、run 39948802288 と描画3本）: 分析とプレビューは14分08秒（音声取得281秒、2分割の工程426秒、プレビュー90秒）、描画3本は並列で4分55秒、`merge --local` は13秒。2分割のCUは0.11、描画は合計0.04。長い方の部分がT4 high-ramに割り当てられた。前回の2分割と同じコード・同じ音声・同じ文字起こしキーでも、文字の一致率は98.1%、話題数は311と304になった。前半の差はGPUの実行ごとの揺れで、分割によるものではないことが確認できた。

### 境界の決め方

- 中央からの範囲で、Silero VAD（faster-whisper同梱、onnxruntime必須）の発話の間（無音）のうち最も長いものの中点で切る。同じ長さなら中央に近い方。
- 範囲は両端から全体の1/4を残す位置までに内側へ切り詰める。VADが使えない・無音が無い・探索0秒は中央で切り、理由（`vad_unavailable`・`no_silence`・`vad_failed`・`search_disabled`）を記録する。
- 境界の方式・探索範囲・窓・無音区間・切断サンプルは部分の依頼IDと文字起こしキャッシュのキーに含まれ、同じ音声でも境界が違えば別キャッシュになる。経過秒と理由は結合後の `asr-receipt.json` の `split` に残す。

### 終了と途中成果物

同じworkerへの依頼はworkflowのconcurrencyで直列化し、1依頼内だけ最大2ランタイムを使う。各子処理の `finally` とworkflowの `always()` で、親・子の状態ファイルが所有するランタイムだけを停止する。子Releaseと親の一時Releaseも終了時に削除する。

失敗時の取得済み部分成果物は非公開 `clipforge-partial-REQUEST_ID` に7日保存する。途中の文字起こしを完成扱いにせず、新しい依頼IDでの自動再送や片側だけでの話題分割をしない。

URL取得は30分、分割・起動・認識・回収は110分、Colabジョブ全体は180分。割り当て可能数と速度はColabの利用状況に依存する。設定済みの同一アカウントの割り当て方式を使い、別アカウントへ切り替えない。

### 入力の追加経路

Kickは既存の取得器を使う。YouTube・Twitchと直接HTTPS mp4/m4a/mp3/webm/m3u8は `media_sources.py` のyt-dlp経路を使う。直接URLは署名クエリを含め元URLを同一性検証に保持し、ログには表示しない。

追加経路のHLSは終了済みのmedia playlist（ENDLISTあり）が必要。音声専用renditionを優先し、取得中も通信量の上限を監視する。直接ファイルや追加経路の描画では全体転送・全体remuxになる場合があり、Kickの区間取得と同じ通信量にはならない。音声専用URLには映像の段階を適用しない。
