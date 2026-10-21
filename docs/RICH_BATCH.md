# 1配信からShortsと通常の話題別動画を作る

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

全編軽量音声→CPU文字起こし→各形式3〜5候補のマニフェスト→高画質区間取得→描画の順です。
ローカルの自動選定は、発話の区切り、語彙の変化、発話密度でtopic候補を作り、
1秒ごとの音声RMSピークでhighlight候補を作ります。候補の終わりは発話末尾へ合わせます。最初に両タイプを取り、
時間が重複する候補や語彙が強く重なる候補を除外します。タイトルは実際の発話から抽出します。
無音・候補不足の場合は3本を水増しせず失敗として知らせます。

これはLLMによる意味的な起承転結・笑いどころの評価ではありません。
大きな物音もRMSピークになるため、公開前の確認が必要です。
既存のGemini映像選定機能は別のパイプラインとして残しており、ここで勝手にモデルや課金を追加しません。
自動選定の`--plan-only --output workspace/output/plans`は高画質取得前に形式別の設計図だけを保存します。
全編取得とASRは必要です。manifest入力の`--plan-only`は従来どおり出力JSONファイルを指定します。


## Shortsと通常動画を同時に作る

以下は十分なCPUとディスクを持つ**非公開worker**で実行します。

```sh
uv run python tools/render_rich_videos.py \
  --auto 'https://kick.com/CHANNEL/videos/UUID' \
  --output-kind both --count 3 --clip-seconds 45 --topic-seconds 300 \
  --style assets/style.example.json --topic-style assets/style.topics.example.json \
  --quality 720p --whisper-model small --max-bytes 8589934592 \
  --cache workspace/cache --output workspace/output/production --resume
uv run python tools/check_rich_batch.py workspace/output/production
```

`--output-kind shorts|topics|both`を選べます。`both`は各3〜5本（合計6〜10本）です。
Shortsの区間を引き延ばす方式ではなく、通常動画は独立して長めの話題候補を選びます。
前後60秒の語彙変化、発話密度、無音境界を使い、発話末尾へ合わせます。
`--topic-seconds`は目標長です。近傍に適切な発話末尾がなければ候補から除外し、本数を水増ししません。
意味的な起承転結・笑いの理解は行わないローカル方式です。公開判断は別途必要です。
通常動画のタイトル箱は先頭8秒、Shortsでは全区間に表示します。

全編音声とCPU ASRを共有し、形式別に`shorts/`と`topics/`へ独立したmanifest・MP4・字幕・検査記録を出力します。
両方完了してから親に`production_result.json`を作成します。一方が失敗しても完成した形式を消しません。
両方の選定を終えてから高画質取得に進むため、候補不足を描画前に検出します。
同じ配信の解析はファイルロック、同じ出力先の描画は別ロックで二重起動を拒否します。

ASRは10分単位で時刻・SHA-256付き保存を行い、再開時に完了部分を再利用します。
`--resume`で既存出力の変更検出も行います。コード・モデル・入力・スタイルを変えた場合は新しい出力先を使います。
キャッシュを同じ場所へ置けば、別の出力先でも音声・ASR・高画質区間を再利用できます。
全編ASRの途中ではモデルを1回だけ読み込みます。区間指定のASRにもハッシュ付きキャッシュがあります。

`deploy/rich-batch.yml`を既存の非公開workerへ同期し、`mode=auto`、`output_kind=both`で依頼できます。
通常のGitHub-hosted CPU runnerを使用し、長い配信に対応するためタイムアウトは360分です。
実行時間・GitHub Actions使用量はアカウントの契約によります。自動再試行や有料runnerへの切替はしません。
重い取得・ASR・描画は制御VMへ戻しません。

通常動画の区間指定例は`jobs/examples/kick-topics-test-3.json`です。
`mode=manifest`でこのパスを指定すると、各120秒の16:9動画3本を生成します。
指定区間による動作確認であり、自動で見どころを選んだ結果ではありません。

