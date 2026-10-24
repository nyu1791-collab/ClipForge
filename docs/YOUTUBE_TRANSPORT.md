# YouTube 音声取得の運用基盤

YouTube の全音声取得に成功した経路を、通常の取得と診断で共通化する。
成功した実行と計測値は [YOUTUBE_DIAGNOSTICS.md](YOUTUBE_DIAGNOSTICS.md) を参照。
この経路は音声取得用。ASR、プレビュー、承認後の映像描画は別工程である。

## 通常の投入

`submit_job.py --audio-analysis-url URL --youtube-audio-transport warp ...` を指定する。
設定JSONでは `"youtube_audio_transport": "warp"` が同じ意味になる。
PRをレビューする段階では、`--ref PUBLIC_SOURCE_SHA --workflow-ref WORKER_BRANCH`も指定する。
`--ref`は公開側のコード、`--workflow-ref`は非公開workerのworkflowを選ぶ。
worker refもreceiptへ保存し、再開時にmainへ戻さない。PRは未マージのため既定mainへは未反映。
URLだけの解析用で、文字起こしは既定4台（[ASR.md](ASR.md)）。
既定の `standard` は従来どおりで、失敗時にWARPへ自動で切り替えない。

WARPは使い捨てのGitHub-hosted runnerにだけ導入する。
VMや開発環境のネットワークは変更しない。ローカル事前確認は設定とURLを検証し、
実際のメタデータ・全音声取得はrunnerでGPU割当前に検証する。
Colabの既存の認証設定は通常の解析を実行する場合に必要で、診断には渡さない。

## 実行順序と停止条件

1. ハッシュ付き依存ロックで取得ツールを導入する。
2. Dockerイメージをdigestで取得し、所有ラベル付きのproviderを127.0.0.1:4416に起動する。
3. 既存WARP接続がないことを確認し、固定版の公式クライアントを接続する。
4. `warp=on`とCloudflare traceで観測した出口IPのハッシュを記録する。
5. providerを再起動し、IP変更前のToken・セッションキャッシュを破棄する。
6. 取得の前後で接続とproviderを確認し、通常の`acquire_audio_url.py`で全音声を取得する。
7. 長さ・容量・ハッシュを検証して`input-video.bin`と`request.json`を確定する。
8. WARPの切断を確認し、自分のラベルと実行IDを持つコンテナだけ削除する。
9. 取得・終了処理の成功後に、既存のASR工程へ進む。

起動、取得、終了処理のいずれかが失敗した場合はGPUを割り当てない。
状態ファイルは実行ID・試行回数・元URLのハッシュに結び付け、他の実行からの流用を拒否する。
既存状態を上書きして再起動しない。途中で終了してもworkflowの`always()`で回収する。
接続によってrunner制御が中断した場合に備え、20分後の切断timerも設ける。

Cloudflare traceは接続の観測手段であり、すべての通信先・アドレスファミリーの
出口IPを保証するものではない。YouTube側の判定による拒否もあり得る。
回数無制限の再試行、別アカウント、有料経路への自動切替は行わない。

## 固定する取得環境

| 構成 | 固定値 |
| --- | --- |
| yt-dlp | 2026.10.21.233146.dev0（配布metadata表記は2026.10.21.233146dev） |
| bgutil provider | 2.0.2 |
| curl-cffi / yt-dlp-ejs | 0.13.0 / 0.8.0 |
| Node.js | 22.23.3 |
| WARP client | 2026.8.2115.0 |
| Docker image | `brainicism/bgutil-ytdlp-pot-provider@sha256:d7d34108b76e52c7d3b575fe9e797e4bf41c1b2a08f80c5292eff6f851ce8472` |

推移依存も`tools/youtube-transport-requirements.txt`でバージョンとSHA-256を固定する。
更新時は`tools/youtube-transport.in`からロックを再生成し、空の環境への導入と実動画取得を再確認する。
通常取得と診断は`tools/youtube_transport.py`の`setup` / `prepare` / `connect` / `verify` / `cleanup`を共用する。

## 記録と認証

UTC時刻、各工程の経過秒、依存バージョン、依存ロックのハッシュ、接続の観測、
取得容量・音声ハッシュ、失敗した工程と例外型、終了確認を非公開Artifactに残す。
Cookie、PO Tokenの値、Google資格情報、GitHubトークンを状態ファイルへ入れない。
Cookie Secretは取得stepに限定し、依存導入・WARP・Dockerの子プロセスとASRへ渡さない。
Cookie入力の詳細は [AUDIO_FIRST.md](AUDIO_FIRST.md#youtube-の-cookie) を参照。

## 取得済み音声の再利用

診断で取得・検証した音声は、`audio.m4a`と`audio-source.json`を一緒に非公開Artifactへ保存する。
再利用時は信頼するGitHub runから取得し、producerのrun ID、request ID、source commitを別途指定する。
manifestだけではGitHub実行の真正性を証明しない。ダウンロード元のprivate runの同一性・成功状態も確認する。

`acquire_audio_url.py`へ次の4引数を加えると、ネットワーク取得を行わず新しい依頼の入力を作る。

```
--verified-audio-dir /private/verified-audio
--verified-audio-run PRODUCER_RUN_ID
--verified-audio-request PRODUCER_REQUEST_ID
--verified-audio-source-ref PRODUCER_SOURCE_SHA
```

元URL、producer情報、regular file、容量、音声長、SHA-256を検証する。
コピー後にもハッシュを照合し、不一致では新しい`request.json`を確定しない。
元音声を上書きせず、ASRも開始しない。診断workflowではWARP切断後にこの再利用も実行する。
以前のmanifestなしArtifactにはこの保証がなく、そのまま再利用しない。

## 現在の範囲

この設定でWARPを使うのは**URL音声の取得**。後段の映像取得を同じ接続へ載せる設定ではない。
自動プレビューや後日の描画で元動画を再取得する場合は、その工程の取得経路・Cookieも確認する。
Data API/RSS/WebSubによる配信検出、住宅用proxy、自宅PC workerは実装していない。

## 見直しの検証

最終コードの単体テスト1,295件、依存ロックからの空環境への導入、workflowのshell構文を確認した。
実環境では、取得拒否時にGPUを使わず、WARP切断・所有provider削除まで完了することを確認した。
また、成功済みの実音声23,867,993 bytesを来歴とハッシュで検証し、
Pythonの接続を禁止した状態で新しい2分割ASR用入力へ再利用できた（入力再生成0.065秒）。
元音声のYouTubeからの取得成功と、新しい出口IPでの取得成功は同じ保証ではない。
最新の再取得ではbot確認が再現しており、無条件に常時取得できる状態とはしていない。
