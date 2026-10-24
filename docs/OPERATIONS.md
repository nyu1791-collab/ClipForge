# 回収・再開・後始末・権限

## リポジトリと権限

公開 ClipForge はコードの保存先。動画・音声・文字起こし・成果物は非公開 worker（`nyu1791-collab/ai-model-workers`）で扱う。workflow の原本は `deploy/` にあり、worker の `.github/workflows/` に置く（`clipforge.yml`・`clipforge-stages.yml`・`colab-usage.yml`）。公開側の `Quality` は検査とテスト、`Build package` は wheel の作成だけで、動画は扱わない。

通常の `GITHUB_TOKEN` による push や PR 作成では別の workflow は起動しない。必要な処理は `workflow_dispatch` で起動する。workflow からの自動マージはしない。

## Secrets（worker）

- `COLAB_ADC_JSON`：指定ユーザーの Colab 自動起動用。初回設定は [COLAB.md](COLAB.md)。
- `YOUTUBE_COOKIES_B64`：YouTube の取得に使う Cookie（任意）。音声取得と描画時の映像取得にだけ渡し、文字起こしのランタイムには渡さない（[AUDIO_FIRST.md](AUDIO_FIRST.md#youtube-の-cookie)）。

Secrets は必要なステップにだけ渡す。値を文書・チャット・Git・公開 Artifacts へ含めない。制御VM側は、所有者でログイン済みの `gh` を使う。

## 実行の上限と同時実行

- 解析の run は全体180分（音声取得30分、文字起こしと話題分け140分）。描画の job は1本あたり100分。
- 解析は1件ずつ（workflow の concurrency と `submit_job.py` の確認）。1件の解析が最大4台の Colab を使う。
- 描画は1本ずつ別の依頼になり、最大5件まで並列に走る（`submit_audio_stage.py` の `MAX_ACTIVE`）。
- 待機の上限は実行上限とは別で、既定18,000秒（5時間）。`--wait-timeout SECONDS` で変えられる。
- 配信長は横用設定で6時間、設定で最大12時間。

## データの経路と保管

1. 解析の依頼は設定と URL を `workflow_dispatch` の入力で渡し、ファイルは送らない。
2. runner が音声を取得し、長さ・サイズ・SHA-256 を確かめる。部分の音声だけを部分ごとの一時 Release に置く。
3. Colab は部分の音声を受け取り、認識結果をチェックサム付きで同じ Release に返す。
4. 音声・文字起こし・話題・候補映像・クリップは非公開 Artifacts に7日保存し、回収時に検証する。
5. 一時 Release とタグは終了時に消す。消せなかったときは run の警告と Summary に出る。回収時にも残りを確かめ、依頼記録の `input_cleanup` に `DELETED`・`REMAINS`・`UNKNOWN` を記録する。`REMAINS` なら `tools/submit_job.py --cleanup-tag <タグ>` で消す。

## 結果の回収と再開

依頼記録は `~/.local/state/clipforge/<request_id>/receipt.json`。`STAGING`・`DISPATCHED`・`RUNNING`・`COMPLETE`・`FAILED`・`UNCONFIRMED` などを区別する。dispatch の応答が不明なときは自動で再送せず、同じ依頼 ID の workflow を確かめる。

`--wait` を使わなかった場合や、SSH が切れた・待機の上限に達した場合も、run を再投入せずに状態の確認と回収を続ける。

```bash
python tools/submit_job.py --status REQUEST_ID
python tools/submit_job.py --collect REQUEST_ID --output /private/result-NEW
python tools/submit_audio_stage.py --collect REQUEST_ID --output /private/stage-NEW
```

回収は新しい出力先へ行い、依頼 ID・元コミット SHA・前段 run・manifest・各ファイルの SHA-256 を検査する。Actions の成功は、人による内容・字幕の確認とは別。

### 受付票が無い環境での解析の回収

期待値を明示し、成功 run・依頼 ID・元コミット・音声 SHA-256・文字起こし・話題・manifest を検査する。手元の記録は書かない。

```bash
python tools/submit_job.py --collect REQUEST_ID --run-id RUN_ID \
  --source-ref REVIEWED_COMMIT_SHA --audio-sha256 AUDIO_SHA256 --output /private/analysis-NEW
```

`--auto-previews` の途中の解析だけを回収する `--collect-stage analysis` と、部分失敗の状態は [AUDIO_FIRST.md](AUDIO_FIRST.md#候補映像の段が失敗したとき) を参照する。

### 取得済み音声の再利用

同じ配信をもう一度解析するとき（設定やコードを変えたときなど）は、前の run が残した検証済みの音声を再利用でき、取得の2〜5分を省ける。

```bash
python tools/submit_job.py --audio-analysis-url 'URL' --ref "$SHA" \
  --reuse-audio-run RUN_ID --reuse-audio-request REQUEST_ID --auto-previews --output /private/NAME-previews
```

### 失敗したとき

失敗した run は自動で再試行しない。部分の結果は `clipforge-partial-REQUEST_ID` artifact に7日残る（[ASR.md](ASR.md#失敗したとき)）。新しい依頼が必要なら、同じ配信・設定・コードで投入する。モデルの拒否や割り当て超過を、別アカウント・別サービスで回避しない。

## クリップの結合（制御VM）

```bash
python tools/submit_audio_stage.py --stage merge --local \
  --part-runs RUN1:REQUEST1,RUN2:REQUEST2,RUN3:REQUEST3 \
  --source-ref RENDER_SOURCE_SHA --output /private/merged-NEW
```

描画に使ったコミット（`--source-ref` と同じ SHA）を制御VMでチェックアウトした作業ツリーで実行する（例：`git worktree add --detach /tmp/merge-SHA SHA`）。手元のコードが違うと `local merge code differs from source_ref` で止まる。各 run の成功・依頼 ID を確かめ、artifact を一度取得して検証・コピーする。FFmpeg・フォントは使わない。receipt の `workflow_run` は `"local"` になる。
