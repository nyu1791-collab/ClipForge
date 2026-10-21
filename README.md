# ClipForge

AIで見どころを選び、動画を切り抜いて字幕を付けるCPU向けパイプライン。

**現在は開発基盤を作成した段階です。動画の切り抜き機能はまだ実装されていません。**

## 目指す処理

1. 利用権限のある動画ファイルを入力する。
2. 音声認識で発話と時刻情報を取得する。
3. Geminiなどで候補場面を選び、開始・終了時刻と選定理由を作る。
4. FFmpegで場面ごとに切り抜き、縦横比と音量を調整する。
5. 字幕を編集・描画し、MP4とSRT/ASS、場面一覧を出力する。

## 開発環境

- Python 3.11以上、FFmpeg / FFprobe。CPUのみ。
- 編集・コミット・PR作成はAIエージェントが進める。
- 重い処理はGitHub Actionsに渡し、制御VMは依頼・認証管理・結果回収を担当する。
- 手動の `Build package` ワークフローを用意。現在は開発用wheelの作成だけを行う。
- 機能実装はGitHub Issuesの `AI開発タスク` から依頼を整理できる。

```bash
python -m pip install -e .
```

現在のパッケージは基盤のみで、実行用CLIはまだありません。

## AI・API接続

Claude向け設定は `.claude/settings.json`、Codex CLI向け設定は `.codex/config.toml`。
各クライアントがこのプロジェクトを信頼した後に適用する。クラウドサービス側の権限・安全判定・割当は別管理。

共有APIキーは制御VMからActions Secretsへ同期する。値をソース、Issue、プロンプト、成果物へ書かない。
キーの登録と、アプリの実際の呼び出し実装・推論成功は別の状態として記録する。
Claude・AntigravityのOAuth資格情報は、この公開リポジトリには複製せず非公開の
[`ai-model-workers`](https://github.com/nyu1791-collab/ai-model-workers)で扱う。

詳細は [構成](docs/ARCHITECTURE.md) と [運用](docs/OPERATIONS.md) を参照。
