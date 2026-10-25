# 通常制作の正本：Parakeet → GPU/NVENC

この手順を使う。旧Whisper・2分割・CPU描画・単純cutを通常制作へ戻さない。公開mainや古いrunを最新構成とみなさず、この変更を含むpush済みの完全SHAを明示する。workerのworkflowもこの版のdeploy原本へ同期する。

| 段階 | 実施する処理 | 場所 |
| --- | --- | --- |
| 1 | URL・設定・依頼IDを確定。元音声が一致する保存済み音声があれば再利用 | 制御側・runner |
| 2 | 音声取得、16kHz mono WAVへ一度復号、静かな位置で4分割 | runner |
| 3 | L4/T4最大4台。NeMo準備と部分音声取得を並行しParakeetで認識 | Colab GPU |
| 4 | 結果を元時刻へ結合。全文を話題分け・字幕で共用 | runner |
| 5 | 候補映像・静止画・未承認の下書き作成。採用候補を確認し承認JSONへ記録 | runner・担当者 |
| 6 | --clips Nで最大5件を投入。GPUでNVENCの実エンコード能力を確認してから素材を取得し字幕描画 | Colab GPU |
| 7 | FFmpeg終了状態、FFprobeの尺・寸法・音声、ASS/SRT・配置を確認 | worker |
| 8 | 同じ描画SHAでmerge --local。完成済みファイルを集約し納品 | 制御側 |

描画は `submit_audio_stage.py --stage render --clips N --config-file config.landscape-long-preview.example.json --source-ref FULL_SHA --previous-run RUN --previous-request ID --review-file /private/approved.json`。既定はColab GPU、360p、NVENC。1080p依頼時はconfig.landscape-long.example.json。CPU描画を明示しても拒否し、NVENCが使えないGPUからCPUエンコードへ黙って切り替えない。GPUの空きに応じて待機するため、5件すべての同時稼働は保証しない。

音声補正は描画中の1パスloudnormとリミッター。事前の2パス測定、完成後の全編デコード・音量測定は実行しない。quality.jsonと共有summaryにはverification_scope、full_decode_performed=false、loudness_measured=false、音量・真ピークnullを保存する。passedは軽量検査の合格で、全編復号・音量値・内容品質の達成を意味しない。

解析・preview・描画workflowはParakeet導入コミット849fad1の子孫以外を起動前に拒否する。既存の成功runの読み取り・回収は継続可能。既存のASRキーと依頼IDの整合条件は維持する。古いASRを新モデルの測定記録として扱わない。

描画のtimings.wall_secondsは実際の経過時間。timings.totalは各区間の待機・取得・描画の作業時間合計として互換性のため残す。並列run全体はActionsの最初の開始から最後の終了までで測る。両者を合算しない。

GPU/NVENCとASR高速化の根拠はdocs/ASR.mdを参照。GPUの確保とNVENC対応は別であり、今回の変更による全体短縮率・実機NVENC成功は未測定。
