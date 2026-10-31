# autopilot（配信監視 → 制作 → 投稿 → 分析）

配信が終わると Discord に「制作する？」が届き、押すと ClipForge の制作が始まる。完成すると YouTube に非公開で上がり、「公開する？」を押すと空いている時間枠へ予約される。公開されたら X に投稿し、毎日の視聴データを次の話題選定へ戻す。

```
毎時  watch      新しい配信を確認 → 承認待ち（approve）／自動開始（auto）、公開済み動画の X 投稿
押下  decide     承認ボタン → 制作開始・公開・見送り・再開
随時  produce    ClipForge で制作 → publish（YouTube・TikTok下書き）
毎日  analytics  視聴データを取り込み、話題選定へのメモを更新
毎週  report     振り返りを Discord へ
```

- 他人の配信を扱うチャンネルは `mode: approve` / `publish_mode: approve` を既定にする。自分のチャンネルだけ `auto` にしてよい。切り抜きガイドラインで許可された配信者だけを登録し、`credit` に元配信の表記を書く。
- 同じ配信から制作が2回始まることはない（状態を保存してから制作する）。失敗後の「もう一度」は保存済みの制作へ再接続するだけで、作り直さない。
- 設定・鍵・状態はすべて Secrets と Variables にあり、公開リポジトリには出ない。

## 1. 必要な登録（あなたの操作が必要な部分）

