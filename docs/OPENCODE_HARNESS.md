# OpenCodeでClipForgeを開発するハーネス

OpenCode CLIのプロジェクト設定・作業コマンドと、実行前後の自動検証を組み合わせています。
追加の外部ハーネス製品やプラグインは導入していません。

## セットアップ

Linux、Python 3.11/3.12、uv、FFmpeg、fontconfig、Noto CJKが必要です。
OpenCode CLIは1.19.3で設定読込を確認しています。

```sh
uv sync --locked --group dev
python tools/opencode_harness.py --check --cli /path/to/opencode
python tools/opencode_harness.py --prepare --parent /workspace
```

`--check`は必要ファイル・CLIバージョン・停止モデルの拒否・既定モデル未指定を点検します。
`--prepare`は公開GitのHEADと明示したハーネス関連ファイルを、新しい独立Gitディレクトリへ取り出します。
元の未コミットのアプリ変更、私有workspace、認証情報、動画、ASR、.venvはコピーしません。
ハーネス関連の設定・スクリプト・回帰テスト・依存manifest/lockは既知の一覧に限定して重ねます。
準備コピーでも `uv sync --locked --group dev` を行ってから使います。

## OpenCodeからの使い方

| コマンド | 作業 |
| --- | --- |
| `/clipforge-review 問題の説明` | 調査→再現→最小修正→共通ハーネス→差分確認 |
| `/clipforge-fix 変更要求` | 指定変更→回帰検査→共通ハーネス→差分確認 |
| `/clipforge-verify` | 共通ハーネスによるオフライン検査 |

新規セッションを起動するwrapperは以下の形式です。利用者がモデルと課金実行を明示した場合だけ使います。

```sh
python tools/opencode_harness.py --start \
  --project /workspace/clipforge-opencode-PREPARED_COPY \
  --model PROVIDER/USER_SELECTED_MODEL \
  --command clipforge-fix --task '実装する変更'
```

wrapperは **事前ハーネス→OpenCode→事後ハーネス** を順に実行します。
事前検査が失敗すればOpenCodeを起動せず、事後検査が失敗すれば終了状態を失敗にします。
モデル未指定と `gpt-6.1-sol` は拒否します。自動fallback・再試行・前セッション再開はありません。
既定のmodel・small_modelも設定していません。
cwd・PWD・`--dir`・`OPENCODE_CONFIG`を準備コピーへ揃え、`--pure`で外部プラグインを無効にします。

## 検証だけを直接使う

```sh
uv run --offline python harness/check_pipeline.py
```

OpenCodeの3コマンドとGitHub Actionsがこの同じ入口を使います。

- Ruffのlint・format、全unittest回帰検査。
- OpenCode設定・権限・停止モデルの拒否と、公開JSONスキーマ/runtimeモデルの一致。
- アプリ全体のbranch coverageを表示。**95%の必須閾値は `harness/` の2スクリプトが対象**です。
- 未ステージ・ステージ済み差分の空白エラー検査。

アプリ全体の95%達成を意味しません。既存アプリのカバレッジはレポートの実測値を参照してください。
既存テストの音声・動画・ASRはmockまたは合成データです。実配信やモデルを取得しません。

ローカルの字幕を明示して検品する場合:

```sh
uv run --offline python harness/lint_captions.py workspace/output/CLIP
```

ASS/SRTの本文・時刻一致、非空・最大2行・4秒・非重複、警告数（既定0）、欠落glyph、review件数を検査します。
字幕本文はログに表示しません。これはMP4の品質検査とは別です。実動画の全デコード等は従来の非公開workerで実施します。

## GitHubへの登録

`.github/workflows/opencode_harness.yml` が全PR・mainへのpush・手動実行で起動し、Python 3.11/3.12で共通入口を実行します。
既存の `tests.yml` を置き換えたため、同じオフライン検査workflowを二重登録しません。
ジョブはcontents:read、checkout認証の保持なし、タイムアウト10分です。実動画や発話を公開Artifactsへ保存しません。
CIはOpenCode CLI自体を起動せず、APIキー・OAuth・GitHub Secretsを必要としません。
失敗時はCIが失敗します。マージの強制ブロックはGitHubのbranch protection/rulesetでrequired checkに設定する別の機能です。
この変更では既存のブランチ保護設定を変更しません。

OpenCode CLIのモデル接続に必要な認証は、ローカルの利用者アカウントで別途扱います。
CLIログイン済みであることからAPIキーを抽出したり、認証DBをGitHubへコピーしたりはしません。
公開リポジトリへモデル実行用のキーは登録していません。

## 確認済みの範囲と制限

2026-10-22: OpenCode 1.19.3の設定読込、primary agent、3コマンド、準備コピーの設定参照、既定モデル未指定をモデルなしで確認。
事前検査失敗でモデルへ進まないこと、事前/事後検査、起動引数・cwd/PWD/設定パスはプロセス起動をmockしたテストで確認しています。
実モデルによる編集ループと、実作業中のディレクトリ境界は未検証です。
設定のexternal_directory拒否はOSサンドボックスではなく、Python検査は任意コードを実行できます。
解決済み設定には認証関連情報が含まれ得るため、丸ごと公開ログへ出しません。

前環境の未コミット差分・認証・完成動画・ASRキャッシュは本環境へ復元していません。
元作業ツリーのアプリ変更は保持し、このハーネス登録には含めません。
