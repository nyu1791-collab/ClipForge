# GPU経路の手順（Kick URL → 切り抜き）

この手順だけを使う。GPUは次の経路だけで使う。

1. 非公開 `nyu1791-collab/ai-model-workers` の GitHub Actions（`colab-auto`）が、指定Googleアカウントの Colab T4 を起動する。
2. その Colab ランタイムで文字起こし（ASR）を行い、終わったら解放する。

対象外（使わない）: ローカル動画の `--input`、Gemini による選定、`--worker cpu`、`--asr-parts 1`（連続ASR）、GitHub上のGPU以外の経路、ほかのGoogleアカウント、ほかのGPUクラウド、有料プランの購入。

## 工程（この順で固定）

| 段 | 内容 | 担当 | 主な出力 |
| --- | --- | --- | --- |
| S0 前提確認 | SHAを固定し、origin に push 済みか確認。作業ツリーが clean か確認。worker の workflow が ClipForge の `deploy/` と一致するか確認 | 私 | 固定したSHA |
| S1 分析＋プレビュー | `submit_job.py --audio-analysis-url` を `--worker colab-auto --asr-parts 4 --auto-previews --wait` で投入。音声取得 → 4部分のASR（T4 2台・L4 2台）→ 話題分割 → 同じrunのCPUジョブでプレビュー・未承認の下書き | Actions | `topics.json`・`review-draft.json`・`preview-contact-sheet.jpg`・`asr-receipt.json` |
| S2 確認 | 話題・下書き・静止画シートを読み、候補を決める。採用する候補だけ映像を確認する | 私 | 採用候補の一覧 |
| S3 承認ファイル | 採用候補だけを残し、`video_reviewed: true` と確認した根拠を書く。確認していない候補は含めない | 私 | 承認ファイル（JSON） |
| S3.5 字幕用の文字起こし | `submit_captions.py --previous-run ANALYSIS_RUN_ID --source-ref "$SHA" --review-file 承認ファイル --wait` で、承認した区間だけを Parakeet（Colab GPU 1台）で起こし直し、字幕データを `clipforge-captions-RUN_ID` artifact に保存 | Actions（Colab GPU） | 字幕のrun ID |
| S4 描画 | `submit_audio_stage.py --stage render --worker colab-auto --clips N --config-file config.landscape-chat.example.json` を承認ファイルで投入。1本ずつ並列に描画 | Actions（Colab CPU） | 各クリップの依頼ID・run ID |
| S5 結合 | 全クリップの run が成功したことを確認し、`--stage merge --local` で制御VMに結合 | 私 | `merged-*/delivery/` |
| S6 検査 | `e2e-summary.json`・各 `quality.json`・フレームを確認。1920×1080・30fps、音量、真ピーク、字幕の位置 | 私 | 合否の記録 |
| S7 解放確認 | 各 run のログで `Owned Colab runtime released` と `active runtimes 0` を確認。`tools/run_timings.py RUN_ID` で時間とCUを記録 | 私 | 時間・CUの表 |
| S8 納品 | 動画を作業用の scratchpad へコピーして報告。Git・公開Artifactsには入れない | 私 | 納品先の絶対パス |

## S0 前提確認（毎回）

```bash
cd /home/user/ClipForge
git status --short                       # 空であること
SHA=$(git rev-parse HEAD)
git ls-remote origin ccr-7cea37c1-n7u7zz  # 同じSHAであること（未pushなら投入しない）
```

workflow の一致は、非公開 worker の `clipforge.yml` と `clipforge-stages.yml` のブロブSHAを、`deploy/clipforge.yml` と `deploy/audio-stages.yml` の `git hash-object` と比べて確かめる。違えば投入せず、同期を先に行う。

## S1 分析＋プレビュー

```bash
python tools/submit_job.py --audio-analysis-url 'URL' \
  --worker colab-auto --ref "$SHA" --asr-parts 4 --auto-previews \
  --wait --wait-timeout 18000 --output /home/user/private/NAME-previews
```

完了すると `--output` に分析結果が回収される。`--status REQUEST_ID` で状態を確認できる。

## S2〜S3 確認と承認

- `topics.json` と `review-draft.json` で候補の話題を読む。
- `preview-contact-sheet.jpg` と `transcripts/` の本文で、候補の内容を確かめる。
- 採用候補は、`video_reviewed: true` にする前に静止画を見る。全編は見ていない場合、その旨を `reason` に書く。
- 承認ファイルは `review-draft.json` と同じ形式（`analysis_key`・`clips`）で、採用候補だけを含める。`clips` は1件以上。