同workflowのArtifactsは`rich-videos-RUN_ID`と`analysis-cache-RUN_ID`、いずれも非公開・7日保持です。
`cache_run_id`に以前の実行IDを指定すると音声・ASR・取得済み区間を再利用します。
キャッシュにも実発話・動画が含まれます。公開側のActionsやGitへ移さないでください。
未完了ASR chunkの一時WAVと生HLSセグメントはArtifactsへ含めません。
CLIの`--max-bytes`は同一自動処理で全編音声＋各形式の区間取得に共有する保守的な予算です。
ローカルTSキャッシュの再読み込みも計上し、完成済みMP4キャッシュの再利用は計上しません。
制御用メタデータ通信・モデル重み・ディスク総量はこの予算に含みません。

## 字幕の改行・品質ゲート

品質改善ブランチ`ccr-12fe9485-ugaszt`で利用されていたBudouXを確認し、このバッチの字幕基盤へ採用しました。
Janomeの形態素境界でASRトークンを結合し直し、BudouXの文節境界と句読点を優先して2行に収めます。
`ちょ`と`うど`のようなASR単位をそのまま改行位置にしません。
認識が乱れた長い未知句はBudouXの文節で分け直し、1語と誤判定されて止まることを避けます。小書きかなや句読点の行頭、開き括弧の行末を避けます。
実際に選ばれる太字フォントのピクセル幅を測定し、未インストール・欠落文字・収まらない字幕は停止します。
1語が行幅を超えるときは最大40%縮小し、調整を警告へ記録します。無理な語中分割や文字削除を行いません。
4秒・2行を保ち、可能なら短い字幕を隣へ結合。余白の時間内で読める長さへ延長します。
限界を超える読み速度や1秒未満の字幕は警告として残ります。

`caption_review.json`に実際の表示イベント数、推定した語境界数、幅、フォントSHA-256、警告を保存します。
ASRトークン内の新しい境界は比例補間であり、強制アライメントの精度を保証しません。
ASSとSRTは同じ10ms単位の境界と改行です。見出しはSRTへ入れません。
`render.json`はASRセグメント数と表示イベント数を別々に記録します。
検査レポートでは`asr_segments`と`caption_events`を使います。
`tools/check_rich_batch.py`は両形式の親ディレクトリ、Shortsのみ、通常動画のみの出力先を検査できます。旧互換の`subtitle_events`はASRセグメント数です。

完成MP4の全デコード・寸法・長さ・音声・真ピークを検査してからclipディレクトリを確定します。
品質ゲートv2では出力音声48kHzを検証し、真ピークが-0.5 dBTPを超える場合は失敗。-16±2 LUFSの外側や無音は警告として記録します。
失敗時は`failure.json`に段階・clip ID・固定のエラーコードを記録し、発話やプロバイダ応答をログへ出しません。
機械検査の合格、AIによるフレーム確認、人による全編視聴・試聴、選定の面白さは別の状態です。

## 文脈を確認した区間を非公開のまま実行する

自動候補を見て区間を調整した場合は、`ProductionPlan`へShortsと通常動画のmanifestをまとめます。
正式スキーマは`schemas/production_plan.schema.json`です。両方のsource URL・動画長・形式・manual由来を検証します。
各形式3〜5本、タイトル、字幕の時刻、スタイルを既存manifestと同じ契約で扱います。
`review_basis=assistant_transcript`はこのセッションのAIによる文字起こしの文脈確認、
`manual_intervals`は提供された指定区間です。どちらも人による映像・音声承認を意味しません。

```sh
uv run python tools/submit_rich_batch.py \
  --plan workspace/output/reviewed-intervals.json \
  --source-ref FULL_COMMIT_SHA --cache-run-id PREVIOUS_PRIVATE_RUN_ID
```

制御VMはJSONを検証し、指定workerが非公開であることをAPIで確認してから、
私有Actionsへ区間・タイトル・スタイルだけを入力します。送信用planの全clipは`subtitles=[]`としてください。
実発話を含むplanを誤送信・破棄しないよう、字幕入りのplanは送信ツールで拒否します。
元の字幕入りplanは保持し、区間のみのコピーを別名で用意してください。

workerの`mode=reviewed`は`cache_run_id`の全編音声と10分単位ASRをSHA-256で検証し、
指定した元配信時刻の字幕を復元して、共通の描画・品質ゲートで両形式を生成します。
この経路はASRを開始しません。完全なキャッシュがない、実装モデルや配信長が一致しない、
チェックサムが違う場合は停止します。同じ高画質取得キャッシュも利用できます。
実発話をGitへcommitしたり、私有ファイルのアップロード先へ再送する必要はありません。

