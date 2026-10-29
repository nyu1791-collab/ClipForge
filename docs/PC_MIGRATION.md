# Windows 11 パソコンへの引き継ぎ

この手順は、Windows 11 で Ubuntu の WSL2 を使う場合の入口です。コードの準備と制作の開始は別です。セットアップやリポジトリのコピーだけでは、モデル・Actions・Colab・描画・Drive 納品は実行しません。所要時間は回線や Windows の更新状況によって変わります。

## 1. Windows と Ubuntu の準備

Windows のセットアップとネット接続を済ませ、Windows Terminal または PowerShell を**管理者として**開きます。WSL2 の Ubuntu をまだ使っていなければ、次を実行します。

```powershell
wsl --install -d Ubuntu
```

表示された指示に従って再起動し、Ubuntu を開いて Linux 用のユーザー名とパスワードを作ります。すでに Ubuntu がある場合は再インストールせず、PowerShell の `wsl --list --verbose` でディストリビューションと WSL のバージョンを確認してください。WSL の設定・不具合は [Microsoft の WSL インストール案内](https://learn.microsoft.com/windows/wsl/install) を参照します。PC 本体の仮想化や再起動が必要な場合があります。

以下は Windows の PowerShell ではなく、**Ubuntu のターミナル**で入力します。Linux 側の `sudo` パスワード入力時は文字が表示されません。

## 2. コードをコピーして準備

2026-10-29 の引き継ぎ時点では、移行作業を含むブランチは `ccr-86066e66-7vd20b` です。`main` に統合されていない間は、このブランチを指定して取得します。統合後は `main` を使えますが、どの版を使うか必ず確認します。公開リポジトリの読み取りだけなら GitHub へのログインは不要です。

```bash
sudo apt-get update &&
sudo apt-get install -y git ca-certificates &&
mkdir -p "$HOME/projects/nyu1791-collab" &&
cd "$HOME/projects/nyu1791-collab" &&
git clone --branch ccr-86066e66-7vd20b https://github.com/nyu1791-collab/ClipForge.git &&
cd ClipForge &&
bash setup.sh &&
export PATH="$HOME/.local/bin:$PATH"
```

途中で失敗すると後続のコマンドは実行されません。すでに同名のフォルダがある場合も clone が停止するので、削除や上書きをせず、中身と `git status` を確認してから進めます。コードは WSL 側のホームに置くと扱いやすく、Ubuntu で `explorer.exe .` を実行すると Windows のエクスプローラーからフォルダを開けます。

`setup.sh` は不足している Ubuntu の Git、FFmpeg、GitHub CLI、日本語フォントなどを `apt` でインストールし、`uv` がない場合は公式のバージョン指定インストーラーから `~/.local/bin` に導入します。新規の仮想環境には Python 3.12 を選び、ロックされた依存をインストールして、最後にブランチと SHA を表示します。システムパッケージの導入には `sudo`、Python や依存の取得にはネット接続が必要です。既存の `.venv` がある場合、Python のバージョンを強制変更しませんが、依存関係は更新されることがあります。

`bash setup.sh --dev` は追加の開発依存・メディア取得依存を既存の `tools/dev_setup.sh` に従って導入します。`bash setup.sh --no-system` は OS パッケージと uv の自動導入を行わず、足りないものがあれば終了します。オプションは `bash setup.sh --help`。OS パッケージとツールのダウンロード・設定に失敗した場合は、そのエラーを確認し、異なるアカウントや接続に切り替えて回避しません。

最後の `export` は、このターミナルで新しく導入した `uv` を使えるようにします。新しいターミナルで `uv` が見つからない場合は、同じコマンドを実行します。`setup.sh` はシェルの設定ファイルを自動変更しません。

セットアップ終了後、次で現在の場所と変更を確認できます。

```bash
git status --short --branch
git rev-parse HEAD
```

ローカルのファイルが未コミットの場合は消さずに確認します。`git clone` でコピーされるのは Git で管理されているファイルと履歴です。`.venv`、認証情報、非公開動画、作業中の状態、未コミットファイルはコピーされません。

## 3. GitHub と制作データ

GitHub への push、Actions の監視、非公開保存庫へのアクセスが必要なときは、Ubuntu で `gh auth login` を実行し、利用する本人の GitHub アカウントでログインします。Secrets の値、OAuth 情報、API キーをチャットや Git に貼りません。ログインできても、非公開リポジトリへの権限や Colab の動作まで保証されません。制作で使う接続・登録先は [QUICK_START.md](QUICK_START.md) と [ANTIGRAVITY_ROUTE.md](ANTIGRAVITY_ROUTE.md) にあります。

公開 ClipForge はコードと Actions 定義です。動画・音声・字幕・モデル応答は、アクセス権のある非公開保存庫や指定先に別途保存されています。PC への clone やブランチの push に含まれず、PC へコピーしただけで各 GitHub Actions の Secrets やクラウドの認証設定が複製されるわけでもありません。

ClipForge 以外の複数リポジトリも移す場合は [複数リポジトリの引き継ぎ](PC_MULTIPLE_REPOSITORIES.md)。まずアクセスできる一覧を表示し、既存フォルダを上書きせずに取得できる。個別プロジェクトの依存導入・デプロイ・Secrets の登録は自動では行わない。

継続中の制作を引き継ぐ場合は、元の非公開作業ディレクトリごと、必要なファイルを安全な場所に保存してから PC に移してください。特に、確認待ちの制作では **同じフォルダにある元の `production-state.json` と `review-package.zip` の両方**が重要です。描画前にコードがサイズ・SHA-256・元データを照合するため、別版の確認 ZIP や編集用 ZIP に置き換えないでください。修正パッチ、完成動画、元の出力・依頼 ID も必要に応じて保持します。これらを公開リポジトリに追加しません。

クラウド環境の `/tmp` は永続保存先ではありません。そこにしかないデータは PC に自動では移りません。非公開 Release から元の成果物を取得できる場合もありますが、個々の Run・権限・ファイルの所在は確認が必要です。保存済みの状態を失った場合は新規投入で埋め合わせず、既存 Run と非公開 Release の記録を確認します。

## 4. 作業を引き継ぐ

AI エージェントには、このフォルダを作業対象として開き、最初に `AGENTS.md`、`INDEX.md`、必要な文書を読むよう伝えてください。規則と実体は更新されるため、指示だけでなく現在のブランチ・コード・接続を確認します。

画面でコードを見ながら作業するなら、Windows に [Visual Studio Code](https://code.visualstudio.com/) と WSL 拡張機能を導入し、Ubuntu の `ClipForge` フォルダで `code .` を実行します。初回は WSL 側の準備に時間がかかる場合があります。コマンドが見つからない場合は [VS Code の WSL 手順](https://code.visualstudio.com/docs/remote/wsl)を確認します。VS Code に接続しても、クラウドで動いていた AI が自動的に PC に移るわけではありません。使う AI エージェントを PC の環境で開き、必要に応じて本人がログインし、作業対象として WSL のフォルダを指定します。

通常制作は [HUMAN_REVIEW_WORKFLOW.md](HUMAN_REVIEW_WORKFLOW.md)、構成は [ARCHITECTURE.md](ARCHITECTURE.md)、開発依存や依頼された場合のテストは [DEVELOPMENT.md](DEVELOPMENT.md) を参照します。ユーザーが URL・本数・設定・コードの40桁 SHA を指定するまで Actions を投入せず、字幕を焼き付ける前に人の確認と明示的な承認を挟みます。

PC に編集ソフトを導入しても、ClipForge が出した字幕を読み込み・修正・再利用できるかは、使用するソフトと保存形式ごとに確認が必要です。完成版と同じ字幕表示、字幕の網羅性、編集データの往復は、セットアップだけでは保証されません。
