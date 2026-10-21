# YouTubeのアクセスを抑えて運用する

2026-10-21に[公式クォータ表](https://developers.google.com/youtube/data/determine_quota_cost)を確認。
現行表では `search.list` は専用bucketで標準100呼び出し/日、その他のAPIは合計10,000unit/日と
記載されています。「search.listは必ず100unit」という古い説明を固定値にしません。
実際の割当・制限は所属プロジェクトのCloud Consoleを優先します。

## 実装した経路

1. `discover youtube UC...` で公式RSSから動画IDを取得。Data APIキーは不要。
2. RSSを最低5分キャッシュ。ETag/Last-Modifiedが返れば条件付きリクエストも利用。
3. 必要なIDだけ `youtube-videos` へ渡す。`videos.list` を最大50件まとめて呼び、5分キャッシュ。
4. `liveStreamingDetails.actualEndTime` がある場合だけ `ended` と表示。
5. SQLiteに試行回数を先に記録。標準1,000unit/日のローカル予算を超えると送信を止める。
6. `quotaExceeded` / `dailyLimitExceeded` は太平洋時間の翌0時まで停止。
   HTTP429はRetry-After、5xxは60秒の待機を記録。処理内で無限再試行しない。

```bash
clipforge-platforms discover youtube UC_CHANNEL_ID_OF_24_CHARACTERS
clipforge-platforms youtube-videos VIDEO_ID_001 VIDEO_ID_002 --daily-budget 1000
```

チャンネルIDは `UC` で始まる24文字、動画IDは11文字。例は実際のIDへ置換してください。
状態DBは再起動後も同じものを使います。複数の監視プロセスも同じDBを共有してください。
別のツールが同じCloudプロジェクトで消費したquotaはこのDBでは分からないため、
Cloud Consoleでも使用量を確認します。APIキーの増設やプロジェクト分割で制限を回避しません。

## RSS・WebSubの限界

RSSは最近の動画だけで、長期停止中に見逃した全履歴を保証しません。
[公式Push通知](https://developers.google.com/youtube/data/guides/push_notifications)の通知対象は
アップロード・タイトル変更・説明変更です。配信終了の完全検知を保証するものではありません。
この版にはYouTubeのWebSub受信・購読更新は含みません。RSS＋必要時の状態確認が利用可能です。
導入時には署名検証、topic照合、lease更新を用意してからWebSubへ移行します。
Googleの通知が来なければ未発見動画の終了も分からない点を運用で補います。

APIから得られる `https://www.youtube.com/watch?v=VIDEO_ID` は視聴URLです。
動画ファイルやダウンロード権限を取得するAPIではありません。

## キー発行

Google Cloudで対象プロジェクトを選択し、YouTube Data API v3を有効化してキーを発行。
API制限をYouTube Data API v3に絞り、実際のサーバー環境に適したアプリケーション制限を設定。
新規作成の前に既存の保護された `YOUTUBE_API_KEY` が使えるか確認します。
値はClipForgeのGitHub Actions Secretsへ保存。新しく発行するキーをVMのファイルへ保存しません。
`Platform API metadata` workflowでSecretsを実行ステップへ注入して確認できます。
このクライアントはキーをURLクエリに載せず `X-Goog-Api-Key` ヘッダーで送ります。

## Bot判定と素材の取り込み

Bot確認は動画本体の取得側で発生することがあり、Data APIのクォータとは別です。
クラウドIP、送信頻度、セッション、動画の公開範囲などを確認します。
「特定クラウドなら必ず拒否」「Cookieや住宅用IPなら必ず成功」とは判断できません。

安定した素材の入口は、配信者のOBS同時録画、配信者が正規機能で取得したファイル、
配信者から直接受領した編集用マスターです。ローカル素材を既存の非公開workerへ投入します。
この実装はyt-dlp、住宅用プロキシ、Cookieの自動輸出入、認証済み別アカウントへの切替を使いません。
アクセス拒否時にはその取得を停止し、配信者提供素材を使用します。

視聴URL発見、認証成功、素材入手、切り抜き生成、人による内容確認を別々に記録してください。
アクセス制限が一切来ないことや、プラットフォームごとのダウンロード成功は保証できません。
