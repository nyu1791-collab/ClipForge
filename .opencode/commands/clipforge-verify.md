---
description: オフライン検査と差分の確認
agent: clipforge
---
検証対象: $ARGUMENTS

git statusとgit diffを確認する。
uv run --offline python harness/check_pipeline.py
git diff --check
を実行し、失敗があれば原因を調査する。依存が不足する場合はその事実を報告する。
この入口はlint・format・全回帰テスト・カバレッジ・設定/スキーマ・差分をまとめて検査する。
実動画・外部サービス・有料モデルの検証はしない。
検査名、件数、終了状態、未確認事項を区別して報告する。