## S4 描画

```bash
python tools/submit_audio_stage.py --stage render --worker colab-auto --clips N \
  --config-file config.landscape-chat.example.json \
  --previous-run ANALYSIS_RUN_ID --previous-request ANALYSIS_REQUEST_ID \
  --source-ref "$SHA" --review-file /home/user/private/NAME-approved.json
```

`--previous-run` と `--previous-request` は S1 の run とその依頼ID。描画は各クリップが別の依頼IDになり、並列に走る。S3.5 の字幕を使うときは `--captions-run 字幕のrun ID` を加える。描画の workflow がその artifact を承認ファイルへ差し込み、全編の Whisper の代わりに Parakeet の字幕で描画する（区間・話題・分析キーが一致しないと止まる）。最終確認で直した字幕は、承認ファイルの各クリップの `captions` に詰め直して、`--captions-run` なしで描画し直す。`--clip-index` を1本ずつ同時に投入すると、手元の多重投入防止で2本目以降が止まる。並列描画は `--clips N` の1回で投入する。
配信画面にコメント欄が映っている配信で話題を長く取るときは、S1 の `--config` と S4 の `--config-file` の両方に `config.landscape-long.example.json` を渡す（[AUDIO_FIRST.md](AUDIO_FIRST.md) の「長めの話題と左上の見出し」）。

## S5 結合（制御VM）

```bash
git worktree add --detach /tmp/merge-SHORT "$SHA"    # 描画と同じSHAを用意
cd /tmp/merge-SHORT
python tools/submit_audio_stage.py --stage merge --local --source-ref "$SHA" \
  --part-runs RUN1:REQ1,RUN2:REQ2,RUN3:REQ3 --output /home/user/private/merged-NAME
```

`--local` の結合は、描画に使ったSHAをチェックアウトした作業ツリーで行う。`--config-file` は付けない（付けると引数エラーで止まる）。手元のコードが違うと `local merge code differs from source_ref` で停止する。

## S6 検査の基準

- `e2e-summary.json` の `passed` が全クリップで true。
- 1920×1080、30fps、H.264、音声あり。
- 音量 −15〜−17 LUFS の範囲、真ピーク −1.0 dBTP 以下。
- 字幕は下中央。固定画面の上だけを持ち上げる。文字が画面外へ出ない。
- 字幕の警告（読みの速さ・固定画面との重なり）は記録し、基準を超えたら報告する。

## S7 解放とCUの記録

```bash
python tools/run_timings.py RUN_ID
```

各 run に `Owned Colab runtime released` と `active runtimes 0` が出ていれば解放は確認済み。CUは、アカウント全体の残高差として記録する（並列の run と共有するため）。

## ルール

- 1つの依頼が失敗しても自動では再試行しない。失敗の原因を読み、同じ条件の新規依頼が必要なら私が判断して投入する。
- GPUの割り当てが拒否されたら停止する。別アカウント・別GPU・有料プランへは切り替えない。
- 分析・描画・結合は、同じSHAで行う。途中でコードを変えたら、新しいSHAで最初からやり直す。
- 承認は、私が確認した候補にだけ付ける。確認していない候補を承認しない。
- 動画・音声・文字起こしの本文は、Git・公開Artifacts・チャットの貼り付けに含めない。
- 1本の切り抜きのCUは描画だけで約0.04、分析は約0.15（2026-10-23の実測）。残高を見てから投入する。

## 既知の制約

- 2分割ASRの前半と後半で表記が揺れることがある（`docs/QUALITY_DECISIONS.md` の 7.5 節）。
- Kickの音声取得は回によって2〜5分揺れる。
- 字幕の基準は、1本の配信で調整したもの。ほかの配信では見直す。
- 横動画の本数は3〜5本。2本は特例で、2026-10-23に1回だけ行った。描画は `796a91c`（設定 `config.landscape-chat-2clips.example.json`、`clips: 2`）、採用は topic-313 と topic-195。特例の描画は、設定ファイルの `clips` の値と承認ファイルの本数が一致し、プレビュー段階で設定の本数以上の話題を指定している必要があった。特例は利用者が本数を2本と明示した場合だけ使い、その都度、コミットSHAと理由を記録する。
- 既知の問題: `--collect` は描画の成果物にある `gpu-receipt.json` を記録外として拒否する。描画の結合は `--stage merge --local` を使い、描画と同じSHAの作業ツリーで行う。