### YouTube（投稿・分析）
1. [Google Cloud Console](https://console.cloud.google.com/) で使うプロジェクトを選び、「APIとサービス → ライブラリ」で **YouTube Data API v3** と **YouTube Analytics API** を有効にする。
2. 「OAuth 同意画面」: ユーザーの種類は外部、アプリ名とメールを入れる。**公開ステータスを「本番環境」にする**（「テスト」のままだと refresh token が7日で失効する）。未確認アプリの警告は自分だけで使う分には進めてよい。
3. 「認証情報 → OAuth クライアント ID」: 種類 **デスクトップ アプリ**。クライアントIDとシークレットを控える。
4. 手元のPCで（ブラウザが開く）:
   ```sh
   uv sync --extra autopilot
   uv run python -m clipforge.autopilot auth-youtube --client-id ID --client-secret SECRET
   gh secret set YOUTUBE_REFRESH_TOKEN --repo nyu1791-collab/ClipForge < ~/youtube-refresh-token.txt
   rm ~/youtube-refresh-token.txt
   ```
   許可するときは、**投稿先チャンネルのアカウント**を選ぶ。
5. 注意:
   - **API 監査に通るまで、API でのアップロードは強制的に非公開**になる。公開して運用するには「YouTube API Services - Audit and Quota Extension」フォームで申請する。
   - 既定の割り当ては1日10,000ユニットで、アップロードは1本1,600ユニット（**1日およそ6本まで**）。本数が多いなら同じフォームで増枠を申請する。
   - 監視だけなら API キー（`YOUTUBE_API_KEY`）でもよい。無い場合は OAuth を使い、どちらも無ければ公開から8時間たった動画だけを拾う（配信中を拾わないため）。

### X（投稿）
1. [X Developer Portal](https://developer.x.com/) でアプリを作り、「User authentication settings」で **Read and write** にする。
2. 「Keys and tokens」で API Key / Secret と、**書き込み権限にした後で** Access Token / Secret を発行する。
3. プランごとの投稿上限を確認する。投稿は「タイトル＋YouTube のリンク」で、動画カードは X が自動で作る。

### Twitch（監視）
[Twitch Developer Console](https://dev.twitch.tv/console) でアプリを登録し、Client ID と Client Secret を控える。

### Kick（監視）
登録不要。ただし公開 API ではないため、ブロックされた日はそのチャンネルだけ「確認できませんでした」と出る。

### TikTok（任意・下書き）
TikTok for Developers でアプリに Content Posting API（`video.upload`）を追加し、OAuth で refresh token を取得する。審査前は「下書きに送る」だけで、最後の投稿ボタンはアプリで押す。

### Discord（承認ボタン）
1. [Discord Developer Portal](https://discord.com/developers/applications) で新しいアプリを作る（既存の販売用 Bot とは分ける）。Bot を作ってトークンを控え、メッセージ送信権限でサーバーに招待する。
2. Worker を公開する:
   ```sh
   cd deploy/approval-worker
   npx wrangler deploy
   npx wrangler secret put DISCORD_PUBLIC_KEY   # アプリの General Information にある Public Key
   npx wrangler secret put OWNER_DISCORD_ID     # 自分の Discord ユーザーID
   npx wrangler secret put GITHUB_TOKEN         # ClipForge の Contents: Read and write だけの fine-grained token
   ```
3. アプリの **Interactions Endpoint URL** に Worker の URL を入れて保存する（Discord が署名確認に成功すると保存できる）。
4. ボタンを使わない場合は `DISCORD_WEBHOOK_URL` だけでよい。その場合の承認は Actions の autopilot を `action=decide` で手動実行する。

### GitHub
- `AUTOPILOT_GITHUB_TOKEN`: ClipForge の **Variables: Read and write** を持つ fine-grained token（状態の保存先）。
- 制作には既存の `CLIPFORGE_PAT` を使う。

## 2. Secrets の一覧

`gh secret set 名前 --repo nyu1791-collab/ClipForge` で1つずつ登録する（値は画面に表示しない）。

| 名前 | 用途 |
|---|---|
| `AUTOPILOT_CONFIG` | チャンネル設定（`autopilot.example.yaml` の形式） |
| `AUTOPILOT_GITHUB_TOKEN` | 状態の保存 |
| `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` / `DISCORD_OWNER_ID` | 承認ボタン付きの通知 |
| `DISCORD_WEBHOOK_URL` | ボタンなしの通知（代わりに使う場合） |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` / `YOUTUBE_REFRESH_TOKEN` | 投稿・公開設定・分析 |
| `YOUTUBE_API_KEY` | 監視（任意） |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET` | X 投稿 |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | Twitch 監視 |
| `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` / `TIKTOK_REFRESH_TOKEN` | TikTok 下書き（任意） |

登録後、Actions の autopilot を `action=check` で実行すると、どれが揃っているかだけを表示する（値は出さない）。

## 3. 動かし方

- **登録前でも試せる**: `uv run python -m clipforge.autopilot preview` は、いま見つかる配信を表示するだけ（保存・通知・制作なし）。YouTube（RSS）と Kick はキー無しで確認できる。
- **登録が済むまでは休止**: 鍵や設定が無い間、毎時・毎日・毎週の自動実行は「休止中」と表示して正常終了する（失敗メールが毎時届くことはない）。
- **チャンネルの確認が6回続けて失敗**すると Discord に1回だけ知らせ、直ったときも1回知らせる。

- 最初の `watch` は各チャンネルの既存の動画を「確認済み」として記録するだけで、通知しない。次の配信から通知が来る。
- 7日間押さなかった承認は期限切れになる。
- 失敗したジョブには「もう一度」と「最初から作り直す」が付く。「もう一度」は保存済みの制作へ再接続するだけ（モデル・GPU の再実行なし、アップロード済みのクリップも上げ直さない）。「最初から作り直す」は料金が再びかかる明示的な選択。
- 実行環境が途中で止まったジョブ（制作中のまま7時間以上、または投稿途中）も「もう一度」で続きから再開できる。
- 手元で試す: `AUTOPILOT_CONFIG` か `autopilot.yaml` を用意し、`uv run python -m clipforge.autopilot watch`（状態は `~/.clipforge-autopilot/state.json`）。

## 4. 話題選定への反映

`analytics` が直近28日の再生数・維持率から「よく見られた話題・見られにくかった話題」をチャンネルごとに短くまとめる。次の制作では ClipForge の話題選定（`audience_feedback` 入力）に参考として渡す。あくまで判断材料の一つで、音声の内容を優先し、誇張はしない。
