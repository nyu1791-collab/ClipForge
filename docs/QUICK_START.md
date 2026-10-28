# 接続・制作・再接続の最短手順

通常操作は `tools/produce_with_gemini.py` に集約する。監査・strict品質検査・候補映像の承認待ちは挟まない。

制作の正規経路は公開ClipForgeのGitHub Actionsです。Gemini 3.8 Flash は Antigravity SDK（Vertex）経由、3.5 Transcribe は Vertex 直接で呼びます。起動入力と確認方法は [ANTIGRAVITY_ROUTE.md](ANTIGRAVITY_ROUTE.md) を参照してください。Actionsが素材取得とGemini処理を行い、採用話題の文字起こしはGemini 3.5 Transcribeに送り、描画はColab GPUへ依頼して非公開保存します。ローカル/VMへ制作を移さず、移行時は既存Runを再接続し、完了済み成果物を再利用してください。二重投入はしません。

## 接続済みの場合

開発用依存やASRモデルは制御側へ入れず、最初の1回だけ基本パッケージを準備する。

```sh
uv sync --locked --no-dev --inexact
uv run --no-sync python tools/produce_with_gemini.py "$SOURCE_URL" --output "$PRIVATE_OUTPUT"
```

通常は360p以下の素材を優先し、利用できなければ取得可能な映像を使い、640×360で描画する。必要なら `--clips 3`（選定目安）、`--render-profile standard`（720pキャンバスを明示選択）、`--deliver-drive`（描画後に指定Driveへ1回納品）を付ける。省略したコードSHAは実行ブランチから一度だけ解決して保存する。`--clips`はActionsから選定処理まで渡す。

Kick素材のGETは429・5xx・通信障害だけを10秒・30秒・60秒の間隔で最大3回まで再試行する。制作workflowの投入は自動再試行しない。Geminiの一時的な通信障害・429・5xxは一度の実行で同一要求につき合計3回まで試す。明示的に再開すると未完了要求を再び最大3回試す。認証拒否・無効な要求は再試行しない。応答が不明なまま再送すると重複課金の可能性がある。取得済みの成果物は保持し、失敗を記録する。KickメタデータのAPIが404の場合だけ、同じ動画の公開ページから情報を取得する。認証拒否・レート制限・通信障害・メタデータの矛盾では、ページ取得へ切り替えない。

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
| 話題 | 3.8が音声から採用話題と見出しを選定（録画長で1・2・4体に分担） | 全文ASRは作らない |
| 文字起こし | 採用区間を3.5 TranscribeのVERBATIM・単語時刻・話者分離で認識 | 898秒（約15分）以内の窓に分けて結果を保存。全配信は文字起こししない |
| 清書 | 採用区間の音声とTranscribeのテキスト・時刻・話者ラベルを話題ごとに3.8 Flash/MEDIUMへ1回送り、誤字脱字と明らかな誤認識だけを直す（話者判定・効果音なし） | 60秒ごとの要求・境界結合を省略。音声をモデルへ送る |
| 描画 | 非公開編集JSONからColab GPU/NVENCで描画 | `--render-only`。`--reuse-render`で一致する完成クリップを再利用 |
| 保存・回収 | 非公開Releaseのresult.zipとtransfer.json | `--resume`。任意JPEGの不足で再制作しない |
| 納品 | 指定時だけ許可済みDriveへ完成本数分を転送 | 1本以上の実在する完成動画が対象。本数目標で拒否しない。失敗時もMP4を保持 |

字幕は要約せず、明らかな誤字と誤認識だけを直し、言い淀み・相槌・反復は残す。数字・否定・条件・口調・発言順を保持する。時刻はTranscribeから引き継ぐ推定値であり、長い無音をまたいで文を結合しない。話者はTranscribeのラベルをクリップ内の出現順に speaker_1〜speaker_6 へ対応させ、ラベルがない箇所は話者不明とする。感情は通常表示（neutral）とする。話者IDはクリップ内での匿名IDであり、クリップ間の同一性は保証しない。

採用話題ごとにTranscribeと3.8 Flash清書を順次行い、最大6話題を並行する。6話題なら通常はTranscribe 6件、清書6件で、終わった話題から描画に進む。空白補助（追加のTranscribe要求）は行わない。898秒を超える話題はTranscribeを重なり付きで分割するため要求数が増える。話者IDは分割ごとに独立し、ラベルがなければunknownとする。ASR用GPUは使わず、描画にはGPUを使う。時刻・話者ラベル・清書の精度や速度は実素材で保証しない。

話題選定は録画長で1体（1時間以下）・2体（2時間以下）・4体に分け、各体は前後600秒を重ねた音声を聞く。新規の要求は体の数だけ並行し、保存済み応答からの再開は1件ずつ送る。一時的な通信障害・429・5xxに限り各要求を合計3回まで試す。全体把握の音声は12 kbpsのOgg/Opus（音声向けの低負荷設定）、採用区間の清書用音声は24 kbpsで作る。清書は文字起こし済みの話題から開始し、全話題が揃うまで待たない。

準備済みクリップの非公開編集情報と入力マーカーはGPU待機前に保存し、その後1本ずつ空いたGPU枠へ送る。同じ制作内のGPU枠は最大5（`gpu_slots.SLOT_COUNT`）。render-only のRunは1つのColab runtimeを確保し、Run内では選んだクリップを順に描画する。複数本を同時に描画する場合は clip_index 指定の別Runを並べ、merge Runで結合する（`tools/dispatch_parallel_render.py`）。音声窓の準備時刻、GPU枠の待機・占有時刻、各クリップのFFmpegプロセス開始・終了時刻を記録する。枠の占有時間はGPUの実際の使用率そのものではなく、別ランタイム間の時刻には時計のずれがあり得る。

描画エラーのスタック情報やプロセスのstderr末尾は、取得できた場合だけURL・認証情報を伏せて非公開結果Releaseの `diagnostics/` に保存する。公開Actionsログには段階とエラー種別のみを表示する。起動前の失敗や結果の保存自体が失敗した場合は詳細が残らないことがある。保存済みクリップや親Releaseを変更せず、失敗を理由に自動再描画しない。

修正パッチ・前回描画の再利用・回収だけ/納品だけの操作は [PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)。字幕本文の共通定義は [編集ルールブック](../src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。

## Pro特典のクレジットを使う請求先

通常経路のGemini 3.8 Flashと3.5 Transcribeはすべて `project-fdadb4dd-cb77-4270-9cd` のVertex AIへ送る。同じ請求先をTranslationとVOICEBOXも使用する。APIでは特定のプロモーションクレジットを選べず、適用対象・優先順位・残額はCloud Billing側で決まる。残額を使い切った後の自動停止は設定していない。
