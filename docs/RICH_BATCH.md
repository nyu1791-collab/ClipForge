# 1配信から3〜5本のリッチショートを作る

## 保存領域

| 場所 | 内容 | Git管理 |
| --- | --- | --- |
| assets/fonts, se, bgm, overlays | 許諾済みフォント・効果音・BGM・透過PNGとライセンス | 可。単体32MiB以内を推奨 |
| schemas, jobs/examples, data/examples | JSON Schema、区間・スタイル・同期設定の例 | 可。秘密URL・実発話は含めない |
| workspace/cache | HLSセグメント、低帯域音声、ASR、区間動画 | 不可、gitignore |
| workspace/output | 完成MP4、ASS/SRT、clips_manifest.json、取得・描画記録 | 不可、gitignore |
| 非公開workerのArtifacts | 完成したバッチとメタデータ | 7日保持。公開リポジトリへのアップロードは禁止 |

素材をリポジトリに常駐させられます。元動画をGitに入れたり、Git LFSを無条件に使ったりはしません。
GitHubのファイル上限と、Releases・Artifacts・LFSの上限、保持期間、料金は別です。
「Releasesなら常に無料・永久保存」とは扱わず、利用するアカウントの契約・容量を確認してください。
生成字幕・文字起こしも配信内容を含むため、軽いテキストであっても公開Gitへ自動commitしません。

## 最初の3本：区間指定から一気通貫

```sh
uv sync --locked --extra media-fetch
uv run python tools/render_rich_shorts.py \
  --manifest jobs/examples/kick-test-3.json \
  --quality 720p --whisper-model small \
  --assets assets --cache workspace/cache --output workspace/output/test
uv run python tools/check_rich_batch.py workspace/output/test
```

例の区間は60.3–90.3秒、240.7–270.7秒、600.2–630.2秒です。
これは動作確認用の指定区間で、意味的な見どころを選んだ結果ではありません。
`selection_method=test_intervals`、score=0で記録し、「話題が完結」「面白い」などの虚偽の評価を付けません。
字幕が空の場合はCPU/int8のWhisper smallで実音声を文字起こしします。発話がない区間に字幕を捏造しません。

CPU処理は`deploy/rich-batch.yml`を既存の非公開`ai-model-workers`に配置して実行します。
制御VMには認証値や巨大動画を残しません。workerは実装の完全なcommit SHAを指定し、
外部LLMへ映像・音声を送りません。Artifactsは非公開workerの権限を持つ人だけが取得できます。

## 2段階の取得

ステップAは全編の音声解析です。

```sh
uv run python tools/fetch_kick_vod.py 'https://kick.com/CHANNEL/videos/UUID' \
  --audio-only --output workspace/cache/full-audio.m4a
```

マスターに音声専用renditionがあれば優先し、なければ最低解像度・低帯域のmuxを取得して
mono/16kHz/48kbps AACに変換します。後者は映像セグメントの通信も必要です。
約2時間ならM4A自体は約43MiBですが、ネットワーク取得量が同じになるとは限りません。
全編をASRした時間が支配的になるため、まず短い区間で品質とCPU時間を確認してください。

ステップBは選んだ区間だけの高画質取得です。

```sh
uv run python tools/fetch_kick_vod.py 'https://kick.com/CHANNEL/videos/UUID' \
  --start-time 600.2 --duration 45 --quality 1080p \
  --output workspace/cache/range.mp4 --max-bytes 2147483648
```

`--end-time`と`--duration`は排他、qualityは720p・1080p・best。
720p/1080pは上限以下の最良renditionを選びます。存在しない解像度へアップスケールしません。
EXTINFの累積秒数と区間の交差で必要セグメントを特定し、前後の余分な部分は結合後に正確に切り出します。
fetchのMP4はCRF18で再エンコードし、非キーフレーム開始でも指定秒へ合わせます。
rawセグメントをコピーしただけのずれた時刻を字幕に使いません。

旧サンプルの120秒・360p・64MiB制限は新しい取得処理にはありません。
代わりにデフォルト2GiB・配信4時間、明示設定で最大16GiB・24時間までの予算を設けています。
CLIの`--max-bytes`は1取得セッション、バッチでは共通セッション中に新規取得した区間の合計に適用します。
既存キャッシュを再利用した区間はネットワーク取得量に含みません。ディスク全体の使用量は別に監視してください。

Chrome互換通信とRefererを使用しますが、同一HTTPSホスト・アーカイブパスに限定し、
プロキシとCA信頼は維持します。再生URLやAPIキーはログへ出しません。
暗号化HLS、fMP4、byte-range、不連続、署名クエリ、リダイレクトは現状未対応で停止します。
ENDLISTがない成長中の配信は処理しません。HTTPエラーでの自動再試行はありません。

## 話題・盛り上がりの自動選定

```sh
uv run python tools/render_rich_shorts.py \
  --auto 'https://kick.com/CHANNEL/videos/UUID' --count 4 --clip-seconds 45 \
  --quality 720p --whisper-model small --style assets/style.example.json \
  --output workspace/output/auto
```

