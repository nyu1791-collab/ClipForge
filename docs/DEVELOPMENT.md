# 開発と検証

## 入口

[AGENTS.md](../AGENTS.md) が共通ルール、[INDEX.md](../INDEX.md) が実装の地図。通常の開発に外部LLMや実配信の再生成は不要。使っていないモデル起動ハーネスは削除した。

## 変更に合わせて選ぶ

| 変更 | 必要な確認 | 通常は不要な処理 |
| --- | --- | --- |
| 文書・ルール | 差分、参照先、現行コードとの一致 | pytest全体、ASR、動画生成 |
| Python・設定 | 変更箇所のテスト、Ruff、呼び出し元の確認 | 無関係な全extra、同じテストの反復 |
| 共通処理・複数の入口 | 関連テスト、その後CIの全単体テストを1回 | 編集前後の全テストを毎回繰り返すこと |
| 字幕・FFmpeg・ASR | 関連単体テスト、必要なら合成動画・CPU ASRを明示実行 | 実配信全工程、Colab GPUの自動起動 |
| worker配備 | 固定SHA、workflow原本との一致、依頼・回収契約 | 実動画の再生成だけで配備確認を代用すること |

既存不具合との切り分けに必要な場合だけ、変更前の同じテストを比較する。失敗時は該当ケースへ絞る。新しいテストは、実際の不具合・境界条件・共通化した契約を確認するものを追加する。

## ローカルの低負荷な検査

Linux、Python 3.11以上。通常は `uv sync --locked`。チャット取得を扱う場合は `--extra chat`、追加URL取得を扱う場合は `--extra media-fetch` を付ける。GPU用extraを制御VMへ一律に導入しない。

```bash
uv run --no-sync python -m pytest -q -p no:cacheprovider tests/test_models_storage.py tests/test_review.py
uv run --no-sync ruff check src tools tests
uv run --no-sync ruff format --check PATH_TO_CHANGED_FILE
```

全単体テストは `uv run --no-sync python -m pytest -q -p no:cacheprovider -m 'not integration and not asr'`。`python -m pytest` を使い、toolsのimport経路をそろえる。字幕検査にはFFmpeg・fontconfig・日本語フォントが必要。依存やフォント不足はアプリの不具合と区別して報告する。

## CI

`Quality` はPython 3.11/3.12でRuff・単体テスト・カバレッジ90%以上を各1回確認する。手動実行も同じ単体テストを使う。10回反復と、単体ジョブ内での動画・ASRテストの重複実行は行わない。

動画・CPU ASRの合成素材検査は、アプリコード・動画fixture・このworkflowの変更時に自動実行する。文書や開発ツールだけの変更では省略する。差分の基点が浅い履歴にない場合は、安全側で実行する。手動実行では `video_checks=true` を明示する。モデルのダウンロードと実際のCPU処理が発生するが、Colabや外部LLMは起動しない。

検査結果のArtifact保存は報告用。保存枠の上限で添付できない場合も検査本体の成功・失敗を保持し、保存失敗はActionsに警告として残す。

依存スキャンは報告用で、検出0件を保証しない。`Build package` は必要なときに手動実行し、構文とwheelを確認する。動画生成や公開は行わない。

## 変更後の見直し

使われていない入口、設定の二重定義、壊れた文書リンク、生成物や認証情報の混入を差分で確認する。既存の依頼ID・SHA検証・承認・再利用の条件を整理のために外さない。結果がそろったら追加の比較実験を始めず、変更と未確認事項を報告する。

## 継続して扱う依存の課題

2026-10-23の既存スキャン記録には `curl-cffi 0.13.0` のHIGH指摘がある。今回の構成整理では依存ロックや取得方式を変更していない。影響範囲と修正版を確認し、通常依存とYouTube専用requirementsを同じ更新で扱う。過去の調査をアーカイブへ移しても、解決済みとはしない。
