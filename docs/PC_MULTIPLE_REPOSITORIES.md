# 複数のリポジトリを Windows PC に引き継ぐ

新しい PC に、GitHub にある複数のリポジトリをそれぞれ独立したフォルダとして取得する手順。まず [PC_MIGRATION.md](PC_MIGRATION.md) で Windows 11・WSL2・Ubuntu と ClipForge の基本環境を準備する。

## 何がコピーされるか

GitHub で指定したアカウント・組織に対し、ログイン中の本人が閲覧できるリポジトリを一覧する。必要な場合だけ `--clone` で Git のコードと履歴を取得する。既存のフォルダ、未コミット変更、作業ブランチは上書きしない。GitHub 上のブランチ情報は取得するが、実際に開くのは各リポジトリの既定ブランチ。未統合のブランチを自動で選択・マージしない。

**Git の clone に入らないもの**: GitHub Actions の Secrets や Variables、Release・Artifact に別保存された成果物、Issue・PR の本文やコメント、Wiki、リポジトリの設定、Google/Cloudflare 等の接続、ローカルの認証情報・`.env`・未コミット変更、ローカル DB、Git に含まれない素材・制作状態。既定では Git LFS の実ファイルとサブモジュールも取得しない。PC のブラウザーに保存されていたデータは別端末に自動で移らない。GitHub や各サービス側のデータは、clone しなくても元の場所に残る。これらをコピーしたり新しく登録したりする前に、所在・権限・利用者の指示を確認する。

この手順はリポジトリの読み取りと、指定された PC 上への Git clone のみ。依存の一括インストール、GPU の確保、モデル要求、Actions の手動実行、デプロイ・公開、Drive の転送は行わない。

## 1. GitHub にログイン

Ubuntu のターミナルで実行する。GitHub 側では、対象リポジトリにアクセスできる本人のアカウントを使う。ログイン時に、ブラウザーでの認証と Git の HTTPS 設定を選べる。

```bash
gh auth login --hostname github.com
gh auth setup-git --hostname github.com
```

`gh auth setup-git` は GitHub CLI を HTTPS 用の Git 認証ヘルパーとして設定する。認証トークンを URL やコマンドに直接入れない。設定・権限・接続が不明なときは `gh auth status --hostname github.com` で確認し、トークンや認証情報をチャットに貼らない。組織のリポジトリが見えるかは、その本人の権限や GitHub 側の設定に依存する。

## 2. 一覧を確認してから取得

ClipForge フォルダで次を実行する。2026-10-30（日本時間）に確認した所有者・組織を指定した例。PC でログインしたときに見える一覧は、この時点の接続で見えた一覧と異なる場合がある。

```bash
cd "$HOME/projects/nyu1791-collab/ClipForge"
uv run --no-sync python tools/prepare_repositories.py \
  --owner nyu1791-collab --owner line-rangers-fan \
  --root "$HOME/projects"
```

この実行は GitHub API の読み取りと一覧表示だけで、リポジトリは取得しない。対象の所有者を追加・変更する場合は `--owner` を指定する。存在しない、または権限不足で見えないリポジトリは一覧に出ないため、「一覧にない＝存在しない」とは断定しない。指定した所有者のものが1件も見えないときは警告する。表示には非公開リポジトリの名前も含まれ得るため、公開するログや画面へ貼らない。

一覧と空き容量を確認し、取得する場合だけ `--clone` を付ける。

```bash
uv run --no-sync python tools/prepare_repositories.py \
  --owner nyu1791-collab --owner line-rangers-fan \
  --root "$HOME/projects" --clone
```

保存先は `$HOME/projects/所有者/リポジトリ名`。Git の HTTPS 認証を使用し、取得時に認証情報を URL に含めない。GitHub が返すサイズは目安であり、実際の使用容量・Git の履歴・後から必要になるデータの容量を保証しない。大きなリポジトリは時間や容量を使う。途中で失敗した場合は自動で再試行・削除せず、残ったフォルダとログ、権限や空き容量を確認する。

既存の取得先は中身を調べず**変更せずにスキップ**する。別のリポジトリや空フォルダがその場所にある場合も、自動では入れ替えない。再実行したときも同じで、最新化が必要なら各フォルダの `git status`、リモート、ブランチを確認する。破損・未完了のフォルダも勝手に消さない。

明示的に Git LFS の実ファイルも取得する場合は、Git LFS をインストールして容量・権限を確認したうえで `--clone --with-lfs` を使用する。取得後に **チェックアウト中の既定ブランチ**の LFS オブジェクトを取得する。ほかのブランチの LFS データはそのブランチを開くときに別途確認する。既存フォルダはこの指定でもスキップする。サブモジュールは自動初期化しない。設定に含まれる取得先を確認してから、必要なものだけ各リポジトリの指示に従う。

