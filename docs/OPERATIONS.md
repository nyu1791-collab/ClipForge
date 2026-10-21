# GitHub・AI運用

## リポジトリの権限

リポジトリはpublic。Actionsを有効化し、既定のworkflow tokenを書き込み可能にする。
ActionsからPRを作成・承認できる設定も有効化する。
`Build package` のjobは `contents: write`、`pull-requests: write`、`issues: write` を宣言する。
権限は実行中の `ClipForge` に対するもので、GitHub全アカウントや他社サービスの全権限ではない。
AIという共通ユーザーへ権限を付ける仕組みではなく、各エージェントが利用する認証に権限が付く。
GitHubの仕様上、botが自身のPRを承認できるとは限らない。
また通常の `GITHUB_TOKEN` によるpushやPR作成では、別のworkflowは自動起動しない。
必要な処理は `workflow_dispatch` か、所有者が認証済みの制御VMから起動する。

初期状態で必須レビューやブランチ保護は追加しない。自動マージは機能として有効にするが、
AIやワークフローが勝手にPRをマージする処理は今回追加しない。

## クライアント設定

- Claude Code: プロジェクトを信頼した場合、ファイル操作とBash実行を事前許可する。
- Codex CLI: 作業ディレクトリ内の書き込みとネットワークを許可し、個々のコマンドの承認要求を省く。
- 上位の管理設定、サービス側の安全判定、OAuth本人確認、利用枠はこの設定では変更されない。
- 他の利用者や他のリポジトリの設定を変更しない。

## APIキー

制御VMの既存 `github-api-sync` が所有リポジトリを検出し、次のキーをActions Secretsへ登録する。

- `GEMINI_API_KEY`
- `YOUTUBE_API_KEY`
- `ANTIGRAVITY_GEMINI_API_KEY`
- `DEEPSEEK_API_KEY`
- `FIRECRAWL_API_KEY`
- `OPENCODE_API_KEY`
- `OPENROUTER_API_KEY`
- `TEMPORAL_CLOUD_API_KEY`

新規リポジトリの検出はVM稼働中の15分間隔。値の更新元はVMの保護された保存先。
GitHub Secretsから値を読み戻すことはできない。新しいキーをリポジトリ側に追加しただけでは、
他のリポジトリへ自動輸入されない。

Secretsは必要なステップにだけ明示的に渡す。公開側のビルドworkflowはAPIキーを使用しない。
Claude・AntigravityのOAuth情報は `ai-model-workers` の非公開Secretsに置く。

Twitch/Kick/ツイキャスのOAuth認証情報は、この既存同期の対象として登録済みとは扱いません。
新しい発行・保存先・登録コマンドは [配信API設定](PLATFORM_SETUP.md) を参照。
YouTubeは [クォータと取得経路](YOUTUBE_OPERATIONS.md) を参照。

## 手動ビルド

Actions → `Build package` → Run workflow。
開発用wheelだけを7日間保存し、動画生成・SNS投稿・PyPI公開・有料サービス購入は行わない。
これはパッケージ作成であり、切り抜きの動作確認やモデル呼び出しの確認ではない。

## 動画ジョブの実行場所

`deploy/clipforge.yml` を非公開 `nyu1791-collab/ai-model-workers/.github/workflows/clipforge.yml` に配置します。
既存のモデルworkerとは別ワークフローです。公開ClipForge側に動画用workflowを追加しません。
元コードは指定されたClipForgeの40桁SHAで取得し、`uv.lock` の依存をPython 3.11へ導入します。

制御VMには `~/.local/bin/clipforge-submit` を置き、`tools/submit_job.py` を起動します。
標準ライブラリと認証済みの `gh` だけを使うので、VMにWhisperやFFmpegを導入しません。
最初の実行には、VMから読める入力動画と動画対応GeminiモデルのAPI識別子が必要です。

```bash
clipforge-submit --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID \
  --config /home/n_yu1791/repos/ClipForge/config.example.json \
  --wait --output /home/n_yu1791/clipforge-results/job-001
```

モデルを指定した動画ジョブで、候補動画のGoogleへの送信とそのモデルのAPI利用を行います。
別のモデルには切り替えません。キーが登録済みでも無料tierや十分な割当を保証しません。
GitHubの非公開Actionsの実行時間・Artifacts・Cacheにも契約の無料枠が適用されます。
1ジョブの実行上限は90分、CPUのみ。サーバー増設や有料プラン購入は行いません。

