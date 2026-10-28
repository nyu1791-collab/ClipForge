# 初回接続と実行

## Google Cloud / GitHub Actions

GitHub ActionsからGoogle Vertexへの接続はOIDCを使用し、APIキーやサービスアカウント鍵をリポジトリに置きません。このリポジトリ専用のWIFプール・サービスアカウントを作るため、既存ClipForgeの設定は変更しません。

1. 使用するGoogle Cloudプロジェクトの所有者としてCloud Shellを開く。
2. このリポジトリを取得し、`GOOGLE_CLOUD_PROJECT` と必要なら `TRANSLATION_GITHUB_REPO` を指定して `bash scripts/setup_vertex_wif.sh` を実行する。既定のリポジトリは `nyu1791-collab/Translation`、既定プロジェクトはユーザーの既存Vertexプロジェクト。
3. スクリプトは `translation-actions` WIFプールと `translation-vertex` サービスアカウントを作り、Translationの `main` だけを許可する。認証済みの `gh` があればGitHub Actionsの変数を登録し、それ以外は値を表示する。
4. Repository Settings → Secrets and variables → Actions で、非公開の保存先リポジトリを読み書きできる `TRANSLATION_STORAGE_TOKEN` を登録する。既存の別リポジトリのSecret値はGitHubから読み戻せないため、手動登録または組織Secretの付与が必要。YouTube認証が必要な素材に限り `YOUTUBE_COOKIES_B64` も登録する。
5. Actions → Translation video → Run workflow から、公開動画URLと必要なオプションを入力する。`translation-result-RUN_ID` のReleaseが非公開保存先に作成される。

必要なActions変数：`TRANSLATION_PROJECT_ID`、`TRANSLATION_WIF_PROVIDER`、`TRANSLATION_SERVICE_ACCOUNT`、`TRANSLATION_STORAGE_REPO`。GitHub Actionsのトークンは保存先が非公開か確認したうえでReleaseを作成する。動画・文字起こし・翻訳・字幕は公開リポジトリのActions artifactへは保存しない。

## 注意点と再開

- WIFの条件はRepositoryの名前と `refs/heads/main` に限定している。ブランチから手動実行する場合は許可されない。
- Actionsの実行時間、GPU、音声認識精度、翻訳や同期は保証されない。Actionsの既定描画はCPU。
- ローカルではGoogle CloudのApplication Default Credentialsか、短命の `TRANSLATION_VERTEX_ACCESS_TOKEN` を使用する。トークンやCookieを引数・ログ・Gitに出さない。
- 手元の `--output` を保管できる場合、同じコマンドでキャッシュを使って再開できる。GitHub-hosted Actionsランナーの一時ディレクトリは実行終了時に破棄されるため、失敗時に別Runで途中キャッシュを自動復旧する機能は未実装。
- 対象動画を利用する権限やプラットフォームの取得条件は利用者が確認する。
