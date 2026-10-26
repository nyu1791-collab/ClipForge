> 2026-10-26：実行入口は公開ClipForgeへ統合済みです。Colab認証Secretの登録先も`nyu1791-collab/ClipForge`です。`tools/configure_colab_oauth.py --login`の既定先を修正しました。非公開workerへの登録だけでは公開ClipForgeのActionsへ認証情報は渡りません。以下の非公開worker前提は旧手順です。

# ActionsからColabを自動起動

対象アカウントは **n.yu17919191@gmail.com** です。Google公式 [google-colab-cli](https://github.com/googlecolab/google-colab-cli) 0.7.5のSDKを使い、Actions実行時に専用ランタイムを作り、処理・結果回収後に解放します。Colab Enterpriseや別のGPUクラウドは作成しません。

## ジョブの流れ

| 用途 | ランタイム | 中身 |
| --- | --- | --- |
| 文字起こし（`asr`） | GPU。部分ごとに1台、L4 と T4 のどちらでも | Colab 標準の Python で NeMo と Parakeet。プロジェクトの環境は入れない（[ASR.md](ASR.md)） |
| 描画（`audio-stage`） | L4 → T4 high-ram → T4 standard。GPU/NVENC専用 | `tools/colab_setup.sh` で uv・Python 3.11・固定した依存を入れて描画 |

1. Actions の CPU 制御ジョブが Google OAuth のメールアドレスを照合する。指定ユーザー以外・サービスアカウントは、GPU を起動する前に拒否する。
2. 候補の順にランタイムを要求する。拒否（412）なら次の候補へ進み、全部拒否なら60秒おきに待つ（文字起こしは最大45分、描画は最大15分）。権限や通信の失敗では次の候補へ進まない。
3. 公開 ClipForge を指定 SHA で取得し、ジョブを実行する。
4. 結果をチェックサム付きで一時 Release に置き、ランタイムを解放してから回収する。
5. 成功・失敗にかかわらず所有するランタイムを解放し、一覧から消えたことを確かめる。他のノートブックのセッションは止めない。

Google の認証情報は制御ジョブにだけ置き、Colab には渡しません。Colab へは、ジョブに必要な GitHub の短期トークンを非公開ファイルで渡し、読み込み後に消します。文字起こしの依存導入と認識のプロセスにはトークンを渡しません。Pro 契約でも GPU の空き・計算ユニット残高・利用制限は別で、割り当てや処理時間を保証するものではありません。

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
