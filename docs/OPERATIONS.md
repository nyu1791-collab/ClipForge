# GitHub・AI運用

## リポジトリと権限

公開ClipForgeはコードの保存先。動画・音声・文字起こし・成果物は非公開workerで扱う。
`deploy/clipforge.yml` と `deploy/audio-stages.yml` の配置・処理の関係は [構成と配備先](../INDEX.md#worker-と成果物) を参照する。
公開側の `Build package` は開発用wheelを7日間保存する。動画生成・SNS投稿・PyPI公開は行わず、ビルド成功はモデル呼び出しや切り抜き品質の確認ではない。

通常の `GITHUB_TOKEN` によるpushやPR作成では、別workflowは自動起動しない。必要な処理は `workflow_dispatch` で起動する。権限は対象リポジトリと処理に必要な範囲へ付与し、workflowからの自動マージは行わない。

## ClipForgeのSecrets

- `GEMINI_API_KEY`：ローカル動画のGemini選定に使用する。URL音声解析経路では要求せず、Colabへ送らない。
- `COLAB_ADC_JSON`：指定ユーザーのColab自動起動用。初回設定と一時認証用Secretの扱いは [COLAB.md](COLAB.md) を参照する。
- `GH_TOKEN`：非公開workerへの依頼・一時入力転送・回収に必要なGitHub認証。転送元とActions内の認証は区別する。

Secretsは必要なステップにだけ明示的に渡す。公開ビルドはAPIキーを使用しない。値を文書・チャット・Git・公開Artifactsへ含めない。

## 動画ジョブの実行場所

主な経路はメディアURL → GitHub-hosted runnerで音声取得 → Colab GPUでASR・話題分割 → 同じrunのCPUジョブでpreviews・未承認の下書き → 映像確認・承認 → Colab CPUで描画 → 制御VMで `merge --local`。Geminiは呼ばない。URL固有の引数と承認は [AUDIO_FIRST.md](AUDIO_FIRST.md) を参照する。

ローカル動画は `tools/submit_job.py --input` で依頼し、Gemini選定と描画を行う。workerは既定の `colab-auto`、または `--worker cpu`。`colab-auto` のローカル動画経路はColabでASR・Gemini選定・描画を行う。Colabの認証・起動・解放・キャッシュ手順は [COLAB.md](COLAB.md) を正とする。

```bash
python tools/submit_job.py --input /path/to/source.mp4 \
  --gemini-model YOUR_VIDEO_MODEL_ID \
  --config "<CLONE>/config.example.json" \
  --wait --output "<CLONE>/outputs/job-001"
```

`<CLONE>` はチェックアウト先、モデル識別子は利用する動画対応モデルに置き換える。ローカル動画では候補動画のGoogleへの送信と指定モデルのAPI利用を行い、別モデルには切り替えない。キー登録だけで無料tierや十分な割当を保証しない。非公開Actionsの時間・Artifacts・Cacheにも契約の無料枠が適用される。

実行上限はローカルCPU動画ジョブ90分、ColabのURL取得を含む全体180分。連続ASRのColab実行ステップは90分、2分割の起動・実行・回収は110分。2分割は1依頼の中で最大2つの所有ランタイムを並列に使い、workflowで依頼同士を直列化する。配信長は横用設定で6時間、設定可能な最大は12時間。待機の上限は実行上限とは別で、既定18,000秒（5時間）、`--wait-timeout SECONDS` で変更できる。

同時に依頼できる初回動画ジョブは、worker種別によらず1件（`tools/submit_job.py` の `MAX_ACTIVE`）。1件の2分割ASRが最大2ランタイムを使うことと、2件の動画ジョブを並行投入することは別。クリップの並列描画は別枠で4件まで（`tools/submit_audio_stage.py`）。入口とActionsで重複実行を防ぎ、待機ジョブを蓄積する用途には使わない。制御VMは依頼・回収・検証・結合だけを担当する。

## 非公開データの経路と保管

1. ローカル入力は送信元でSHA-256と設定を記録し、非公開workerの一時Releaseへ入力・依頼JSON・任意のチャットJSONを送信する。URLだけの投入はrunnerが音声取得後に一時Releaseを作る。
2. runnerが非公開であること、依頼ID、コードSHA、入力サイズ、SHA-256を検査する。
3. URL経路は取得音声をColab GPUへ渡し、Geminiキーを渡さない。ローカル動画のGeminiへは候補の低解像度映像・音声・該当ASR文だけを送る。
4. 送信したGoogle Filesは処理後に削除を試行し、失敗は記録・通知する。
5. MP4・字幕・文字起こし・JSONは非公開Artifactsへ7日保存し、検証して回収する。
6. 一時入力Release・タグは終了時に削除する。削除はrunner既定の `gh` だけで行うため、checkoutや依存導入が失敗しても実行される（タグ形式と非公開repoを確認してから削除）。削除を確認できなかった場合は、runの警告とSummaryに表示する。回収時には制御側がReleaseの残存を確認し、依頼記録の `input_cleanup` に `DELETED`・`REMAINS`・`UNKNOWN` を記録する。`REMAINS` のときは `tools/submit_job.py --cleanup-tag <タグ>` で削除する。

段階キャッシュにはASR・チャットの時刻/重み・候補・選定JSONを含むが、原動画・MP4・APIキー・Cookie・OAuth情報は含めない。キャッシュはGitHubの保持・容量制限に従い、API呼び出しを確実に1回にする仕組みではない。モデル応答後・保存前にrunnerが失われた場合は記録を確認してから再依頼する。公開キャッシュは既定off。

## 結果の回収と再開

依頼記録は `~/.local/state/clipforge/<request_id>/receipt.json`。
`STAGING` / `STAGED` / `DISPATCHED` / `RUNNING` / `COMPLETE` / `FAILED` / `UNCONFIRMED` 等の状態を区別する。APIの応答が不明な場合は自動再送せず、同じ依頼IDのworkflowを確認する。

`--wait` を使わなかった場合、SSH切断、待機上限到達時も、runを再投入せず状態確認・回収を続ける。

```bash
python tools/submit_job.py --status REQUEST_ID
python tools/submit_job.py --collect REQUEST_ID --output /private/result-NEW
```

音声後段の回収には `python tools/submit_audio_stage.py --collect REQUEST_ID --output /private/stage-NEW` を使う。回収は新しい出力先へ行い、依頼ID・元コミットSHA・前段run・manifest・各ファイルのSHA-256を検査する。`complete` やActions成功は、人による内容・字幕確認や公開とは別。

### ローカル受付票がない環境での音声解析の回収

期待値を明示し、成功run・依頼ID・元コミット・音声SHA-256・ASR・話題・manifestを検査する。ローカル記録は書き込まない。受付票がある場合は通常の回収を使う。

```bash
python tools/submit_job.py --collect REQUEST_ID --run-id RUN_ID \
  --source-ref REVIEWED_COMMIT_SHA --audio-sha256 AUDIO_SHA256 --output /private/analysis-NEW
```

`--auto-previews` の中間解析だけを回収する `--collect-stage analysis` と部分失敗状態は [AUDIO_FIRST.md](AUDIO_FIRST.md#同じrunで候補映像と採用候補の下書きまで作る) を参照する。

### アップロード失敗と同じ音声依頼の再開

HTTP 401／403のアップロード失敗は `UPLOAD_FAILED_AUTH`、タイムアウトや5xx等は `STAGING_PARTIAL` と記録する。添付失敗はworkflowやASRの起動成功ではない。release作成・dispatch・run閲覧が成功しても、添付アップロードの認証成功とは限らない。

認証は転送元のGitHub接続で復旧する。workerのActions Secretsだけを更新しても、転送元CLIには渡らない。Fine-grained PATを使う場合は対象の非公開repoにContents: Read and write（release/添付）とActions: Read and write（dispatch/回収）を付与する。classic PATは通常 `repo`、workflow自体の変更には別途 `workflow` の条件も確認する。値は安全な設定画面で登録する。

```bash
python tools/submit_job.py --status SAVED_REQUEST_ID
python tools/submit_job.py --resume-audio SAVED_REQUEST_ID --input /private/original-audio.m4a
```

`--resume-audio` は保存済み設定・元コミット・依頼IDを維持し、元音声と添付のSHA-256・サイズを照合して不足分だけを転送する。不一致・重複添付・別の稼働ジョブで停止し、上書きしない。同じIDのworkflowがあれば新規dispatchしない。dispatch前は `DISPATCHING`、応答不明は `DISPATCH_UNCONFIRMED` とし自動再送しない。投入ロックはdispatch記録まで保持し、待機・回収前に解放する。

入力は処理後に削除されるため、Actionsの「Re-run jobs」だけでは再取得できない。失敗を確認し、新規依頼が必要な場合だけ同じ動画・設定・コードを送る。段階キャッシュが残れば完了済みASR・選定を再利用するが、長いASRの途中チェックポイントからの再開は未実装。モデル拒否・割当超過に自動再試行や別アカウント・別サービスでの回避を行わない。

### 途中保存と一時入力の削除

初回workflowが失敗・タイムアウト・キャンセルした場合、`transcripts/*.partial.jsonl` を非公開 `clipforge-partial-REQUEST_ID` artifactへ7日保存する。これは途中の発話本文で、完成ASRでも再開用でもない。私有領域へだけ回収する。

```bash
gh run download RUN_ID --repo nyu1791-collab/ai-model-workers \
  --name clipforge-partial-REQUEST_ID --dir /private/partial-NEW
python tools/submit_job.py --cleanup-tag clipforge-input-REQUEST_ID
```

### 単一クリップ成果物のローカル結合

```bash
python tools/submit_audio_stage.py --stage merge --local \
  --part-runs RUN1:REQUEST1,RUN2:REQUEST2,RUN3:REQUEST3 \
  --source-ref RENDER_SOURCE_SHA --output /private/merged-NEW
```

`--local` の結合は、描画に使ったコミット（`--source-ref` と同じ SHA）を制御VMでチェックアウトした状態で実行する。手元のコードが違うと `local merge code differs from source_ref` で停止する。例: `git worktree add --detach /tmp/merge-SHA SHA` で別の作業ツリーを作って、その中で実行する。

制御VMで各runのcompleted・success・依頼IDを確認し、artifactを一度取得して `merge_parts` で検証・コピーする。結合用のGitHub workflowは新規起動せず、FFmpeg・フォントも使わない。元コミット・本数・前段依頼IDは検証済みpartから取得し、任意の `--source-ref REVIEWED_COMMIT_SHA` で期待値を固定できる。
`delivery/`・manifest・request receiptを通常の回収と同じ検証後に確定し、receiptの `workflow_run` は `"local"` となる。GitHub上の結合では `--local` を付けず、`--previous-run`・`--previous-request`・`--source-ref`・`--part-runs` を指定する。

## チャットと人による修正

`--chat-file` は元動画0秒基準の配列で、`chat_offset_seconds` を時刻へ加算する。以下は合成例。

```json
[
  {"time_in_seconds": 130.5, "message": "合成の反応A"},
  {"time_in_seconds": 131.0, "message": "合成の反応B"}
]
```

チャットは任意。取得不能時は状態を記録して場面転換・ASRで候補を作る。候補データにユーザー名・本文を残さず、時刻と反応の重みだけを保存する。

ローカル動画の区間修正は `selection.json` を別ファイルへ複製し、元動画秒数の `clips` を編集して `clipforge run ... --selection-file edited-selection.json --output NEW_DIRECTORY` を使う。各clipに `start`・`end`・`candidate_id`・`title`・`reason`・0〜1の `confidence` が必要。この指定ではGeminiを呼ばず、既存生成物とは別の出力先を使う。
