# 接続・制作・再接続の最短手順

通常操作は `tools/produce_with_gemini.py` に集約する。監査・strict品質検査・候補映像の承認待ちは挟まない。

## 接続済みの場合

開発用依存やASRモデルは制御側へ入れず、最初の1回だけ基本パッケージを準備する。

```sh
uv sync --locked --no-dev --inexact
uv run --no-sync python tools/produce_with_gemini.py "$SOURCE_URL" --output "$PRIVATE_OUTPUT"
```

通常は素材を最大360pで取得し、640×360で描画する。必要なら `--clips 3`（選定目安）、`--render-profile standard`（720pキャンバスを明示選択）、`--deliver-drive`（描画後に指定Driveへ1回納品）を付ける。省略したコードSHAは実行ブランチから一度だけ解決して保存する。`--clips`はActionsから選定処理まで渡す。

Kick素材の取得・モデル要求・制作workflowの投入は、失敗時も自動再試行しない。取得済みの成果物は保持し、失敗を記録する。KickメタデータのAPIが404の場合だけ、同じ動画の公開ページから情報を取得する。認証拒否・レート制限・通信障害・メタデータの矛盾では、ページ取得へ切り替えない。

## 再接続

```sh
uv run --no-sync python tools/produce_with_gemini.py --resume --output "$PRIVATE_OUTPUT"
```

URL・コードSHA・Run ID・納品指定は `production-state.json` から復元する。Run IDの記録前に切断していても、保存した依頼IDと完全一致するRunだけを探す。新しい制作/モデル要求/重複納品は投入しない。元の依頼でDrive納品まで指定していた場合は、描画回収後に未投入の納品だけを開始し、投入済みの納品は同じRunを監視する。失敗確定のRunは自動再試行しない。

状態確認がGitHubの401・403・429で拒否された場合、監視を一時停止してHTTP状態だけを保存する。同じ要求を繰り返したり、制作を再投入したりしない。一時的な読み取り障害による監視の再開でも、保存したRunと依頼IDの一致を確認する。

## 完了したクリップから回収

並列制作のログに `clip-001` などの保存完了が出たら、全体の終了前でも別端末からその1本を回収できる。

```sh
uv run --no-sync python tools/collect_production_clip.py --output "$PRIVATE_OUTPUT" --clip 1
```

保存した依頼IDとRun IDから非公開Releaseを参照し、素材・コード・クリップ番号を照合する。新しい制作やモデル要求は投入しない。取得先は `completed/clip-001` 以下。同じクリップを再指定した場合は保存済みファイルを再利用する。すでに1本以上を先に回収している場合、最終回収ではそれらを再ダウンロードせず、未回収分と保存記録から完成フォルダを構成する。

Release回収時は、展開と同時にファイル一覧・サイズと制作元が記録したハッシュを照合する。現行形式で動画のハッシュが記録されていない場合、動画全体のハッシュ照合は行わない。ZIP自体の破損は展開時に検出し、失敗したファイルを完成物として扱わない。

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
| 文字起こし | 小さな声を拾いやすく音量を調整した採用話題のコピーをColab GPUのParakeetで認識 | 完成映像の音量は変更しない。全配信を文字起こしせず、完了結果は入力が一致するとき非公開で再利用 |
| 整文 | 採用区間の音声とGPUのテキスト・時刻を話題ごとに3.5 Flash/MEDIUMへ1回送り、整文と匿名の話者判定を行う | 60秒ごとの要求・境界結合を省略。音声をモデルへ送る |
| 描画 | 非公開編集JSONからColab GPU/NVENCで描画 | `--render-only`。`--reuse-render`で一致する完成クリップを再利用 |
| 保存・回収 | 非公開Releaseのresult.zipとtransfer.json | `--resume`。任意JPEGの不足で再制作しない |
| 納品 | 指定時だけ許可済みDriveへ完成本数分を転送 | 1本以上の実在する完成動画が対象。本数目標で拒否しない。失敗時もMP4を保持 |

字幕は要約せず、無意味な言い淀みと明らかな誤字だけを整え、数字・否定・条件・口調・発言順を保持する。時刻はGPU文字起こしから引き継ぐ推定値であり、長い無音をまたいで文を結合しない。声を聞き分けられない箇所は話者不明とし、感情は通常表示とする。話者IDはクリップ内での匿名IDであり、クリップ間の同一性は保証しない。

採用話題だけGPUで文字起こしし、そのテキストと区間音声を3.5 Flash/MEDIUMで話題数に応じて並行整文する。今回の制作では文字起こしと描画で最大4 GPU枠を共有し、空いた枠へ待機中の文字起こしを順に投入する。1本の文字起こし用GPUを解放したら、その字幕の整文・描画へ進める。全編GPU文字起こしへ自動で戻さない。

話題選定の音声窓は、準備が終わったものから最大3件を並行して送る。窓の数やモデル要求の回数は増やさず、失敗・成否不明の要求は自動再送しない。音声準備は従来と同じ16 kHz・12 kbpsのOgg/Opusで、音声向けの低負荷設定を使用する。整文は文字起こし済みの話題から開始し、全話題が揃うまで待たない。

準備済みクリップの非公開編集情報と入力マーカーはGPU待機前に保存し、その後1本ずつ空いたGPU枠へ送る。ASRと描画の合計は最大4枠で、ASRの待ち行列を優先するため、4本が常に同時開始するわけではない。音声窓の準備時刻、GPU枠の待機・占有時刻、各クリップのFFmpegプロセス開始・終了時刻を記録する。枠の占有時間はGPUの実際の使用率そのものではなく、別ランタイム間の時刻には時計のずれがあり得る。

描画エラーのスタック情報やプロセスのstderr末尾は、取得できた場合だけURL・認証情報を伏せて非公開結果Releaseの `diagnostics/` に保存する。公開Actionsログには段階とエラー種別のみを表示する。起動前の失敗や結果の保存自体が失敗した場合は詳細が残らないことがある。保存済みクリップや親Releaseを変更せず、失敗を理由に自動再描画しない。

修正パッチ・前回描画の再利用・回収だけ/納品だけの操作は [PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)。字幕本文の共通定義は [編集ルールブック](../src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。