全編軽量音声→CPU文字起こし→3〜5候補のマニフェスト→高画質区間取得→縦型描画の順です。
ローカルの自動選定は、発話の区切り、語彙の変化、発話密度でtopic候補を作り、
1秒ごとの音声RMSピークでhighlight候補を作ります。最初に両タイプを取り、
時間が重複する候補や語彙が強く重なる候補を除外します。タイトルは実際の発話から抽出します。
無音・候補不足の場合は3本を水増しせず失敗として知らせます。

これはLLMによる意味的な起承転結・笑いどころの評価ではありません。
大きな物音もRMSピークになるため、公開前の確認が必要です。
既存のGemini映像選定機能は別のパイプラインとして残しており、ここで勝手にモデルや課金を追加しません。
`--plan-only --output workspace/clips_manifest.json`で高画質取得前に設計図だけ確認できます。

## マニフェスト契約

`schemas/clips_manifest.schema.json`が正式なJSON Schemaです。
各clipはclip_id、type、hook_title、start_time、end_time、subtitles、score、reasonを持ちます。
3〜5本、1本30〜180秒、連番ID、配信内の範囲、20%を超える区間重複を検証します。
start_time/end_timeおよびsubtitles内のstart/end/wordsはすべて**元配信の秒数**です。
字幕をローカル秒と混在させません。レンダラーが一度だけ開始秒を引きます。
選定方法はtest_intervals / manual / local_transcript_audioのいずれかです。

成功時はclips_manifest.jsonとbatch_result.json、各clip_01等のvideo.mp4、captions.ass/srt、render.jsonを出力します。
render.jsonには元区間、入力・素材・完成物のSHA-256、実画質、字幕の由来（ASR/manifest）を記録します。
`--resume`は同じ入力・素材・コード・モデル・画質の場合のみ使用でき、完成物のハッシュを確認して再利用します。
ファイル名は連番IDから生成し、LLMや発話テキストをパスに使用しません。
ロックで同一出力先への並列実行を防ぎ、完成したclipディレクトリだけを原子的に確定します。
削除する場合はworkspace/cacheとworkspace/outputを個別に扱い、完成物を巻き込まないようにしてください。

## リッチ編集

- 9:16、720/1080高、背面は拡大＋Gaussian blur、中央は全景を保持。
- 上部は黄色フックタイトルと半透明の背景箱。下部は白・太字・黒縁、短いフェードとカラオケ字幕。
- PNG枠を全体へ重ね、フォント名・フォントディレクトリ・字幕/縁/強調色をRichStyleで設定可能。
- BGMはループ＋フェード、SEは開始時に1回、元音声と混ぜて最終loudnormを実施。
- gainは既定BGM 0.07、SE 0.15。これは自動ダッキングやラウドネスの厳密な二段階測定ではありません。
  大音量の素材では聞き取りを確認し、gainを調整してください。オプション未指定の音源は追加しません。

元動画そのものへ書き戻さず、ffmpegにはローカルファイルだけを渡します。
低画質素材を1080高の縦型キャンバスへ置いても、元の細部が高画質になるわけではありません。

## 外部素材の同期

マウントの例：

```sh
uv run python tools/sync_assets.py data/examples/assets-mount.json \
  --mount /path/to/licensed-assets --assets assets
```

各itemにはpath、source、sha256、license、任意のmax_bytesを指定します。
HTTPSのsourceなら`--allow-host assets.example.com`を必須とし、リダイレクトは追いません。
S3/R2は`uv sync --extra storage`と、通常の環境変数/認証プロファイルを使用します。
sourceを`s3://BUCKET/key`、R2等は`--s3-endpoint https://ACCOUNT.r2.cloudflarestorage.com`
および`--allow-host ACCOUNT.r2.cloudflarestorage.com`で設定します。鍵はJSONやGitへ書きません。
AWS標準S3 endpointはSDKに任せます。S3接続は現在の認証・バケット権限で別途確認が必要です。
Google Driveはこの版ではマウント済みディレクトリまたは直接HTTPSで読めるファイルを扱います。
Drive専用OAuth、共有画面の解析、同期デーモンは実装していません。

動画素材は`kind=video`・path=`video/NAME.mp4`でworkspace/cache側へ配置します。
SHA-256不一致・予算超過・symlink・外へのパス逸脱・既存ファイルとの差異は停止します。
tar/zipの自動展開はせず、zip slipや任意パス上書きを避けます。
HTTPSホストの許可は管理者が信頼した配信先に限定し、任意ユーザーのURLをそのまま公開サーバーで受けないでください。
R2/S3の保存先、認証、保持期限、容量課金は導入先で設定するもので、未設定の接続を成功扱いにしません。

## 検証状態

実配信の区間指定3本のrunner結果は、実行完了後のPR説明に記載します。
全編音声・全編ASR・ローカル自動選定は区間指定テストとは別の経路です。
外部BGMやロゴの商用利用許諾、S3/R2の実アカウント接続、配信全体の自動選定品質は別途確認対象です。