## 非公開データの経路と保管

1. VMが入力SHA-256と設定を記録し、非公開workerの一時Releaseへ動画・依頼JSON・任意のチャットJSONを送信。
2. runnerが非公開であること、リクエストID、コードSHA、入力サイズ、SHA-256を検査。
3. 元動画はrunner内で処理。Googleには選定候補の低解像度動画・その音声・該当するASR文だけを送信。
4. 送信したGoogle Filesは処理後に削除を試行。削除失敗は成功したふりをせず記録・通知します。
5. 処理結果のMP4・字幕・文字起こし・JSONは非公開Artifactsに7日間保存。`--wait` 指定時はVMへ回収します。
6. 入力用の非公開Releaseとタグは終了時に削除を試行。checkoutや依存導入が失敗した場合などは手動削除が必要です。

段階キャッシュには文字起こし・チャットの時刻/重み・候補・選定JSONを含みます。
原動画・MP4・APIキー・Cookie・OAuth情報はキャッシュしません。キャッシュの保管はGitHubの保持・容量制限によります。
キャッシュはAPI呼び出しが確実に1回だけであることを保証する仕組みではありません。
モデル応答後・キャッシュ保存前にrunnerが失われたときは、記録を確認してから再依頼します。

## 結果の回収と失敗時

依頼記録は `~/.local/state/clipforge/<request_id>/receipt.json`。
`STAGING` / `STAGED` / `DISPATCHED` / `RUNNING` / `COMPLETE` / `FAILED` / `UNCONFIRMED` を区別します。
依頼APIの応答が不明な場合は自動再送せず、Actions一覧を確認します。

`--wait` を使わなかった場合やSSHが切れた場合は、記録にあるrun IDで回収できます。

```bash
gh run view RUN_ID --repo nyu1791-collab/ai-model-workers
gh run download RUN_ID --repo nyu1791-collab/ai-model-workers \
  --name clipforge-result-REQUEST_ID --dir /path/to/new-result-directory
```

`request-receipt.json` のrequest IDと、`manifest.json` の `status=complete` を確認します。
各クリップの `render.json` に記録したSHA-256と実ファイルを照合できます。
Actions成功・生成完了と、人による内容・字幕の確認、YouTubeへの公開は別です。

同時に依頼できる動画ジョブは1件です。VM入口は処理中のジョブがあれば新規依頼を止めます。
Actionsも同時実行1件で、待機ジョブを何件も蓄積する用途には使いません。

入力は処理後に削除されるため、Actionsの「Re-run jobs」だけでは再取得できません。
失敗内容を確認し、必要なら同じ動画・設定・コードを新しい依頼として送信します。
段階キャッシュが残っていれば文字起こしや完了済み選定を再利用します。
モデル拒否や割当超過に対する自動再試行・別アカウントや別サービスによる回避は行いません。

一時入力が残っている場合は、依頼記録のタグを確認して削除します。

```bash
clipforge-submit --cleanup-tag clipforge-input-REQUEST_ID
```

## チャットと人による修正

`--chat-file` は次の形式の配列です。元動画の0秒に合わせて時刻を指定します。
`chat_offset_seconds` はチャットの時刻へ加算されます。

```json
[
  {"time_in_seconds": 130.5, "message": "草"},
  {"time_in_seconds": 131.0, "message": "すごい"}
]
```

チャットは任意です。公開リプレイが取得できない場合は、その状態を記録して場面転換とASRで候補を作ります。
候補データにはユーザー名やチャット本文を残さず、時刻と反応の重みだけを保存します。

ローカルで区間を修正する場合は、元動画秒数の `selection.json` を別ファイルへ複製して `clips` を編集し、
`clipforge run ... --selection-file edited-selection.json --output NEW_DIRECTORY` を使えます。
各clipには `start`、`end`、`candidate_id`、`title`、`reason`、0～1の `confidence` が必要です。
この指定ではGeminiを呼びません。既存の生成物とは別の出力先を使います。

## 初期実装の確認範囲

依存解決、ソースの構文・静的検査、wheel作成とGitHub/VMへの配置を確認します。
ユーザーからテスト実行の依頼がないため、テストの追加・実行はしません。
動画未提供のため、モデル呼び出し・文字起こし・実動画レンダリングも未実施です。
