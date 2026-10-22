---
description: 不具合を調査し、再現・修正・回帰検査まで進める
agent: clipforge
---
対象: $ARGUMENTS

AGENTS.mdとdocs/RICH_BATCH.mdを読み、git statusと既存差分を確認する。
不具合をコードと合成データで再現し、原因を説明できる最小修正と回帰検査を実装する。
オフライン検査、git diff --check、最終差分のレビューまで実施する。
オフライン検査の共通入口は `uv run --offline python harness/check_pipeline.py` を使う。
動画取得・ASR・描画・モデル変更・ジョブ投入・公開は実行しない。
実行した検査と結果、実行していない検証を明記する。
