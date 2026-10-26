> 最新の制作経路と再開方法は[音声経路の手順](docs/GEMINI_AUDIO_REVIEW.md)を参照してください。動画・音声・字幕・モデル応答は非公開で保存します。

通常制作・字幕/見出し/演出の修正・描画再開・回収は [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md) を参照。新規字幕の基準は [Gemini必読編集ルールブック](src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。

# ClipForge

同じ配信から、話題別の**横動画（16:9）**を作るパイプラインです。コードと実行用Actionsはこの公開リポジトリで管理し、計算はActionsとColabで行います。動画・音声・文字起こしは非公開の保存庫へ転送します。

通常経路の設定・工程は [GPU_ROUTE.md](docs/GPU_ROUTE.md) を正本とする。

## 流れ

| 段 | 処理 | 使うもの | 実行場所 |
| --- | --- | --- | --- |
| 1 | 元音声を取得、保存済みなら再利用 | yt-dlp・ffmpeg | Actions runner |
| 2 | 音声から話題と採用区間を決める | Gemini 3.8 Flash | Actions runner |
| 3 | 採用音声を切り出し、用語表記を調べる | ffmpeg・Gemini 3.5 Flash | Actions runner |
| 4 | 採用区間だけを60秒単位で字幕化し、本文と区間時刻を結合 | Gemini 3.8 Flash | Actions runner |
| 5 | 選択字幕を焼き込み、360pで描画 | FFmpeg ASS・NVENC | Colab GPU |
| 6 | 完成動画と工程記録を回収 | 非公開Release | 制御側 |

既定5本、各5〜15分。話題分けと字幕のルールは `src/clipforge/prompts/` に置き、実際のGemini入力へ読み込みます。字幕の要約・言い換え・文字数圧縮はせず、無意味な言い淀みだけを除きます。字幕時刻はGemini推定です。完了済み応答は同じルールで作られた場合だけ再利用し、失敗時の自動再送や全文ASRへの復帰は行いません。

## 使い方

手順の全体は [docs/GPU_ROUTE.md](docs/GPU_ROUTE.md) です。最初の Google 認証は [docs/COLAB.md](docs/COLAB.md)。

```bash
uv sync --locked --extra media-fetch
uv run --no-sync python tools/produce_with_gemini.py \
  'https://kick.com/CHANNEL/videos/VOD_ID' \
  --source-ref PRODUCTION_COMMIT_SHA \
  --workflow-ref fix/pr18-verified-transfer \
  --output /private/NAME-production
```

`PRODUCTION_COMMIT_SHA`は新方式を含む40桁SHA。音声を理解して字幕を生成し、GPU描画・回収まで待ちます。`--deliver-drive`を付けた場合は既存の専用資格情報で納品します。過去の失敗を再開する際は[保存済み応答の再利用手順](docs/GEMINI_AUDIO_REVIEW.md)を先に確認してください。

## 文書

- [INDEX.md](INDEX.md)：コードと文書の地図
- [docs/GPU_ROUTE.md](docs/GPU_ROUTE.md)：運用手順（投入から納品まで）
- [docs/ASR.md](docs/ASR.md)：文字起こし（4台への分割と Colab 側の処理）
- [docs/AUDIO_FIRST.md](docs/AUDIO_FIRST.md)：段階の詳細（取得・下書き・承認ファイル・描画の設定）
- [docs/OPERATIONS.md](docs/OPERATIONS.md)：回収・再開・後始末・権限
- [docs/QUALITY_DECISIONS.md](docs/QUALITY_DECISIONS.md)：字幕と品質の決定事項
- [docs/YOUTUBE_TRANSPORT.md](docs/YOUTUBE_TRANSPORT.md)：YouTube の取得経路（WARP）

URL解決ツール・Gemini選定・GPU描画・回収まで自動でつなぐ入口は`tools/produce_with_gemini.py`です。初回認証と使い方は[Vertex設定](docs/VERTEX_GEMINI.md#urlからの自動制作)を参照してください。
