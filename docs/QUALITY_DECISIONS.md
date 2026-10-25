# 現行の品質・速度方針

通常経路はGPU_ROUTE.md。文字起こしはParakeetのみ、4分割WAV、NeMo準備と入力取得の並行を維持する。描画はGPU/NVENC。旧Whisper、CPU描画、cut経由の制作、事前音量2パス、完成後の映像デコードと音量測定は1走査へ統合した。

## 字幕と内容

BudouXとfugashiで文節・語の境界を使い、実フォント幅で最大2行へ配置する。横字幕の目標は7字/秒。ASS/SRTの本文・時刻、文字欠け、行数、重なり、画面超過を確認する。単語時刻や本文を警告削減のために改変しない。承認JSONのcaption_editsで必要な字幕訂正を行い、該当1本だけ再描画する。話題の面白さ・完結性・発話の正確さは候補確認で扱う。

## 画面とエンコード

通常設定はconfig.landscape-long-preview.example.json（16:9、360p、NVENC）。1080p依頼時はconfig.landscape-long.example.json。見出しは左上1.4倍、字幕は下中央。固定画面の上への持ち上げは高さの32%まで。ソースfpsは30を上限に保持する。字幕・overlay等のフィルターはCPUで実行されるため、NVENCだけで描画全体がGPU化されるわけではない。

## 音声と統合検査

事前音量測定は行わず、描画中の1パスloudnormとリミッターで48kHz AACへ出力する。完成MP4を1回読み、映像デコードとebur128音量・真ピーク測定を同じFFmpegで実行する。-vnは付けず、破損エラー・欠けた測定結果を失敗とする。無音、-17〜-15 LUFS外、真ピーク-1 dBTP超をstrictで拒否する。字幕の整合性・配置とFFprobeの検査も維持する。

検査の成功時だけfull_decode_performed=true、音声を測れた場合だけloudness_measured=trueとし、実測値を保存する。無音の-inf等はJSONではnullにして失敗理由を記録する。qualityと共有summaryのverification_scopeはsingle_pass_full_decode_loudness_and_captions。passedを内容の合格と混同しない。

## 計測

利用者の待ち時間はwall_seconds、各部分の作業時間合計はtotalとして別記録する。並列runの経過時間は最初の開始から最後の終了で測り、部分の時間を足さない。7分06秒は旧Whisperの過去記録。Parakeetの認識本体・入力・準備待ちはASR.mdの実ログ内訳を使う。今回のNVENC変更の短縮率は実機比較前には示さない。
