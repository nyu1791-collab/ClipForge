# ActionsからColab Pro T4を自動起動

対象アカウントは **n.yu17919191@gmail.com** です。Google公式 [google-colab-cli](https://github.com/googlecolab/google-colab-cli) 0.7.5のSDKを使い、Actions実行時に専用T4を作り、処理・結果回収後に解放します。Colab Enterpriseや別のGPUクラウドは作成しません。

## 通常の動画ジョブ

既定の `worker=colab-auto` は次の順で動きます。

1. ActionsのCPU制御ジョブがGoogle OAuthのメールアドレスを照合。指定ユーザー以外・サービスアカウントはGPUを起動する前に拒否します。
2. 指定ユーザーのColabにT4を割り当て、公開ClipForgeを指定SHAで取得します。別GPUや別アカウントへは切り替えません。
3. 非公開GitHubから依存・モデルのキャッシュを復元し、ASR/CUDAとレンダリングをColabで実行します。制御ジョブにASRやCUDAパッケージは導入しません。
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

| アセット | 保存と再利用 |
| --- | --- |
| 制御用Google SDK | ハッシュ付き独立依存ロックとActionsのuvキャッシュ |
| Python 3.11、uv、CUDA/Python・OSパッケージ | ColabのOS・アーキテクチャ・依存ロックに合わせたアーカイブ |
| Whisperモデル | モデル専用キャッシュをローカル優先で参照し、存在すればHubへの取得要求を省略 |
| 同一セッションのツール | 導入済みツールを再利用。apt updateや重複するPython/uv導入を省略 |

自動起動ではキャッシュを非公開workerの `clipforge-cache-<互換性キー>` Releaseへ保存します。512MiB以下のチャンクに分割し、転送・復元時にSHA-256を照合します。既存キャッシュと同じ内容なら再アップロードしません。更新後の不要チャンクは、そのキャッシュRelease内だけで整理します。

依存スナップショット自体は展開しやすいtar、GitHubへの公開アセット転送は低圧縮率のgzipで通信量を減らします。元から圧縮済みのMP4成果物は再圧縮しません。古い未圧縮キャッシュの復元にも対応します。

Google OAuth情報、Colabのプロキシトークン、Runnerの登録情報、元動画、ASR、成果物を公開アセット用キャッシュへ含めません。Googleの認証情報は制御ジョブに保持し、GPUには渡しません。GPUへはジョブに必要なGitHubの短期トークンとGeminiキーだけを非公開ファイルで渡し、読み込み後にファイルを削除します。依存導入プロセスにはこれらのキーを渡しません。

初回・依存変更・未保存モデルには取得が必要です。キャッシュからの転送やモデル読み込みは残るため、通信量・待ち時間が完全にゼロになる構成ではありません。自動実行ワーカーではSelf-hosted Runnerのアーカイブ自体が不要です。

### GPUを起動する前にCPUで準備する

非公開workerへ `deploy/colab-maintenance.yml` を配置し、**Colab GPU verification and asset preparation** の `mode=prepare-cache` を実行できます。CPUのColabで公開アセットと既定のWhisper `small` をキャッシュし、GPU・推論・Geminiを使わず終了します。次のT4ジョブは互換性が合うキャッシュを復元します。CPU/GPUランタイムのOS等が違う場合は、互換性を優先して別のキーにします。

`CLIPFORGE_OFFLINE=1` は、セットアップの依存・モデル・Runnerアーカイブが不足しても自動取得せず停止する設定です。GitHub/Colabの制御通信・入力や結果の転送を止める設定ではありません。

## GPUでの実機検証

同じmaintenance workflowで `mode=verify-gpu` を選ぶと、専用T4を作成し、次を実行してから解放します。

- `nvidia-smi` で実際のGPU名・ドライバ・VRAMを取得。T4以外なら停止。
- CUDAドライバを初期化。
- Whisper `small` をCUDA/float16でロードし、VADを切って2秒の合成音声を実際に処理。
- NVENCの短い実行を試し、利用可否を記録。

初回キャッシュ保存後、`offline=true` でもう一度実行すると、新しいランタイムで依存パッケージとモデルを追加取得せずに復元・実行できるか確認できます。キャッシュのGitHubからの転送は発生します。不足アセットは自動取得せず、検証を失敗として扱います。

Geminiを呼ばず、実ユーザーの動画も使いません。結果は非公開の `colab-verification-report` に保存します。この検証はASRの文字起こし品質や実動画での所要時間の保証ではありません。

`deploy/colab-integration.yml` はGPUを作らず、キャッシュ・転送・認証ガードのテスト、非公開GitHubへの実アップロード/ダウンロードと一時データ削除、合成動画のCPU切り抜きを確認します。

## 手動ノートブックと既存ランナーも利用可能

`notebooks/colab_gpu.ipynb` は手動実行用です。Driveをマウントして `MyDrive/ClipForge_Cache` を使えます。`PREPARE_ONLY=True` はCPUでのアセット事前準備、`STRICT_OFFLINE=True` は不足アセットの取得停止です。Driveマウントの転送・本人認証は発生します。

`--worker colab-gpu` は接続済みSelf-hosted Runnerを使う従来方式です。`COLAB_RUNNER_MODE=session` なら同じColabセッションで複数ジョブを受け付け、`ephemeral` なら1件で登録解除します。セルの `Listening for Jobs` とGitHubの `Idle` が揃って接続完了です。既存ランナー方式では停止したColabを自動作成しません。待機中のGPU枠消費と、切断後のOfflineランナー削除に注意してください。

直接処理する場合は `tools/colab_run.py` を使います。ローカル動画または直接動画ファイルのHTTPS URLに対応し、YouTube視聴ページ・HLSには対応しません。完了後Driveへコピーし、チェックサムを照合します。

## 根拠と確認状態

[調査した公式資料・ツール](COLAB_RESEARCH.md)。公式REST APIも存在しますが、2026-10-22時点でベータの許可制です。本構成は公開REST APIの許可制を迂回するコードを実装せず、Google公式CLIが提供する認証・割り当て・実行機能を使用します。

Google OAuthが未登録なら実GPUの接続・起動・推論は未確認です。コードテスト・CPU合成動画の成功を、GPU成功や実動画の品質確認と混同しません。

2026-10-22の[初回実機検証](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39763967607)では、指定アカウントで **Tesla T4 / VRAM 15360MiB / driver 580.82.07** を割り当て、Whisper `small` のCUDA/float16処理、依存・モデルのキャッシュ保存、専用ランタイムの解放確認が成功しました。合成音声による検証であり、実動画のASR品質・速度は別です。初回NVENC試験は検出映像の幅がTuringの最小幅を下回っていたため、320×240へ修正して再検証します。
