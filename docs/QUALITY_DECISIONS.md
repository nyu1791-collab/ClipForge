# 現行の品質・速度方針

通常経路はGPU_ROUTE.md。文字起こしはParakeetのみ、4分割WAV、NeMo準備と入力取得の並行を維持する。描画はGPU/NVENC。旧Whisper、CPU描画、cut経由の制作、事前音量2パス、完成後の全編デコード・音量再測定は通常経路から外した。

## 字幕と内容

BudouXとfugashiで文節・語の境界を使い、実フォント幅で最大2行へ配置する。横字幕の目標は7字/秒。ASS/SRTの本文・時刻、文字欠け、行数、重なり、画面超過を確認する。単語時刻や本文を警告削減のために改変しない。承認JSONのcaption_editsで必要な字幕訂正を行い、該当1本だけ再描画する。話題の面白さ・完結性・発話の正確さは候補確認で扱う。

## 画面とエンコード

通常設定はconfig.landscape-long-preview.example.json（16:9、360p、NVENC）。1080p依頼時はconfig.landscape-long.example.json。見出しは左上1.4倍、字幕は下中央。固定画面の上への持ち上げは高さの32%まで。ソースfpsは30を上限に保持する。字幕・overlay等のフィルターはCPUで実行されるため、NVENCだけで描画全体がGPU化されるわけではない。

## 音声と軽量検査

描画中に1パスloudnormとリミッターを適用し、48kHz AACへ出力する。完成値の別測定は行わない。FFmpeg終了コードとFFprobeの尺・寸法・音声情報、字幕を確認する。検査を通った場合だけファイルを確定する。

quality.jsonと共有summaryのverification_scopeはencode_exit_probe_and_captions、full_decode_performed=false、loudness_measured=false。integrated_lufsとtrue_peak_dbtpはnull。passedはこの範囲の合格であり、全編復号・無音検出・実測LUFS・真ピーク・内容の合格を意味しない。旧レポートの測定値を今回の結果へ転記しない。

## 計測

利用者の待ち時間はwall_seconds、各部分の作業時間合計はtotalとして別記録する。並列runの経過時間は最初の開始から最後の終了で測り、部分の時間を足さない。7分06秒は旧Whisperの過去記録。Parakeetの認識本体・入力・準備待ちはASR.mdの実ログ内訳を使う。今回のNVENC変更の短縮率は実機比較前には示さない。
