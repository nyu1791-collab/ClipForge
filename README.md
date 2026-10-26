> 接続・制作・再接続は[最短手順](docs/QUICK_START.md)から開始してください。動画・音声・字幕・モデル応答は非公開で保存します。

通常制作・字幕/見出し/演出の修正・描画再開・回収は [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md) を参照。新規字幕の基準は [Gemini必読編集ルールブック](src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。 字幕・音・余白・解像度・参考画像の数値は [仕上げ目標](docs/FINISHING_TARGETS.md) として扱い、品質の停止条件にはしません。

# ClipForge

同じ配信から、話題別の**横動画（16:9）**を作るパイプラインです。コードと実行用Actionsはこの公開リポジトリで管理し、計算はActionsとColabで行います。動画・音声・文字起こしは非公開の保存庫へ転送します。

通常制作・修正・再開の入口は [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md) にまとめています。

## 流れ

| 段 | 処理 | 使うもの | 実行場所 |
| --- | --- | --- | --- |
| 1 | 元音声を取得、保存済みなら再利用 | yt-dlp・ffmpeg | Actions runner |
| 2 | 音声から話題と採用区間を決める | Gemini 3.8 Flash | Actions runner |
| 3 | 採用音声を切り出し、用語表記を調べる | ffmpeg・Gemini 3.5 Flash | Actions runner |
| 4 | 採用部分だけを文字起こし、話題内の発言時刻を記録 | Parakeet | Colab GPU |
| 5 | GPU文字起こしを話題ごと1回、要約せず整文 | Gemini 3.8 Flash | Actions runner |
| 6 | 最大360p素材を標準720pキャンバスへ拡大して字幕描画 | FFmpeg ASS・NVENC | Colab GPU |
| 7 | 完成動画と工程記録を回収 | 非公開Release | 制御側 |

本数・尺は既定5本、各5〜15分を目安にします。話題分けと字幕のルールは `src/clipforge/prompts/` からGemini入力へ読み込みます。字幕は要約せず言い淀みと明らかな誤字だけを整え、必要な効果音も同じ字幕応答に含めます。字幕時刻はGPU文字起こしの推定時刻を引き継ぎます。音声のない整文工程では話者や感情を推測しません。完了済み応答は同じ依頼文とルールで作られた場合だけ再利用します。監査・strict品質検査・追加の時刻照合・目標未達による再生成・全文ASRへの復帰は行いません。

## 使い方

接続済みなら以下の入口からそのまま開始できます。既存のGitHub接続からActionsを投入し、runnerのVertex OIDCとColab専用資格情報を使います。初回設定が必要な場合は [docs/COLAB.md](docs/COLAB.md) と [docs/VERTEX_GEMINI.md](docs/VERTEX_GEMINI.md) を参照します。

```bash
uv sync --locked --no-dev --inexact
uv run --no-sync python tools/produce_with_gemini.py \
  'https://kick.com/CHANNEL/videos/VOD_ID' \
  --output /private/NAME-production
```

コードSHAは専用実行ブランチから一度取得して固定・記録します。明示する場合は `--source-ref 40桁SHA` を使います。音声理解・字幕生成・GPU描画・非公開保存・回収までつなぎます。`--deliver-drive` を付けた場合だけ既存の専用資格情報で納品します。再開は [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md) の該当入口を使い、同じ出力先から新規投入を繰り返しません。

## 文書

- [最短手順](docs/QUICK_START.md)：初回接続・新規制作・保存状態からの再接続
- [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md)：字幕/見出し修正・描画再利用・回収/納品だけの操作
- [音声経路](docs/GEMINI_AUDIO_REVIEW.md)：話題選定と1話題1回の整文字幕
- [共通編集ルール](src/clipforge/prompts/gemini_editorial_rulebook_ja.md)：要約しない整文・話者・感情・SE
- [INDEX.md](INDEX.md)：コードと関連文書の地図

旧全編GPU ASRや候補プレビューの手順は、過去成果物を明示的に復旧する場合だけ参照します。通常制作の前提にしません。