同じplan内容・実装SHA・cache runの送信は`workspace/control/batches/`の私有記録で再送を防ぎます。
通信失敗時は実際には投入済みの可能性があるため、`submission_uncertain`と一意の`submission_id`を保存し、
自動で二度目のdispatchはしません。Actionsの実行名で同じIDを確認してから対応します。
完成物とmanifestは非公開Artifactsの保持期間内に回収してください。
`tools/render_reviewed_batch.py --plan ...`で、十分なCPUのworker上へ直接実行する場合は、
編集済み字幕を含むplanもそのまま利用でき、字幕が揃っていればASRキャッシュは不要です。

## マニフェスト契約

`schemas/clips_manifest.schema.json`が正式なJSON Schemaです。
各clipはclip_id、type、hook_title、start_time、end_time、subtitles、score、reasonを持ちます。
各形式3〜5本、連番ID、配信内の範囲、20%を超える同形式内の区間重複を検証します。
`output_kind=shorts`（省略時）は30〜180秒・9:16、`topics`は60〜1200秒・16:9です。
個別clipは共通モデルで30〜1200秒を受け取り、manifestが形式ごとの長さとtopicsのtype=topicを検証します。
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

- Shortsは9:16、通常動画は16:9。720/1080高、背面は拡大＋Gaussian blur、中央は全景を保持。
- 素材が30fps以下なら元fps（30000/1001等の分数も）を保持。30超は30に制限。既定slow/CRF18。
- 上部は黄色フックタイトルと半透明の背景箱。下部は白・太字・黒縁、短いフェードとカラオケ字幕。
- PNG枠を全体へ重ね、フォント名・フォントディレクトリ・字幕/縁/強調色をRichStyleで設定可能。
- BGMはループ＋フェード、SEは開始時に1回、元音声と混ぜて最終loudnormを実施。
- gainは既定BGM 0.07、SE 0.15。発話中はsidechaincompressでBGMを下げます。ラウドネスの二段階補正ではありません。
  混合後にloudnorm（-16 LUFS / -2 dBTP目標）とリミッターを適用し、48kHzのAACへ符号化して真ピークを再測定します。
  loudnorm内部の高いサンプルレートをAACの自動選択へ渡さず、出力48kHzを明示します。
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

元の区間指定3本は[非公開worker 39651468786](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39651468786)で成功。
2026-10-21にArtifactsを回収し、ハッシュ、全デコード、字幕時刻を再確認。AIによる各1フレームの確認ではブラー・箱タイトル・字幕・枠を確認しました。
記録の5/18/6はASRセグメント数です。実際に表示する字幕イベント数とは区別します。
完成AACの測定は-17.22/-16.77/-17.15 LUFS、真ピーク-0.34/-1.25/-1.44 dBTP。
旧1本目のピーク余裕不足と語の途中の改行を確認し、今回の実装で対処しています。
人による全編視聴・音声試聴、意味的な選定品質の承認は未実施です。
全編131分の音声取得・ASR・ローカル自動選定で両形式の描画まで検証済みです。
自動生成実行は最後のASS/SRT空白差の検査で停止し、その差を修正しました。
その後、AIが文字起こしで文脈を確認した3話題を両形式へ組み直し、
[最終worker39668413753](https://github.com/nyu1791-collab/ai-model-workers/actions/runs/39668413753)で成功。
Shorts3本（48/58/67秒）、通常動画3本（130/82/162秒）、全デコード・品質ゲート・ASS/SRT一致を確認。
回収後にハッシュを検証し、AIが計13フレームを確認しました。末尾0.33秒の字幕1件とASR誤認識候補は記録しています。
最新コードのCIはPython3.11/3.12で24件通過。詳細・測定値は`docs/QUALITY_REVIEW.md`を参照してください。
全編音声・全編ASR・ローカル自動選定は区間指定テストとは別の経路です。
外部BGMやロゴの商用利用許諾、S3/R2の実アカウント接続、配信全体の自動選定品質は別途確認対象です。
