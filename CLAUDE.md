# Claude Code

共通の作業ルールは [AGENTS.md](AGENTS.md)、コードの入口は [INDEX.md](INDEX.md)、検証方法は [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) を参照する。

- 大きなファイルは検索して必要な範囲を読む。依存ロック・ログ・生成物を毎回丸ごと読み込まない。
- worktreeや別checkoutへ作業を渡す場合は、絶対パス、基点SHA、担当範囲、検証結果を明示する。
- `.claude/settings.json` のクライアント権限に従う。生成物の確認を依頼された場合はユーザー指示を優先し、無関係な私有データへ探索を広げない。

担当モデル、並列数、委任の有無をこのファイルでは固定しない。
