# 開発環境の準備

新しい Claude Code / クラウドセッションでは、作業対象のブランチと最新 SHA を確認し、リポジトリ直下で実行する。

```bash
bash tools/dev_setup.sh
```

`uv` が必要。ロックファイルに従って開発・チャット・メディア取得依存を導入し、FFmpeg と日本語フォントを確認する。不足する OS パッケージは Debian / Ubuntu の root または非対話 sudo で導入する。管理権限がない場合は必要なパッケージを表示して終了する。再実行でき、秘密情報や認証設定は変更しない。制御 VM には CUDA を入れない。GPU 開発環境で必要な場合だけ `bash tools/dev_setup.sh --extra cuda` を使う。

検証は変更モジュールのテスト、全テスト、lint、変更した Python ファイルの形式確認の順に行う。

```bash
uv run --no-sync python -m pytest -q -p no:cacheprovider
uv run --no-sync ruff check .
uv run --no-sync ruff format --check path/to/changed.py
```

セットアップ成功と動画生成成功は別。URL ジョブ投入には `gh` と非公開 `nyu1791-collab/ai-model-workers` への認証が必要。実行前に `gh api repos/nyu1791-collab/ai-model-workers --jq '{private, permissions}'` で対象と権限を確認する。拒否された場合は別経路で回避せず、セッションの GitHub 接続に対象リポジトリの Contents / Actions の読み書きを設定する。認証情報の値は表示・保存しない。投入・回収は [OPERATIONS.md](OPERATIONS.md)、Colab は [COLAB.md](COLAB.md) に従う。
