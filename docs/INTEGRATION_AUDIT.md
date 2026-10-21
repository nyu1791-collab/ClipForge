# 配信自動取り込みの確認（2026-10-21）

「コード実装」「API認証成功」「本番稼働」は別の状態です。この変更はPR #7でレビュー可能です。

| 提案 | 確認結果 |
| --- | --- |
| Twitch配信終了通知 | online/offline購読CLI、署名・時刻検証、重複排除、SQLite保存は実装済み。本番HTTPS受信先と購読・実通知の確認は未完了 |
| 終了した配信のVOD特定 | `twitch-archive`を追加。配信者ID・stream_id・archive種別を照合し、照合前後に配信中でないことを確認。最新動画への推測は禁止 |
| チャットから候補絞り込み | 既存のリプレイ取得／JSON入力、5秒ごとの反応密度、LLM前の候補ランキングを確認。草・連続w・LOL等にWTF/KEKW/LUL/OMEGALULを追加 |
| 公式ライブチャット収集 | 未実装。Client ID/Secretだけでは完了せず、ユーザーのOAuth権限等が必要 |
| YouTube URL発見 | RSSからvideoIdと視聴URLを取得済み。既知IDをvideos.listで最大50件まとめて状態確認 |
| YouTube呼び出し削減 | RSSキャッシュ・条件付き取得、APIキャッシュ、ローカル日次予算、拒否時クールダウンは実装済み。WebSub受信・購読更新は未実装 |
| 動画の自動ダウンロード・ジョブ投入 | 未実装。視聴URLやVOD一覧の取得は動画ファイルの取得成功を意味しない。現在は許諾を得たローカル録画を既存ジョブに渡す |
| Twitch/Kick/YouTube認証 | リポジトリSecretsを用いた公式APIの認証成功を確認済み。ツイキャスは依頼に従って保留 |

## VOD照合の実行

```sh
clipforge-platforms twitch-archive BROADCASTER_ID STREAM_ID
```

`STREAM_ID`は対象の`stream.online`通知から取得してください。offline通知を受けただけで
「直近のonline＝終了した配信」と推測しないでください。通知の遅延・順序逆転があり得ます。
コマンドは1回の照合のみ行い、100件までのアーカイブを確認します。
`matching_archive_listed`は一致した公開メタデータであり、保存完了・動画取得を保証しません。
`channel_live` / `archive_not_resolved`では取得せず、再確認の目安300秒を返します。
自動再試行や常駐ワーカーは含みません。運用側で回数・期限を設定し、期限後は人が確認します。
配信保存が無効・削除済み・限定公開の場合や100件より古い場合は未解決のままにします。

## 画像の説明から修正した点

- Twitchの通知は動画の保存完了通知ではありません。IPブロックがないという保証もしません。
- YouTubeの現行公式クォータ表ではsearch.listは専用枠で標準100回/日、1回1単位、その他のAPIは合計10,000単位/日です。画像の100単位という説明は現行表と異なります。
- YouTube WebSubは投稿・タイトル・説明更新の通知です。配信終了の確実な通知として扱いません。
- APIのクォータと動画取得のボット判定は別です。Cookieのリポジトリ保存、住宅用プロキシや別アカウントへの切替で拒否を回避する処理は組み込みません。
- 安定した入力は配信者が許諾・提供したOBS等の元録画です。小容量VMは制御のみ、動画処理は既存のCPU runnerに渡します。

公式仕様：[Twitch動画](https://dev.twitch.tv/docs/api/reference/#get-videos)、
[EventSub・チャット権限](https://dev.twitch.tv/docs/eventsub/eventsub-subscription-types/)、
[YouTubeクォータ](https://developers.google.com/youtube/data/determine_quota_cost)、
[YouTube通知](https://developers.google.com/youtube/data/guides/push_notifications)。

## 本番取り込みに残る設定

HTTPS受信先と常駐プロセス、対象配信者・許諾、元録画の受け渡し先、再試行期限と監視を設定し、
実際の配信開始→終了→対象VOD照合→録画ジョブ投入を一連で確認する必要があります。
Kickは公式API認証済みですが、ポータルのWebhookは画像では無効で、本番通知は未確認です。
