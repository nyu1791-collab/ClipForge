# 接続・制作・再接続の最短手順

通常操作は `tools/produce_with_gemini.py` に集約する。監査・strict品質検査・候補映像の承認待ちは挟まない。

## 接続済みの場合

開発用依存やASRモデルは制御側へ入れず、最初の1回だけ基本パッケージを準備する。

```sh
uv sync --locked --no-dev --inexact
uv run --no-sync python tools/produce_with_gemini.py "$SOURCE_URL" --output "$PRIVATE_OUTPUT"
```

必要なら `--clips 3`（選定目安）、`--render-profile preview`（360p文字キャンバス）、`--deliver-drive`（描画後に指定Driveへ1回納品）を付ける。素材取得は最大360p。省略したコードSHAは実行ブランチから一度だけ解決して保存する。`--clips`はActionsから選定処理まで渡す。

## 再接続

```sh
uv run --no-sync python tools/produce_with_gemini.py --resume --output "$PRIVATE_OUTPUT"
```

URL・コードSHA・Run ID・納品指定は `production-state.json` から復元する。Run IDの記録前に切断していても、保存した依頼IDと完全一致するRunだけを探す。新しい制作/モデル要求/重複納品は投入しない。元の依頼でDrive納品まで指定していた場合は、描画回収後に未投入の納品だけを開始し、投入済みの納品は同じRunを監視する。失敗確定のRunは自動再試行しない。

従来の `URL --resume-existing-run RUN --output DIR` も使える。完成動画を回収済みならダウンロードを繰り返さない。状態ファイルは非公開権限の一時ファイルから置き換えて保存する。

## 初回接続

接続の登録名と設定先だけを一度で表示できる。

```sh
uv run --no-sync python tools/produce_with_gemini.py --connections
```

これは一覧の読み取りだけ。資格情報の値、モデル呼び出し、GPU確保、納品は扱わない。一覧権限がない場合は`unknown`と表示し、未登録とは断定しない。通常制作にこの一覧確認を必須化しない。

| 接続 | 設定先・役割 |
|---|---|
| GitHub | `gh`の既存ログインを使う。公開実行先はClipForge、成果物は非公開ai-model-workers |
| `CLIPFORGE_PAT` | ClipForgeのActions Secret。非公開保存庫のContents読み書き |
| `CLIPFORGE_VERTEX_PROJECT_NUMBER` | Actions変数。Google Cloud側の初回設定は指定所有者のCloud Shellで `bash tools/setup_vertex_actions.sh`。OIDC許可先はmain/専用実行ブランチ |
| `COLAB_ADC_JSON` | 専用Googleユーザーの既存ADCを `python tools/configure_colab_oauth.py` で登録。新規同意が必要なときだけ同ツールの`--login` |
| `GOOGLE_DRIVE_ADC_JSON` | Drive納品を指定するときだけ必要。[Drive設定](TARGET_DRIVE.md) |
| `YOUTUBE_COOKIES_B64` | 認証が必要なYouTubeの音声・採用映像取得に使用 |

Secretsの値をチャットへ貼らない。通常制作では必要な接続の未登録を音声取得前に案内し、GPU用認証なしで字幕モデルまで進める手戻りを避ける。登録の存在確認は実際の認証成功やGPU割当を保証しない。

## 工程と戻る場所

| 工程 | 処理 | 再開・修正の範囲 |
|---|---|---|
| 取得 | YouTube/Twitch/Kick/直接HTTPSの元音声を取得・保存済みなら再利用 | 保存音声のRun/request対を指定可能 |
| 話題 | 3.8が音声から採用話題と見出しを選定 | 全文ASRは作らない |
| 文字起こし | Colab GPUのParakeetで採用話題だけを認識 | 全配信を文字起こししない。完了結果は非公開で再利用 |
| 整文 | GPUのテキストと時刻を話題ごとに3.8へ1回送り、要約せずに整える | 60秒ごとの要求・境界結合を省略。音声は整文モデルへ送らない |
| 描画 | 非公開編集JSONからColab GPU/NVENCで描画 | `--render-only`。`--reuse-render`で一致する完成クリップを再利用 |
| 保存・回収 | 非公開Releaseのresult.zipとtransfer.json | `--resume`。任意JPEGの不足で再制作しない |
| 納品 | 指定時だけ許可済みDriveへ完成本数分を転送 | 1本以上の実在する完成動画が対象。本数目標で拒否しない。失敗時もMP4を保持 |

字幕は要約せず、無意味な言い淀みと明らかな誤字だけを整え、数字・否定・条件・口調・発言順を保持する。時刻はGPU文字起こしから引き継ぐ推定値であり、長い無音をまたいで文を結合しない。音声根拠がないため話者・感情は推測しない。

採用話題だけGPUで文字起こしし、そのテキストを3.8で整文する。GPUは文字起こし終了後に解放し、描画時に再度確保する。全編GPU文字起こしへ自動で戻さない。

修正パッチ・前回描画の再利用・回収だけ/納品だけの操作は [PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)。字幕本文の共通定義は [編集ルールブック](../src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。
