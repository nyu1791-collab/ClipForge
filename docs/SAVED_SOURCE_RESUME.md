# 保存済み素材と動画分析からの再開

制御VMで素材・ASR・採用区間・実行IDの対応を保存し、重い認識と描画は通常どおり所有Colab GPUで実行する。YouTubeで取得済みの素材を使う場合、描画時に同じURLを再取得しない。

## 素材の再利用

非公開ai-model-workersのReleaseに`source.mp4`と`acquisition-receipt.json`を保存する。取得記録には`status: success`、`source_url`、`duration`、`height`、`files: ["source.mp4"]`を記録する。公開Artifactへ素材を出さない。

描画を起動する制御プロセスへ、`CLIPFORGE_SAVED_SOURCE_RELEASE=clipforge-youtube-source-RUN_ID`を明示する。制御側がこの指定をGPUへ渡し、GPUが非公開Releaseから素材と取得記録を取得する。取得状態・元URL・サイズ・動画長・360p以下を確認して再利用する。指定素材が不正な場合は停止し、別の取得を始めない。

## 既存の動画分析の再利用

通常の採用JSONに`selection_basis: saved_video_analysis`と`source_url`を指定する。`analysis_key`は完成ASRの分析記録から取得する。各clipには`candidate_id`、`start`、`end`、`title`、`reason`、`confidence`を設定する。候補を新しく生成するモデル呼び出しは行わない。

映像分析時の秒単位の境界は、ASRの単語時刻と一致するとは限らない。GPUを起動する前に、完成ASRを使って区間境界が単語の途中に入らないよう調整し、変更前後の秒数を記録する。各区間が300秒以上、重複なし、元動画の範囲内であることを保つ。字幕は同じ完成ASRから作る。

## ASRと結果の保持

分割ASRの非公開Releaseは、入力WAVと認識結果を同じ場所へ保存する。`split_asr_job.py`はこれを自動削除しない。結合結果の永続保存を確認する前に、一時入力として削除すると成功部分のASRも失われるためである。

Colabの接続終了処理でエラーが出ても、所有GPUの解放と保存結果の回収を継続する。認識・描画の失敗は別に判定し、接続警告を成功した推論と同一視しない。自動再認識・再描画は行わない。

制御VMへ元動画、完成ASR、採用JSON、Run番号を保存する。認証情報を対応記録・Git・公開Artifactへ含めない。通常経路とGPU上限は[GPU_ROUTE.md](GPU_ROUTE.md)に従う。
