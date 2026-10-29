# 開発環境の準備

Windows 11 PC の初回導入、WSL2・GitHub・非公開制作データの引き継ぎは [PC_MIGRATION.md](PC_MIGRATION.md)。通常の基本依存はリポジトリ直下の `bash setup.sh` で準備できます。

開発用依存も必要な Claude Code / Codex / クラウドセッションでは、作業対象のブランチと最新 SHA を確認し、リポジトリ直下で実行する。

```bash
bash tools/dev_setup.sh
```

`uv` が必要。ロックファイルに従って開発・チャット・メディア取得依存を導入し、Python 3.12 以上なら Colab 制御 SDK も入れる（入れないと `tests/test_colab_control.py` がまるごと skip される）。FFmpeg と日本語フォントも確かめる。不足する OS パッケージは Debian / Ubuntu の root または非対話 sudo で導入する。管理権限がない場合は必要なパッケージを表示して終了する。再実行でき、秘密情報や認証設定は変更しない。旧ASR（NeMo・Parakeet）は Colab だけで動き、手元には入れない。現行の文字起こしは Gemini 3.5 Transcribe（Vertex直接）で、ローカルのASRは使わない。

検証（テスト・lint）は利用者が依頼したときだけ行う（AGENTS.md）。依頼された場合は変更モジュールのテスト、全テスト、lint、変更した Python ファイルの形式確認の順に行う。

```bash
uv run --no-sync python -m pytest -q -p no:cacheprovider
uv run --no-sync ruff check .
uv run --no-sync ruff format --check path/to/changed.py
```

セットアップ成功と動画生成成功は別。制作の投入には GitHub の認証（公開 ClipForge の Actions と非公開保存庫 `nyu1791-collab/ai-model-workers`）が必要。実行前に `bash tools/check_github_api.sh` で接続と読み取りを確認する。拒否された場合は別経路で回避せず、セッションの GitHub 接続に対象リポジトリの Contents / Actions の読み書きを設定する。認証情報の値は表示・保存しない。投入・回収は [OPERATIONS.md](OPERATIONS.md)、Colab は [COLAB.md](COLAB.md) に従う。
