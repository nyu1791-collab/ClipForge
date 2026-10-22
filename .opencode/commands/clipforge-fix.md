---
description: 指定変更を実装し、オフラインで検証する
agent: clipforge
---
変更要求: $ARGUMENTS

AGENTS.mdと既存差分を確認し、要求を実装する。既存の未コミット変更を保持する。
必要な回帰検査とオフライン検査を実行し、git diff --checkと差分レビューを行う。
オフライン検査の共通入口は `uv run --offline python harness/check_pipeline.py` を使う。
重い動画処理、モデル変更、ジョブ投入、push、マージ、公開はしない。
変更内容・実測の検査結果・未確認事項を報告する。
