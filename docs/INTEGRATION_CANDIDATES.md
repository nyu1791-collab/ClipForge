# 活用する公開リポジトリの調査

調査日: 2026-10-23 UTC。Tavily で候補を探し、公式 GitHub API と README、現在の pyproject.toml で確認した。スター数は調査時点の値であり、品質保証ではない。

| リポジトリ | Stars | ライセンス | ClipForge への判断 |
| --- | ---: | --- | --- |
| [aquasecurity/trivy](https://github.com/aquasecurity/trivy) | 38,642 | Apache-2.0 | 今回のCIへ導入。vuln/licenseを報告し、.venvを除外。ActionはコミットSHA固定。 |
| [SubtitleEdit/subtitleedit](https://github.com/SubtitleEdit/subtitleedit) | 14,683 | MIT | 新規の活用候補。字幕ファイルを別アプリで開き、読む速さ・行幅・波形を目視確認する。描画パイプラインへ組み込まず、品質確認の比較対象とする。 |
| [snakers4/silero-vad](https://github.com/snakers4/silero-vad) | 10,505 | MIT | faster-whisper内の既存VADを利用する方針を維持。別のPyTorch実行環境を足さず、保留中の2分割ASR実測で評価する。 |
| [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | 5,282 | BSD-3-Clause | scenedetect-headlessとして既存依存。ショット境界に切り出し位置を合わせる用途を優先。話題境界やワイプ検出そのものを解決するものではない。 |
| [google/budoux](https://github.com/google/budoux) | 1,808 | Apache-2.0 | 既存依存。今回のfugashi/UniDicによる単語内改行保護と併用し、語句の自然な区切りを改善する。別の分かち書きライブラリを追加しない。 |
| [tkarabela/pysubs2](https://github.com/tkarabela/pysubs2) | 443 | MIT | 既存依存。ASS/SRTの読み書きを一元化する。字幕描画エンジンの置換は不要。 |

Subtitle Editの具体的な確認項目は[公式の画面説明](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/features/main-window.md)にあるChars/sec、Words/min、Pixel width、波形との同期。日本語の7字/秒基準はClipForge側の基準を維持する。実際の3クリップをこのツールで検証した実績はまだない。

## 次に取り込む順序

1. 今回の字幕・ワイプ・キャッシュ修正を既存レビュー担当者が確認する。実配信3クリップを同じ条件で再描画し、単語内改行、7字/秒超過、記号、ワイプ、エンコード後のtrue peakを再測定する。今回の自動テスト合格を実映像の合格として扱わない。
2. Trivyが報告した任意のmedia-fetch依存curl-cffi 0.13.0のHIGH/CVE-2026-33752を別の依存更新として評価する。スキャナのFixedVersionは0.15.0。lock更新、利用箇所の互換性と到達性を確認してから取り込む。今回は報告のみで、取得経路を変更しない。
3. 未反映のPR #12 bundle（33cea7e）を取得・レビューしてから、同じ実配信で1台ASRとSileroによる2分割の話題境界・時間・費用を比較する。現時点ではbundleがこの端末にもGitHubにもなく、比較は未実施。
4. 手動字幕確認が必要な場合だけSubtitle Editを別アプリとして利用する。既存BudouX、PySceneDetect、pysubs2を活かし、同じ目的の実行時依存を増やさない。

WhisperX、whisper-timestamped、stable-tsは追加しない。YouTubeのボット判定を回避する取得経路の追加・改善は対象外。公開リポジトリには実配信の音声・動画・チャット本文・認証情報を置かない。
