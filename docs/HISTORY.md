# 過去の調査・計測

現在の規則は [品質と納品](QUALITY_DECISIONS.md)、実行は [運用](OPERATIONS.md)、検証は [開発](DEVELOPMENT.md) を参照する。ここにある結果は指定日時・コード・素材の記録で、現在の提供条件や全入力への保証ではない。

## 過去の品質規則と検証

2026-10-22・23に、字幕の語境界・文末・実フォント幅・表示時間、ASR再利用、音量とAAC後の真ピーク、画面分析の省メモリ化を比較した。採用中の数値だけを品質規則へ残し、途中の候補・取り消し済みの規則は通常の作業指示から外した。

- 保存ASRを再利用した比較では認識331秒から0.16秒、全体約496秒から162秒だった。1つの素材・環境での結果。
- 4時間配信の2分割ASRでは約10分のrunとCU 0.18を記録した。別の通し実行では分析・プレビュー14分08秒、描画3本4分55秒、ローカル結合13秒。コードや取得条件が違う測定を速度保証として比較しない。
- GPUの同じコード・認識キーでも文字列と話題数に揺れがあった。分割の影響は同一条件の比較で区別する。
- 真ピーク−0.8 dBTPを含む通し実行を「合格」とした旧記録がある。処理完走と−1 dBTP以下の達成を区別し、現行のゲートが音量超過を警告にすることも品質規則へ明記した。
- PR #12・#13は `39f4edf` でmainへ統合済み。旧資料の「bundle未取得」「未統合」は当時の状態。

## 長時間音声のメモリ見積もり

6時間×16kHzのfloat32保持配列は約1.38GB、読み込み時の一時配列を含む概算ピークは約3.46GB。ASR/VAD・モデル・特徴量は別。実測のピークRSSや6時間の完走を保証する値ではない。

## Colab・取得・依存の調査

Colab公式CLI SDKの認証・割り当て・転送・解放と、GPUの合成音声処理を調査・検証した。YouTube取得は経路・時刻・サービス判定に依存し、過去の成功から常時成功を保証しない。現在の設定は [Colab](COLAB.md) と [YouTube取得](YOUTUBE_TRANSPORT.md) を参照する。依存スキャンの未解決課題は [開発](DEVELOPMENT.md#継続して扱う依存の課題) に残す。

## 統合前の資料

詳細な測定表・根拠・取り消しの経過はGit履歴に保持する。以下は統合前の固定コミット `c857bb9` の資料で、追加の必読文書ではない。

| 記録 | 固定版 |
| --- | --- |
| 品質計測 | [QUALITY_REVIEW](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/archive/QUALITY_REVIEW.md) |
| 旧品質決定 | [QUALITY_DECISIONS](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/archive/QUALITY_DECISIONS_2026-10-23.md) |
| 信頼性改善 | [RELIABILITY_IMPROVEMENTS](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/archive/RELIABILITY_IMPROVEMENTS.md) |
| ライブラリ調査 | [INTEGRATION_CANDIDATES](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/archive/INTEGRATION_CANDIDATES.md) |
| Colab調査 | [COLAB_RESEARCH](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/archive/COLAB_RESEARCH.md) |
| YouTube診断 | [YOUTUBE_DIAGNOSTICS](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/archive/YOUTUBE_DIAGNOSTICS.md) |
| 構成詳細 | [ARCHITECTURE](https://github.com/nyu1791-collab/ClipForge/blob/c857bb92eec54dfe7deac77aae8290f3e64b8e80/docs/ARCHITECTURE.md) |
