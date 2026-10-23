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

## Docker・nightly・WARP の追加検証

[再試験 run](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39901516308)
は `yt-dlp 2026.10.21.233146.dev0`、bgutil 2.0.2、`web,mweb`、
公式 `warp-cli 2026.8.2115.0` で実施。
Docker image digest は `sha256:d7d34108b76e52c7d3b575fe9e797e4bf41c1b2a08f80c5292eff6f851ce8472`。
`warp=on` を確認し、その後に Docker サーバーを再起動して、IP変更前のキャッシュを破棄した。
WARP 条件開始時のサーバー uptime は1.155秒だった。

| 経路 | 条件 | Token取得 | Token付きplayerリクエスト | メタデータ | 経過 |
| --- | --- | --- | ---: | --- | ---: |
| 直通 | auto | なし | 0 | bot確認で失敗 | 3.146秒 |
| 直通 | always + player API再要求 | 成功 | 2 | bot確認で失敗 | 2.307秒 |
| WARP | auto | 成功 | 0 | 成功 | 4.216秒 |
| WARP | always + player API再要求 | 成功 | 2 | 成功 | 3.467秒 |

成功条件は動画長3,731秒・42形式を返した。ジョブ全体は04:39:57〜04:41:29 UTC（92秒）。
`always` は `fetch_pot=always;webpage_skip=player_response` を指定し、
既存のplayer応答を使わずにAPIを再要求する。autoと一変数だけの比較ではない。
Token生成・返却のtraceと、送信JSON内のpoTokenの存在を値を保存せずに計測した。

前の[試験 run](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39901207425)
では、WARPとToken取得・付与を確認してもメタデータは拒否された。
別runではWARP出口IPなども変わり得るため、再起動だけが成功原因だとは断定できない。
また、最初のNode版試験でもサーバー起動と/ping成功を確認しており、
「Pythonパッケージだけを入れてサーバーがなかった」という説明には当てはまらない。
メタデータの成功だけでは全音声取得・ASR成功を意味しない。

## 実装範囲

- Cookie入力: pipeline/CLI/worker Secretに実装。実Cookieは未設定・認証成功は未検証。
- Docker + nightly + WARP: 診断と通常の音声取得で共通化。`--youtube-audio-transport warp`で明示し、自動切替はしない。
- `acquire_audio_url.py --youtube-web-clients`: web/mwebと明示的PO Token要求を指定する取得オプション。別途providerの起動が必要。
- Data API / RSS / WebSubによる配信の検出: 未実装。
- 住宅用proxy・自宅PC取得worker: 未実装。

[Data API](https://developers.google.com/youtube/data/reference/rest/v3/search/list)は動画IDを検索できるが、
そこから構成したwatch URLは視聴ページであり、音声・動画ファイルの直接URLではない。
今回のファイル取得に対するbot確認を解消するAPIではない。

## 全音声取得の成功

[音声取得 run](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39902058014)
で、上記の WARP + fresh Docker + nightly + web/mweb を使い、
通常の `RemoteMedia` と `audio_first.acquire` を通じた全音声の取得・ASR用変換に成功した。
Cookieは使っていない。GPU・ASRは起動していない。

- 取得開始: 2026-10-23 04:47:34.428099 UTC
- 検証終了: 04:48:10.868132 UTC
- 合計: **36.437秒**（メタデータ2.967秒、取得・変換33.415秒、その他はハッシュ・音声検証）
- 音声: **3,730.773313秒、23,867,993 bytes、開始時刻0**
- SHA-256: `4a59a78f20a9f4fbd6c4052b667cf8acae7daea5075b0a4314e4a92e491a6af8`
- [非公開の検証済み音声Artifact](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39902058014/artifacts/12478959396)（保存期間7日）

このrunでも直通はbot確認で失敗し、WARPはauto/alwaysともメタデータ取得に成功。
GitHubのジョブ記録は04:45:55〜04:49:55 UTC（240秒）で、上記の音声取得時間とは別に扱う。
元メタデータの3,731秒と実ファイルの3,730.773313秒の差は0.227秒。
ファイルの全区間をPyAVで変換し、ffprobeで音声・長さを検証した。
単体テストは1,270件合格、13件除外（20.14秒）。

## 基盤化後の確認

[共通化した取得経路のrun](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39904822228)
（source `7d4494cf909ed0769548f567823efa12f782acfb`）では、固定環境の導入、
WARP接続、provider再起動まで成功した。ただし、このrunの出口ではTokenを取得・付与しても
YouTubeのbot確認でメタデータが拒否され、音声取得も1.517秒で失敗した。
**診断jobのsuccessは記録収集の成功であり、音声取得の成功ではない。**
通常の`acquire_audio_url.py`はこの例外で失敗するため、GPU割当前に停止する。

private Artifact内の状態を実際に回収して確認した結果:

- `status=cleaned`、`warp_disconnected=true`、`provider_removed=true`
- 依存導入0.344秒、provider起動10.344秒、WARP接続20.276秒、provider再起動1.882秒
- 終了確認0.588秒、GPU割当・ASRともなし
- yt-dlp、provider、curl-cffi、EJS、Node、WARPの実バージョンが固定値と一致

成功した既存runの音声はGitHub Appから回収し、Artifact ZIPのSHA-256をGitHubのdigestと照合した。
成功状態・request ID・worker commit・checkoutしたsource commit・元URLをrunとjob logで確認し、
取得報告のSHA-256と実音声、ffprobeの長さを照合してから、旧Artifact用のmanifestを作った。
旧Artifactを無条件に信用して自動移行する処理は設けていない。

この実音声（23,867,993 bytes、3,730.773313秒）から、Pythonのネットワーク接続を禁止して
`acquire_audio_url.py --verified-audio-dir ...`を実行し、新しい依頼を作ることにも成功した。
入力再生成は**0.064910秒**（Archive回収、旧run確認、事前ffprobe、独立した事後照合は別）。
新requestは`asr_parts=2`・`aspect=16:9`で、入力ハッシュは元の音声と一致した。
GPU・ASRは開始していない。

運用方法・固定値・失敗時の扱いは [YOUTUBE_TRANSPORT.md](YOUTUBE_TRANSPORT.md) を参照。
