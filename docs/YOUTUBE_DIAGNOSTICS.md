# YouTube 取得診断（2026-10-23 UTC）

対象: `https://youtu.be/6ras3m-gFuc`。
[匿名比較 run](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39900550459)
は GitHub-hosted Ubuntu 24.04、Python 3.12、Node 22、yt-dlp 2026.10.21、
curl-cffi 0.13.0、bgutil-ytdlp-pot-provider 2.0.2 で実行した。
bgutil サーバーは commit `26475e9d3665b972c4aeb83b4dbaafcf64b88438` を使用した。

`--ignore-config --skip-download --no-playlist` で匿名メタデータのみを確認。
Cookie を渡していない。各時間は Python monotonic による yt-dlp プロセスの起動・終了を含む。

| 条件 | PO 無効 | PO 有効 |
| --- | ---: | ---: |
| 既定 | 3.392秒 | 1.509秒 |
| ios,mweb,tv_embedded | 1.944秒 | 1.714秒 |
| Chrome impersonation | 1.470秒 | 1.525秒 |
| クライアント指定 + Chrome | 1.888秒 | 1.939秒 |

**8条件すべてメタデータ取得失敗**。YouTube の応答は
`Sign in to confirm you’re not a bot`。
音声ファイルの取得、GPU 割当、ASR は実施していない。
試験ジョブは 04:29:05〜04:30:01 UTC（準備・後片付け込み56秒）。
メタデータ比較は 04:29:40.676721〜04:29:56.098976 UTC。
各条件は別プロセスだが、同一 runner の IP を共有するため、独立した IP 間比較ではない。

PO 有効の4条件では provider 読み込みとローカル HTTP サービスの起動を確認した。
生成試行のログはなく、生成済み PO Token の効力を実証した結果ではない。
[provider の説明](https://github.com/Brainicism/bgutil-ytdlp-pot-provider#readme)にも
PO Token は IP ベースのログイン要求を解消する保証がない旨が記載されている。

worker の Secret は名前だけを確認し、`YOUTUBE_COOKIES_B64` は未設定だった。
Cookie 入力の実装・テストと、実際の認証 Cookie による取得成功は別。
現在の結果から「クラウド IP でも100%取得できる」とは判断できない。
設定方法は [AUDIO_FIRST.md](AUDIO_FIRST.md#youtube-の-cookie-入力と匿名診断) を参照。