## 3. 各プロジェクトを開く

各フォルダで `git status --short --branch`、`git log -1 --oneline`、`git branch -r` を確認する。未統合 PR・作業中ブランチがある場合は、目的と最新 SHA を確認してから開く。ブランチ名だけで完成版と決めず、変更・マージ・本番反映を自動で行わない。ClipForge の作業ブランチは [PC_MIGRATION.md](PC_MIGRATION.md) を参照する。

次は確認時点の公開リポジトリの入口。各リンク先は既定ブランチを表示するため、実際に使う作業ブランチと最新の手順は別途確認する。

| リポジトリ | 用途と PC で最初に確認するもの |
| --- | --- |
| [ClipForge](https://github.com/nyu1791-collab/ClipForge) | 切り抜き制作。`AGENTS.md`、`INDEX.md`、この作業ブランチの `setup.sh` と [PC_MIGRATION.md](PC_MIGRATION.md) を読む。確認・承認前に描画しない |
| [voiceBox-Pipeline](https://github.com/nyu1791-collab/voiceBox-Pipeline) | VOICEVOX を使った動画制作。`AGENTS.md` と README の「VM・Linuxでも実行」。エンジン本体・正規素材・フォントは clone だけでは揃わない。PC 上の動作や各音声の利用条件は別途確認 |
| [StreamCut](https://github.com/nyu1791-collab/StreamCut) | 別の切り抜きパイプライン。README では Python 3.11〜3.13、FFmpeg、フォントを要求し、GPU 推論は CUDA 必須。ClipForge の経路に勝手に混ぜない |
| [Translation](https://github.com/nyu1791-collab/Translation) | 動画字幕の翻訳。README では Python 3.11 以上、FFmpeg、フォント、Vertex AI を要求。クラウドの接続は ClipForge から自動で引き継げるとは限らない |
| [MoneyPrinterTurbo-Foundation](https://github.com/nyu1791-collab/MoneyPrinterTurbo-Foundation) | 動画生成の派生版。README と `docs/FOUNDATION_DESIGN.md`、使用するブランチ、素材と容量を確認する。取得後に API や生成を自動実行しない |
| [discord-digital-delivery](https://github.com/nyu1791-collab/discord-digital-delivery) | 販売用の配布 Worker。README と Cloudflare の既存設定を確認。R2 のファイルや運用中の接続は Git clone されない |
| [line-rangers-pvp](https://github.com/line-rangers-fan/line-rangers-pvp) | PvP の集計とサイト。README と `OPERATIONS.md` を確認。公開データ、Pages、Cloudflare の Worker・D1/R2 は Git のコードだけとは別 |
| [line-rangers-guildwar](https://github.com/line-rangers-fan/line-rangers-guildwar) | ギルド戦のサイト。README はローカル版に Node.js 24 以降を指定。ローカル SQLite、ブラウザー内の記録、公開版の D1 はそれぞれ別 |
| 非公開のワーカー・制作物・バックアップ | 各作業規則、アクセス権、保存範囲を確認。Secrets、Release、Git 管理外の制作データは Git clone されない |

GPU が必要なソフトは、PC に対応 GPU とドライバーがあるかを個別に確認する。例えば CUDA が必須の処理は、NVIDIA GPU のない PC で動くとは限らない。クラウドの既存経路を使う場合も、対象・設定・課金とアクセス権を確認し、勝手に新規投入しない。

AI に作業を任せるときは、各リポジトリの `AGENTS.md` や README、存在する場合は `CLAUDE.md` を読み、対象フォルダとブランチを明示する。プロジェクトごとにルール・用途・依存が異なるため、一つのセットアップやルールを全リポジトリへコピーしない。

## 確認した範囲

2026-10-30（日本時間）の作業環境では、指定した2つの所有者・組織について、接続中のアカウントから合計11件を一覧できた。各リポジトリの入口となる README、存在するルートの `AGENTS.md`、ルートのファイル構成を確認した。公開・非公開のリポジトリ各1件については、別の一時フォルダへの Git clone も確認した。全リポジトリの全コードや全ブランチの動作を確認したわけではない。

取得ツールは、既存フォルダを変更しないこと、一覧のページ送り、認証失敗や LFS 取得失敗の扱いなどの個別テスト14件が成功し、変更した Python ファイルの lint と形式確認も通った。購入した PC での Windows・WSL・GitHub ログイン、全リポジトリの取得、LFS の実データ取得、各制作環境の動作は未確認。
