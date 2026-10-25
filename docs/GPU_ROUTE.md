# 通常制作の正本：Parakeet → GPU/NVENC

この手順を使う。旧Whisper・2分割・CPU描画・単純cutを通常制作へ戻さない。公開mainや古いrunを最新構成とみなさず、この変更を含むpush済みの完全SHAを明示する。workerのworkflowもこの版のdeploy原本へ同期する。

| 段階 | 実施する処理 | 場所 |
| --- | --- | --- |
| 1 | URL・設定・依頼IDを確定。元音声が一致する保存済み音声があれば再利用 | 制御側・runner |
| 2 | 音声取得、16kHz mono WAVへ一度復号、静かな位置で4分割 | runner |
| 3 | L4/T4最大4台。NeMo準備と部分音声取得を並行しParakeetで認識 | Colab GPU |
| 4 | 結果を元時刻へ結合。全文を話題分け・字幕で共用 | runner |
| 5 | 候補映像・静止画・未承認の下書き作成。採用候補を確認し承認JSONへ記録 | runner・担当者 |
| 6 | --clips Nで1件のバッチを投入。1台のGPUで最大5本を順次描画し、次の区間取得を先行 | Colab GPU |
| 7 | FFmpeg終了状態・FFprobe・字幕に加え、完成映像の全デコードと完成AACの音量・真ピークを1回の走査で確認 | worker |
| 8 | 通常バッチの完成成果物を--collectで回収・納品。個別修正のpartだけmerge --localで集約 | 制御側 |

描画は `submit_audio_stage.py --stage render --clips N --config-file config.landscape-long-preview.example.json --source-ref FULL_SHA --previous-run RUN --previous-request ID --review-file /private/approved.json`。既定はColab GPU、360p、NVENC。1080p依頼時はconfig.landscape-long.example.json。CPU描画を明示しても拒否し、NVENCが使えないGPUからCPUエンコードへ黙って切り替えない。通常は1台だけを確保し、環境構築と解析素材の転送を1回にまとめる。GPUの空き待ちは残る。--clips Nは承認本数と一致させ、設定のclipsも同じ本数にする。--clip-index Nは字幕修正など1本だけの再描画用。

音声補正は描画中の1パスloudnormとリミッター。事前の2パス測定は行わない。完成後は映像デコードとebur128を1つのFFmpegで同時に実行する（-vnは付けない）。破損・音声無測定・無音・-17〜-15 LUFS外・真ピーク-1 dBTP超はstrictで拒否する。成功時のverification_scopeはsingle_pass_full_decode_loudness_and_captions。実測値と検査実施フラグを保存する。内容品質の確認は別に行う。

解析・preview・描画workflowはworkerに固定した現行制作コミットの子孫以外を起動前に拒否する。既存の成功runの読み取り・回収は継続可能。既存のASRキーと依頼IDの整合条件は維持する。古いASRを新モデルの測定記録として扱わない。

描画のtimings.wall_secondsは実際の経過時間。timings.totalは各区間の待機・取得・描画の作業時間合計として互換性のため残す。並列run全体はActionsの最初の開始から最後の終了までで測る。両者を合算しない。

GPU/NVENCとASR高速化の根拠はdocs/ASR.mdを参照。GPUの確保とNVENC対応は別であり、今回の変更による全体短縮率・実機NVENC成功は未測定。

## 提案の採否

19分16秒・ASR7分06秒は旧構成の測定であり、新構成の短縮見込みには使わない。NVENCの1本5〜15秒や全体7〜8分は未実測。フィルターはCPUで動き、長い動画の処理時間は尺・解像度・GPUに依存する。

16kHz monoの軽量解析音声は、最終映像の音声と帯域・チャンネル・符号化・時刻が異なるため、最終AACの検査の代わりにしない。事前測定は復活させず、最終AACを1回の走査で検証する。

ASRは実績のあるParakeet4分割WAVを維持。Opus/AACで精度が全く落ちない、単一GPUが必ず速いとは判断しない。入力取得は既に軽量renditionを優先する。音声取得時の一時映像をプレビューに流用する変更は、映像の有無・時刻対応・保存容量・後始末を確認してから実施する。パイプ化によって並列取得を失う変更は行わない。

wall_clock_secは描画バッチ本体の実経過時間。substep_breakdownは先行取得の待ち・取得・描画・最終検査の内訳。queue_wait_secとsetup_secはこの層で測っていないためnull（ゼロ秒と偽らない）。起動待ち・環境構築はColab制御ログで別に確認する。内訳は重複を含むので合算してwall timeとしない。
