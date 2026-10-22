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

## 起動時のダウンロードを減らす

| アセット | 保存と再利用 |
| --- | --- |
| 制御用Google SDK | ハッシュ付き独立依存ロックとActionsのuvキャッシュ |
| Python 3.11、uv、CUDA/Python・OSパッケージ | ColabのOS・アーキテクチャ・依存ロックに合わせたアーカイブ |
| Whisperモデル | モデル専用キャッシュをローカル優先で参照し、存在すればHubへの取得要求を省略 |
| 同一セッションのツール | 導入済みツールを再利用。apt updateや重複するPython/uv導入を省略 |

自動起動ではキャッシュを非公開workerの `clipforge-cache-<互換性キー>` Releaseへ保存します。512MiB以下のチャンクに分割し、転送・復元時にSHA-256を照合します。既存キャッシュと同じ内容なら再アップロードしません。更新後の不要チャンクは、そのキャッシュRelease内だけで整理します。

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

Geminiを呼ばず、実ユーザーの動画も使いません。結果は非公開の `colab-verification-report` に保存します。この検証はASRの文字起こし品質や実動画での所要時間の保証ではありません。

`deploy/colab-integration.yml` はGPUを作らず、キャッシュ・転送・認証ガードのテスト、非公開GitHubへの実アップロード/ダウンロードと一時データ削除、合成動画のCPU切り抜きを確認します。

## 手動ノートブックと既存ランナーも利用可能

`notebooks/colab_gpu.ipynb` は手動実行用です。Driveをマウントして `MyDrive/ClipForge_Cache` を使えます。`PREPARE_ONLY=True` はCPUでのアセット事前準備、`STRICT_OFFLINE=True` は不足アセットの取得停止です。Driveマウントの転送・本人認証は発生します。

`--worker colab-gpu` は接続済みSelf-hosted Runnerを使う従来方式です。`COLAB_RUNNER_MODE=session` なら同じColabセッションで複数ジョブを受け付け、`ephemeral` なら1件で登録解除します。セルの `Listening for Jobs` とGitHubの `Idle` が揃って接続完了です。既存ランナー方式では停止したColabを自動作成しません。待機中のGPU枠消費と、切断後のOfflineランナー削除に注意してください。

直接処理する場合は `tools/colab_run.py` を使います。ローカル動画または直接動画ファイルのHTTPS URLに対応し、YouTube視聴ページ・HLSには対応しません。完了後Driveへコピーし、チェックサムを照合します。

## 根拠と確認状態

[調査した公式資料・ツール](COLAB_RESEARCH.md)。公式REST APIも存在しますが、2026-10-22時点でベータの許可制です。本構成は公開REST APIの許可制を迂回するコードを実装せず、Google公式CLIが提供する認証・割り当て・実行機能を使用します。

Google OAuthが未登録なら実GPUの接続・起動・推論は未確認です。コードテスト・CPU合成動画の成功を、GPU成功や実動画の品質確認と混同しません。
