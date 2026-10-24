# 運用手順（配信 URL → 切り抜き）

この手順だけを使う。GPU は非公開 `nyu1791-collab/ai-model-workers` の Actions が、指定 Google アカウントの Colab で起動し、終わったら解放する。別アカウント・別 GPU クラウド・有料プランの購入へは切り替えない。

## 工程

| 段 | 内容 | 担当 | 主な出力 |
| --- | --- | --- | --- |
| S0 前提確認 | SHA を固定し、origin に push 済みか、作業ツリーが clean か、worker の workflow が `deploy/` と一致するかを確かめる | 私 | 固定した SHA |
| S1 解析とプレビュー | `submit_job.py --auto-previews` を投入する。音声取得 → 最大4台の Parakeet → 話題分け → 同じ run の CPU ジョブで候補映像と未承認の下書き | Actions | `topics.json`・`review-draft.json`・`preview-contact-sheet.jpg`・`asr-receipt.json` |
| S2 確認 | 話題・下書き・静止画シートを読み、候補を決める。採用する候補だけ映像を見る | 私 | 採用候補 |
| S3 承認ファイル | 採用候補だけを残し、`video_reviewed: true` と確認した根拠を書く | 私 | 承認ファイル（JSON） |
| S4 描画 | `submit_audio_stage.py --stage render --worker colab-auto --clips N`。1本ずつ並列に描画 | Actions（Colab CPU） | 各クリップの run |
| S5 結合 | 全クリップの run の成功を確かめ、`--stage merge --local` で制御VMに結合 | 私 | `merged-*/delivery/` |
| S6 検査 | `e2e-summary.json`・各 `quality.json`・フレーム（解像度・30fps・音量・真ピーク・字幕の位置） | 私 | 合否 |
| S7 解放確認 | 各 run の `Owned Colab runtime released` と `active runtimes 0`。`tools/run_timings.py RUN_ID` で時間と CU を記録 | 私 | 時間・CU |
| S8 納品 | 動画を作業用の scratchpad へコピーして報告する。Git・公開 Artifacts には入れない | 私 | 納品先の絶対パス |

確認後に直したい字幕は、承認ファイルの該当クリップに `caption_edits` を足し、その1本だけ `--clip-index N` で描画し直す（S4）。結合は差し替えた run で `--part-runs` を組み直す（S5）。

## S0 前提確認

```bash
git status --short                 # 空であること
SHA=$(git rev-parse HEAD)
git ls-remote origin BRANCH        # 同じ SHA であること（未 push なら投入しない）
```

worker の `.github/workflows/clipforge.yml` と `clipforge-stages.yml` のブロブ SHA を、`deploy/clipforge.yml` と `deploy/audio-stages.yml` の `git hash-object` と比べる。違えば投入せず、先に同期する。

## S1 解析とプレビュー

```bash
python tools/submit_job.py --audio-analysis-url 'URL' \
  --ref "$SHA" --config config.landscape-long.example.json --auto-previews \
  --wait --wait-timeout 18000 --output /private/NAME-previews
```

- 文字起こしは既定で4台。`--asr-parts 1`〜`4` で台数を変えられる。各部分は L4 と T4 のどちらでも取り、空きが無ければ他の部分の解放を待つ（[ASR.md](ASR.md)）。
- 話題の長さは設定で決まるため、S1 の `--config` と S4 の `--config-file` には同じ話題設定（`topic_min_seconds`・`max_seconds`）の設定を渡す。
- YouTube を WARP 経由で取るときは `--youtube-audio-transport warp` を足す（[YOUTUBE_TRANSPORT.md](YOUTUBE_TRANSPORT.md)）。
- 完了すると `--output` に回収される。`--status REQUEST_ID` で状態を確かめられる。

## S2〜S3 確認と承認

- `topics.json` と `review-draft.json` で候補の話題を読む。
- `preview-contact-sheet.jpg` と `transcripts/` の本文で候補の内容を確かめる。
- 採用候補は、`video_reviewed: true` にする前に静止画を見る。全編を見ていないときは、その旨を `reason` に書く。
- 承認ファイルは `review-draft.json` と同じ形式（`analysis_key`・`clips`）で、採用候補だけを含める。書式は [AUDIO_FIRST.md](AUDIO_FIRST.md#承認ファイル)。

## S4 描画

```bash
python tools/submit_audio_stage.py --stage render --worker colab-auto --clips N \
  --config-file config.landscape-long-preview.example.json \
  --previous-run ANALYSIS_RUN_ID --previous-request ANALYSIS_REQUEST_ID \
  --source-ref "$SHA" --review-file /private/NAME-approved.json
```

- 確認用と納品は `config.landscape-long-preview.example.json`（360p・veryfast）を基本とし、1080p は頼まれたときだけ `config.landscape-long.example.json` で描画する。
- 各クリップは別の依頼 ID になり、並列に走る。並列は `--clips N` の1回で投入する（`--clip-index` を1本ずつ同時に投入すると、手元の多重投入防止で2本目以降が止まる）。

## S5 結合（制御VM）

```bash
git worktree add --detach /tmp/merge-SHORT "$SHA"    # 描画と同じ SHA を用意する
cd /tmp/merge-SHORT
python tools/submit_audio_stage.py --stage merge --local --source-ref "$SHA" \
  --part-runs RUN1:REQ1,RUN2:REQ2,RUN3:REQ3 --output /private/merged-NAME
```

手元のコードが描画の SHA と違うと `local merge code differs from source_ref` で止まる。`--config-file` は付けない。

## S6 検査の基準

- `e2e-summary.json` の `passed` が全クリップで true。
- 16:9、30fps、H.264、音声あり。
- 音量 −15〜−17 LUFS、真ピーク −1.0 dBTP 以下。
- 字幕は下中央。固定画面の上だけを持ち上げ、画面外へ出ない。
- 字幕の警告（読みの速さ・固定画面との重なり）は記録し、基準を超えたら報告する。

## ルール

- 1つの依頼が失敗しても自動では再試行しない。原因を読み、同じ条件の新規依頼が必要なら判断して投入する。例外は文字起こしの部分で、割り当て後にランタイムが失敗した部分だけ、依頼の中で1回やり直す（[ASR.md](ASR.md#失敗したとき)）。
- GPU が割り当てられなければ、待つか止まる。別アカウント・別 GPU・有料プランへは切り替えない。
- 解析・描画・結合は同じ SHA で行う。途中でコードを変えたら、新しい SHA で最初からやり直す。
- 承認は確認した候補にだけ付ける。
- 動画・音声・文字起こしの本文は、Git・公開 Artifacts・チャットの貼り付けに含めない。
- CU はアカウント全体の残高から引かれる。残高を見てから投入する（2026-10-23 の実測では、描画1本で約0.04）。

## 既知の制約

- 部分の境目の直後で表記が揺れることがある（分け目は静かな位置に置く）。
- Kick の音声取得は回によって2〜5分揺れる。
- 字幕の基準は1本の配信で調整したもの。ほかの配信では見直す。
- `--collect` は描画の成果物にある `gpu-receipt.json` を記録外として拒否する。描画の結合は `--stage merge --local` を使う。
