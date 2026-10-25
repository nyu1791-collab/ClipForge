# ClipForge

同じ配信から、話題別の**横動画（16:9）**を作るパイプラインです。動画・音声・文字起こしは非公開の worker（`nyu1791-collab/ai-model-workers`）の GitHub Actions と Colab で扱い、このリポジトリにはコードだけを置きます。

通常経路の設定・検査範囲は [GPU_ROUTE.md](docs/GPU_ROUTE.md) を正本とする。

## 流れ

| 段 | 処理 | 使うもの | 実行場所 |
| --- | --- | --- | --- |
| 1 | 配信の音声を取得する（Kick・YouTube・Twitch・直接 HTTPS） | yt-dlp・ffmpeg（YouTube は任意で WARP） | Actions runner |
| 2 | 全編を最大4台で文字起こしする | NVIDIA Parakeet（`nvidia/parakeet-tdt_ctc-0.6b-ja`） | Colab GPU（L4・T4） |
| 3 | 話題に分け、候補の低画質映像と未承認の下書きを作る | 規則（間と語彙の変化）・ffmpeg | Actions runner |
| 4 | 候補を確認し、承認ファイルを作る | 人（または Claude） | 手元 |
| 5 | 採用した区間だけ取得し、字幕を焼き込んで描画する | ffmpeg・BudouX・fugashi | Colab GPU/NVENC |
| 6 | クリップを検証してまとめる | — | 制御VM |

文字起こしは1回だけで、その結果を話題分けと字幕の両方に使います。外部の生成AIは呼びません。

## 使い方

手順の全体は [docs/GPU_ROUTE.md](docs/GPU_ROUTE.md) です。最初の Google 認証は [docs/COLAB.md](docs/COLAB.md)。

```bash
uv sync --locked --extra media-fetch --extra chat
uv run --no-sync python tools/submit_job.py \
  --audio-analysis-url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --ref REVIEWED_COMMIT_SHA --config config.landscape-long.example.json \
  --auto-previews --wait --output /private/NAME-previews
```

**あなたが権利を持つ配信・ファイルの URL** を指定してください。MP3・M4A など音声だけの入力は `--auto-previews` を外し、解析までにします。

## 文書

- [INDEX.md](INDEX.md)：コードと文書の地図
- [docs/GPU_ROUTE.md](docs/GPU_ROUTE.md)：運用手順（投入から納品まで）
- [docs/ASR.md](docs/ASR.md)：文字起こし（4台への分割と Colab 側の処理）
- [docs/AUDIO_FIRST.md](docs/AUDIO_FIRST.md)：段階の詳細（取得・下書き・承認ファイル・描画の設定）
- [docs/OPERATIONS.md](docs/OPERATIONS.md)：回収・再開・後始末・権限
- [docs/QUALITY_DECISIONS.md](docs/QUALITY_DECISIONS.md)：字幕と品質の決定事項
- [docs/YOUTUBE_TRANSPORT.md](docs/YOUTUBE_TRANSPORT.md)：YouTube の取得経路（WARP）
