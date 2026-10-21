# GitHub・AI運用

## リポジトリの権限

リポジトリはpublic。Actionsを有効化し、既定のworkflow tokenを書き込み可能にする。
ActionsからPRを作成・承認できる設定も有効化する。
`Build package` のjobは `contents: write`、`pull-requests: write`、`issues: write` を宣言する。
権限は実行中の `ClipForge` に対するもので、GitHub全アカウントや他社サービスの全権限ではない。
AIという共通ユーザーへ権限を付ける仕組みではなく、各エージェントが利用する認証に権限が付く。

初期状態で必須レビューやブランチ保護は追加しない。自動マージは機能として有効にするが、
AIやワークフローが勝手にPRをマージする処理は今回追加しない。

## クライアント設定

- Claude Code: プロジェクトを信頼した場合、ファイル操作とBash実行を事前許可する。
- Codex CLI: 作業ディレクトリ内の書き込みとネットワークを許可し、個々のコマンドの承認要求を省く。
- 上位の管理設定、サービス側の安全判定、OAuth本人確認、利用枠はこの設定では変更されない。
- 他の利用者や他のリポジトリの設定を変更しない。

## APIキー

制御VMの既存 `github-api-sync` が所有リポジトリを検出し、次のキーをActions Secretsへ登録する。

- `GEMINI_API_KEY`
- `YOUTUBE_API_KEY`
- `ANTIGRAVITY_GEMINI_API_KEY`
- `DEEPSEEK_API_KEY`
- `FIRECRAWL_API_KEY`
- `OPENCODE_API_KEY`
- `OPENROUTER_API_KEY`
- `TEMPORAL_CLOUD_API_KEY`

新規リポジトリの検出はVM稼働中の15分間隔。値の更新元はVMの保護された保存先。
GitHub Secretsから値を読み戻すことはできない。新しいキーをリポジトリ側に追加しただけでは、
他のリポジトリへ自動輸入されない。

Secretsは必要なステップにだけ明示的に渡す。現時点のビルドworkflowはAPIキーを使用しない。
Claude・AntigravityのOAuth情報は `ai-model-workers` の非公開Secretsに置く。

## 手動ビルド

Actions → `Build package` → Run workflow。
開発用wheelだけを7日間保存し、動画生成・SNS投稿・PyPI公開・有料サービス購入は行わない。
これはパッケージ作成であり、切り抜きの動作確認やモデル呼び出しの確認ではない。
