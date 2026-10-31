# 今回の確認範囲

2026-10-31、Linuxクラウド環境で実施。Windows PCは未到着のため実機確認ではない。

- 共通の8項目を公開9リポジトリの独立したcheckoutで実行: 合計66成功・6スキップ。スキップは任意のresearch.pyを採用しなかった6リポジトリの収集テスト。確認対象は元ファイル保持、SRT時刻維持、相対パス素材パック、MOVの形式偽装防止、Windows/Unixパス移行、ID/本文/時刻の保持、Postiz下書き、初期値での非実行、資格情報を出力しないdoctor、Crawl4AIアダプターの模擬応答。
- ClipForge-Deliveryで、素材パックを別の場所へ移動してから既存のread_manifest/import_manyへ取り込む確認: 1成功。メディアはコピー確認用の合成バイト列で、動画の再生品質を検証したものではない。
- discord-digital-deliveryの既存 `npm run check`: 構文チェックと7件のテストが成功。Workerへデプロイ・メッセージ送信・決済処理はしていない。
- 追加workstation Pythonのruffとgit diff --checkを確認。他の既存アプリ全体のテストを通したという主張はしない。
- 監視サービス: 公式changedetection.io 0.60.10をdigest検証付きで取得。1CPU・768MB・1ワーカー・60秒・AI無効のコンテナを起動し、空の確認用datastoreでHTTP 200とアプリ画面を確認。外部の実監視ターゲット0件・モデル要求0件。確認用コンテナは停止・削除した。Compose定義は127.0.0.1だけに公開し、構文を確認した。

モデル推論・CapCut Proの実機読込・Crawl4AIの実Chromium・browser-useの実推論・SNS投稿・Discord通知・商品の監視/購入・GPU処理・制作データの公開移管は未実施。Crawl4AI/browser-useは固定した上流ソースのAPIと模擬応答で接続処理を確認し、実パッケージはPC用の任意環境へ導入する設計。

GitHubの公開ページとGit checkoutで8つの外部候補の機能・LICENSEを確認した。GitHub APIホストはクラウドのネットワークで拒否され、Desktop Commanderのツールもこのセッションにはない。公開プロフィールで見えた9リポジトリを対象にし、非公開全件列挙は未確認。

以前の公開保存庫移管の未コミット変更はgit stashと `/workspace/private/pre-pc-integration-2026-10-31` の控えに保持した。この作業で適用・削除・公開はしていない。

追加確認: GitHubへpushした専用ブランチから、PC用workspaceコマンドでshorts-onlyとClipForge-Deliveryを新しい一時フォルダへ取得し、各HEADをworkspace-lock.jsonへ記録できた。Deliveryでは作成した新しい仮想環境へrequirements.txtを導入し、独立データフォルダでWaitressを起動してHTTP 200とアプリ画面を確認。確認用サーバーは停止済み。これもLinuxでの確認であり、Windows/CapCutの実機確認ではない。
