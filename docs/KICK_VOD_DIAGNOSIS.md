# Kick指定VODの取り込み診断（2026-10-21）

対象：https://kick.com/meat_gengorou/videos/01a1574c-2350-77ce-90e2-4a7fefd4235c

## 更新：ブラウザ互換通信で短いサンプルを取得成功

ユーザー指定の`curl-cffi`のChrome互換通信とKickのRefererを付けたところ、
同じHLS再生先がHTTP 200になりました。既存プロキシ・CA信頼・証明書検証は維持しています。
TLS特性とヘッダーを同時に変えた比較であり、どちらが決定要因だったかは未分離です。
yt-dlp 2026.8.19のKick VOD extractorはメタデータ取得で404になるため、
公開ページのVideoObjectと指定VOD URLを照合してからcontentUrlを使います。

冒頭10秒の360p動画を1セグメント928,344バイトからMP4に変換できました。
ffprobeでH.264映像640×360、AAC音声、長さ10.007333秒、783,660バイトを確認し、
ffmpegで全映像・音声を最後までデコードしてエラーがないことを確認しました。
全編取得、AI候補選定、字幕・縦型編集、公開はまだ実行していません。
別IP・別アカウントへの切替、Cookie取得、証明書検証の無効化は使用していません。

```sh
uv sync --extra media-fetch
uv run python tools/inspect_kick_vod.py --browser-compatible 'https://kick.com/CHANNEL/videos/VOD_UUID'
uv run python tools/download_kick_sample.py 'https://kick.com/CHANNEL/videos/VOD_UUID' --seconds 10 --output sample.mp4
```

`media-fetch`は任意依存です。ChromeのUser-Agentはcurl-cffiの互換設定が生成するものを使い、
古い固定Chromeバージョンとの不整合を避けます。現行パッケージの最新ターゲットはChrome 150でした。
サンプル取得は1〜120秒・360p以下・合計64MiBまで、HTTPSの同一アーカイブパス内に限定します。
暗号化・byte-range・fMP4・不連続のプレイリスト、クエリ付き再生先、リダイレクトは未対応として停止します。
HTTPエラーは自動再試行せず、保存済みファイルも上書きしません。
ffmpegへ渡すのはダウンロード済みのローカルTSだけです。動画や再生トークンはGitへ保存しません。

以下はブラウザ互換通信導入前の診断記録です。

## 実際に確認した結果

- 公開ページはHTTP 200。構造化データのVideoObjectのURLと指定URLが一致。
- タイトル：お昼ご飯を作る！、公開日時：2026-10-20T03:12:50.000Z。
- 長さ：2時間11分5秒。ページ記載の期限：2026-11-19T03:12:50.000Z。
- og:videoとVideoObject.contentUrlの再生先は一致、HTTPSのstream.kick.com上のHLS。
- 再生先の応答はHTTP 403、本文 `error code: 1010`。HLSプレイリストや動画データは未取得。
- cloud-environment-runtimeの確認ではHTTPネットワークポリシーはunrestricted/enforced、現在の観測。
- Kick公式APIで対象チャンネルの情報を取得できた：[実行記録](https://github.com/nyu1791-collab/ClipForge/actions/runs/39646004209)。これはVOD取得の認可とは別。

Cloudflareの[1010の説明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)はクライアント特性に基づく拒否を示します。
応答内容はこれと整合しますが、CDN・中継のどの層が生成したかまでは断定していません。
HTTP 403だけで「IPブロック」「ログイン不足」「APIキー不足」と断定しません。
公開ページの有効期限は記載情報であり、今後の配信継続を保証しません。

## 再現用の診断コマンド

```sh
python tools/inspect_kick_vod.py 'https://kick.com/CHANNEL/videos/VOD_UUID'
```

ページに埋め込まれた構造化データと指定URLを照合し、許可した再生先に1回だけアクセスします。
APIキー、Cookie、署名付き再生URLをログに出さず、リダイレクトや拒否後の別経路への切替を行いません。
終了コード0はHLSプレイリストの確認のみ、2は未確認・拒否等、1は入力・通信等の例外です。
動画ダウンロードや自動ジョブ投入は行いません。公開ページの構造変更や再生先変更時には停止します。

## 取得を進めるための具体的な経路

1. このVODの配信者に許諾と元録画の提供を依頼する。提供ファイルは既存の`tools/submit_job.py --input`へ渡せる。
2. Kickに、今回のアプリから利用可能な公式のVOD取得方法・認可条件を確認する。
3. Kick側で取得が認可されたら再診断し、HLS到達確認→短いサンプルの動画・音声検証→サイズ制限付き取得→既存ジョブ投入の順で実装する。

別IP・プロキシ・アカウントやブラウザ偽装で拒否を回避して取得成功扱いにはしません。
元録画を使う場合も、Gemini等へ送るモデル・範囲は既存の明示設定に従います。

## Kickへの問い合わせ下書き（未送信）

宛先候補：公開ページ記載のTechnical Support `techsupport@kick.com`。

> We are integrating ClipForge with the official Kick Developer API. Channel metadata requests work, but the public HLS content URL advertised by the following VOD page returns HTTP 403 with body “error code: 1010” to our client:
> https://kick.com/meat_gengorou/videos/01a1574c-2350-77ce-90e2-4a7fefd4235c
>
> The VOD page itself returns HTTP 200. Could you confirm the supported, authorized method for retrieving VOD media for creator-approved clip editing, including any required OAuth scopes, creator consent flow, or application access approval? We have stopped media requests after the denial and are not attempting to bypass it.

メールは送信していません。キー、Cookie、再生URLのトークンを問い合わせへ含めないでください。
