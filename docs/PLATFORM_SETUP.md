# 配信APIの登録とTwitchからの導入

対象リポジトリは `nyu1791-collab/ClipForge`。公開コードにはキーを入れず、
GitHub Actions Secretsへ保存します。VMは発行・登録操作だけを担当し、新しいキーの
永続ファイルをVMへ残しません。`.env.example` は必要な**名前だけ**の一覧です。
空欄を置いただけでは登録完了になりません。

## 発行先と保存するSecret名

| 優先 | サービス・発行先 | Secret名 | この実装で利用する機能 |
| --- | --- | --- | --- |
| 1 | [Twitch Developer Console](https://dev.twitch.tv/console/apps) | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_EVENTSUB_SECRET` | Client Credentials、VOD一覧、配信開始・終了EventSub |
| 2 | [Kick Developer Portal](https://kick.com/settings/developer) | `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` | 公式OAuth、`api.kick.com/public/v1/channels`、配信状態Webhook |
| 3 | [ツイキャス開発者登録](https://twitcasting.tv/developer.php) | `TWITCASTING_CLIENT_ID`, `TWITCASTING_CLIENT_SECRET` | 公式APIの録画一覧、開始・終了Webhook登録 |
| 4 | [Google Cloud Credentials](https://console.cloud.google.com/apis/credentials) | `YOUTUBE_API_KEY` | RSSで見つけたIDのData APIメタデータ確認 |

Twitch/KickのClient IDとClient Secretは、アプリを登録して発行するOAuth認証情報です。
一律の「APIキー」ではありません。Twitchの2FA、Kickの本人確認、各サイトのログインや
利用条件への同意が求められる場合は、そのアカウントの所有者による操作が必要です。
配信キー（stream key）は取得・保存しません。

## Twitchを最初に設定する

1. Developer Consoleで `ClipForge` アプリを作成。実際に使用するサイトとOAuth redirectを登録。
   今回のサーバー間Client Credentialsは、ユーザーのログイントークンを使用しません。
2. Client ID/SecretをClipForgeのGitHub Secretsへ登録。Webhook署名用のランダム値は
   下記の生成コマンドでメモリ内で作り、`TWITCH_EVENTSUB_SECRET` へ直接登録できます。
3. 下の軽量受信プロセスを常設VMで動かし、HTTPSの443番を持つリバースプロキシから転送。
   プロキシにはボディ上限、接続数・リクエスト数の制限を設定。
4. 受信が可能になってから `subscribe`。同じ配信者・イベント・callbackの有効な購読は再利用。

```bash
clipforge-platforms credentials
clipforge-platforms --state /private/state/platforms.sqlite3 serve
clipforge-platforms discover twitch CHANNEL_LOGIN
clipforge-platforms subscribe twitch BROADCASTER_ID \
  --callback-url https://YOUR_HTTPS_HOST/webhooks/twitch
clipforge-platforms events --after 0 --limit 100
```

`credentials` は環境変数の存在確認であり、認証成功の確認ではありません。
`discover` の実応答、購読statusの `enabled`、実際の通知受信をそれぞれ確認します。
`serve` はデフォルトで127.0.0.1:8787。GitHub Actionsの一時runnerやGitHub Pagesは
常設Webhook受信先に使いません。

開始通知では `stream_id` と `started_at` を記録します。終了通知の後にVOD一覧を取得する際は、
その `stream_id` に一致するVODを選びます。単に「最新のVOD」を切り抜くと、前回配信を
誤処理する可能性があります。再開配信・順序逆転を考慮し、実行前にGet Streamsで状態を再確認。
VOD保存無効、削除、公開前、準備遅延もあるため、終了通知を「録画取得完了」と扱いません。
取得間隔の目安は終了後2、5、10、20分、最大4回。次回実行時刻をジョブ側で管理します。

今回の実装は**通知の保存とURL・メタデータの取得まで**です。自動VOD解決、ダウンロード、
動画ジョブの自動投入はまだ行いません。配信者提供のOBS録画等を既存の `clipforge-submit`
へ渡して切り抜きます。これで小容量の制御VMにASRやレンダリングを載せずに進められます。

## Kick公式API

[App Setup](https://docs.kick.com/getting-started/kick-apps-setup)に従いアプリ登録。
Portal側でWebhookを有効にし、実在するHTTPS callbackを `/webhooks/kick` に設定します。
公式の `id.kick.com/oauth/token` でApp Access Tokenを取得し、
`api.kick.com/public/v1/events/subscriptions` で `livestream.status.updated` v1を登録。

```bash
clipforge-platforms discover kick CHANNEL_SLUG
clipforge-platforms subscribe kick BROADCASTER_USER_ID
```

署名はKickの公式公開鍵エンドポイントから取得した鍵でRSA/SHA-256検証します。
公開鍵は1時間キャッシュ。無効な署名で無制限の鍵再取得は起こしません。
鍵ローテーション時はキャッシュ更新後に通知再送・購読状態を確認します。
登録にユーザー認可を要求された場合は、公式OAuth 2.1と必要scope `events:subscribe` を確認。
このCLIはApp Access Tokenを使い、ユーザー認可フローやscopeの自動拡張を実装しません。

Kickの配信終了通知にVODダウンロードURLが含まれるとは仮定しません。
非公開の `/api/v1`・`/api/v2` を使った取得や、IPブロックの弱さを前提にした実装はありません。

## ツイキャス

開発者登録後、アプリのClient ID/SecretとWebhook URLを設定。
`X-Api-Version: 2.0` と公式Basic認証で利用します。

```bash
clipforge-platforms discover twitcasting USER_SCREEN_ID
clipforge-platforms subscribe twitcasting NUMERIC_USER_ID
```

`livestart` / `liveend` の登録APIに対応。**この受信サーバーはツイキャスの受信には未対応**です。
署名キーの検証と再送対策を備えた別の受信先をPortalに登録してから購読してください。
今回の常設受信はTwitch/Kickが対象です。録画の `is_recorded`、公開範囲、権限を確認します。

## Secretsへの保存

発行した認証情報を登録プロセスのメモリ内に渡し、次を実行。
値は標準入力から `gh secret set` に渡し、引数・ログ・コミットには含めません。

```bash
python3 tools/register_platform_secrets.py
python3 tools/register_platform_secrets.py --apply \
  --names TWITCH_CLIENT_ID TWITCH_CLIENT_SECRET TWITCH_EVENTSUB_SECRET
python3 tools/register_platform_secrets.py --apply --generate-eventsub-secret \
  --names TWITCH_EVENTSUB_SECRET
```

最初のコマンドは登録予定の名前と不足だけ表示。`--apply` で実登録。
権限拒否では停止し、別アカウントへの自動切替を行いません。
`gh secret list --repo nyu1791-collab/ClipForge --json name` で名前を確認できます。
GitHub SecretsはVMへ逆方向に配布されません。新しいキーはVMの永続ファイルへ保存しません。
常設監視を実際に配備する際には、実行先へSecretを注入する経路を別途設定します。
公開のwheel作成にこれらのキーは不要で、workflowには渡しません。

Actionsの `Platform API metadata` workflowは、Secretsをその実行ステップの環境へ注入し、
`credentials` で名前と存在だけ、`discover` で公開メタデータ、`youtube-videos` で指定IDの
状態を確認できます。キーをVMへ戻さず確認するための手動実行です。
依存モデルを導入せず、動画処理も行いません。状態DBはrunner終了で消えるため、
定期監視ではなく単発の確認用です。繰り返し起動してプロジェクトの割当を消費しないでください。

## 1人目の顧客に向けた運用

最初は許諾を得たTwitch配信者1人・1配信・30〜45秒の1本に限定。
OBS録画と、必要なら同意を得たチャット時刻データを受け取り、候補選定と字幕の見直しを行います。
声と字幕の同期、誤字、顔の切れ、見出し、SNS表示領域を実機で確認し、配信者に私的に提示。
配信者への連絡やSNS投稿は別の操作で、現在は自動送信しません。

仕様の確認先：[Twitch EventSub](https://dev.twitch.tv/docs/eventsub/handling-webhook-events/)、
[Kick公式ドキュメント](https://docs.kick.com/)、[ツイキャスAPI](https://apiv2-doc.twitcasting.tv/)。

## 2026-10-21の登録・確認結果

保存先は `nyu1791-collab/ClipForge` のGitHub Actions Secretsです。
オンラインの `dc-control-node` から所有者アカウントで確認しました。

| 名前 | 結果 |
| --- | --- |
| `YOUTUBE_API_KEY` | 既存登録を保持。リポジトリのSecretを注入したActionsでData APIの認証成功を確認 |
| `TWITCH_EVENTSUB_SECRET` | メモリ内で新規生成し、リポジトリのSecretへ直接登録。VMへのキーファイル作成なし |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | 未発行・未登録。VMのブラウザ起動がタイムアウトし、ログイン済みプロファイルも確認できず |
| `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET` | 未発行・未登録。VMからDeveloper PortalへHTTP403 |
| `TWITCASTING_CLIENT_ID`, `TWITCASTING_CLIENT_SECRET` | 未発行・未登録。VMから開発者ページへHTTP503 |

確認用の[Actions実行](https://github.com/nyu1791-collab/ClipForge/actions/runs/39634142932)は成功。
YouTubeの実リクエストは1回・1動画IDで、キーの値をログへ出していません。
署名用Secretの登録はTwitchアプリの発行やEventSubの本番受信成功を意味しません。
残り3社はログイン・開発者ページへのアクセスを確立してから発行・登録を再開します。
