> 接続・制作・再接続は[最短手順](docs/QUICK_START.md)から開始してください。動画・音声・字幕・モデル応答は非公開で保存します。

通常制作・字幕/見出し/演出の修正・描画再開・回収は [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md) を参照。新規字幕の基準は [共通編集ルール](src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。字幕・音・余白・解像度・参考画像の数値は [仕上げ目標](docs/FINISHING_TARGETS.md) として扱い、品質の停止条件にはしません。

# ClipForge

同じ配信から、話題別の**横動画（16:9）**を作るパイプラインです。コードと実行用 Actions はこの公開リポジトリで管理し、計算は Actions と Colab で行います。動画・音声・文字起こしは非公開の保存庫へ保存します。

## 流れ

| 段 | 処理 | 使うもの | 実行場所 |
| --- | --- | --- | --- |
| 1 | 元音声を取得（保存済みなら再利用） | yt-dlp・ffmpeg | Actions runner |
| 2 | 音声から話題・見出し・採用区間を決める（録画長で 1・2・4 体） | Gemini 3.8 Flash（Antigravity） | Actions runner |
| 3 | 採用部分を切り出し、話題ごとに文字起こしする | Gemini 3.5 Transcribe（Vertex 直接） | Actions runner |
| 4 | 文字起こしを話題ごと 1 回、誤字と明らかな誤認識だけ直す | Gemini 3.8 Flash / MEDIUM（Antigravity） | Actions runner |
| 5 | 360p の字幕付き横動画を描画する | FFmpeg・ASS・NVENC | Colab GPU |
| 6 | 完成動画と工程記録を回収 | 非公開 Release | 制御側 |

本数・尺は既定 5 本、各 5〜15 分を目安にします。話題選定と字幕のルールは `src/clipforge/prompts/` から Gemini の入力へ読み込みます。字幕は要約せず、言い淀み・相槌・反復は残し、明らかな誤字と誤認識だけを直します。話者は Transcribe のラベルを使い、感情は通常表示、効果音は付けません。時刻は Transcribe の発話時刻を引き継ぎます。保存済みの応答は、同じ依頼文・音声・ルールの場合だけ再利用します。品質監査・strict 検査・時刻の照合による再生成・全文 ASR への復帰は行いません。

## 使い方

接続済みなら以下の入口からそのまま開始できます。既存の GitHub 接続から Actions を投入し、runner の Vertex OIDC と Colab 専用資格情報を使います。初回設定が必要な場合は [docs/COLAB.md](docs/COLAB.md) と [docs/VERTEX_GEMINI.md](docs/VERTEX_GEMINI.md) を参照します。

```bash
uv sync --locked --no-dev --inexact
uv run --no-sync python tools/produce_with_gemini.py \
  'https://kick.com/CHANNEL/videos/VOD_ID' \
  --output /private/NAME-production
```

コード SHA は専用実行ブランチから一度取得して固定・記録します。明示する場合は `--source-ref 40桁SHA` を使います。音声理解・字幕生成・GPU 描画・非公開保存・回収までつなぎます。`--deliver-drive` を付けた場合だけ既存の専用資格情報で納品します。再開は [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md) の該当入口を使い、同じ出力先から新規投入を繰り返しません。

## 文書

- [最短手順](docs/QUICK_START.md)：初回接続・新規制作・保存状態からの再接続
- [統合経路ガイド](docs/PRODUCTION_AND_REVISION.md)：字幕・見出しの修正、描画の再利用、回収と納品だけの操作
- [音声経路](docs/GEMINI_AUDIO_REVIEW.md)：話題選定・文字起こし・清書の工程と記録
- [Antigravity 経路](docs/ANTIGRAVITY_ROUTE.md)：起動入力と確認
- [共通編集ルール](src/clipforge/prompts/gemini_editorial_rulebook_ja.md)：字幕表現・話者・感情・効果音の規則（[適用と記録](docs/GEMINI_EDITORIAL_RULEBOOK.md)）
- [INDEX.md](INDEX.md)：コードと関連文書の地図

旧の全編 GPU ASR（Parakeet）と候補プレビューの手順は、過去成果物を明示的に復旧する場合だけ参照します。通常制作の前提にしません。
