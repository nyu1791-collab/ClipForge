# Vertex Gemini 3.8 Flash と OpenCode

ユーザー指定の接続:

- Google所有者: `n.yu1791@gmail.com`（Colab用`n.yu17919191@gmail.com`とは別）
- プロジェクト: `project-fdadb4dd-cb77-4270-9cd`
- 指示役: `gemini-3.8-flash` / MEDIUM
- 軽作業役: `gemini-3.5-flash` / MINIMAL、独立した2体を並列呼び出し
- Vertex AI / global。AI Studio無料枠やOpenCode Go/Zenへ自動切替しない。

## 初回のGoogle連携

この環境に指定Googleアカウントの認証は接続されていないため、本人がCloud Shellで一度設定する。

1. 指定アカウントでCloud Shellを開く。
2. 次のコマンドを実行する。

   ```bash
   curl -fsSL https://raw.githubusercontent.com/nyu1791-collab/ClipForge/fix/pr18-verified-transfer/tools/setup_vertex_actions.sh | bash
   ```

   Vertex APIの有効化、専用サービスアカウントと鍵なしGitHub OIDC接続を作る。サービスアカウントキー・APIキーは作らない。
3. 最後に表示されるプロジェクト番号を、ClipForgeのActions変数`CLIPFORGE_VERTEX_PROJECT_NUMBER`へ登録する。番号は秘密情報ではない。

Google側で権限を与えるのは`nyu1791-collab/ClipForge`の`main`と`fix/pr18-verified-transfer`ブランチだけ。専用の権限はVertexの推論とそのプロジェクト利用に限定する。他のGoogleアカウントの資格情報は流用しない。mainへの反映はユーザー承認済み。

Cloud Shell:
https://console.cloud.google.com/home/dashboard?project=project-fdadb4dd-cb77-4270-9cd&cloudshell=true

GitHub変数の登録:
https://github.com/nyu1791-collab/ClipForge/settings/variables/actions/new

## ClipForge Actionsでの自動呼び出し

候補作成段階でVertexを呼ぶ。35前後の5〜15分の話題とASR本文を送り、指定本数のtopic ID・見出し・理由をJSONで得る。時刻は既存topic IDの値を使い、モデルが架空の時刻を作る方式にはしない。5本以上・各5分以上の設定を維持する。

ユーザーの追加指示により、候補計画は3.8の指示生成、3.5の軽作業2件（並列）、3.8の編集判断の4リクエストで行う。3.8はMEDIUM、3.5はMINIMALを明示する。全文ASRを渡すのは3.8の編集判断だけ。各リクエストは1回のみで再試行しない。失敗・応答不明時は記録して停止し、別モデル・別課金経路へのfallbackや再生成はしない。候補作成を同じディレクトリで繰り返しても、完了記録は再利用し、不明状態では再送しない。モデル提供・権限のエラーもそのまま停止する。

資格情報はGitHub OIDCからの短期アクセストークンをそのステップだけに渡す。Vertexの資格情報をColab GPUやYouTube用コンテナへ送らない。ASR・描画・字幕に別途モデルを追加する処理ではない。品質監査や承認待ちは戻さない。

## OpenCodeハーネス

OpenCode CLIは`opencode-ai@1.19.4`。Vertexモデル名は`google-vertex/gemini-3.8-flash`。providerとsmall_modelも同じモデルに固定し、別モデルを使わせない。

```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/temporary-oidc-credentials.json \
python tools/opencode_harness.py --task /path/to/task.md
```

ActionsでOpenCodeを実行する場合は、Google認証Actionの`create_credentials_file: true`が出す一時ファイルを使う。固定の秘密鍵を配布しない。モデルSDKが資格情報を読むためのパスとして使い、内容をモデルやログへ出さない。

Vertexハーネスは1タスク・同時1起動・起動再試行なし。ローカルのOpenCodeサーバーのイベントへ先に接続し、通信再試行の通知が来たらサーバーとクライアントを停止する。監視接続の再接続もしない。自動タイトル生成と自動コンテキスト要約は無効にする。コード編集やツール操作に伴うエージェントの複数ターンは、候補選定の1回のAPI呼び出しとは別に料金が発生し得る。OpenCodeからテスト・監査・追加生成を勝手に起動させない。

OpenCodeの実行定義はClipForgeの`clipforge-opencode.yml`へ統合する。PR #19でdefault branchへの登録は完了。実行にはGoogle/Secretsの移行設定が必要。移行状態は[PUBLIC_ACTIONS.md](PUBLIC_ACTIONS.md)を参照。

```bash
gh workflow run clipforge-opencode.yml --repo nyu1791-collab/ClipForge \
  --ref fix/pr18-verified-transfer -f source_ref=FULL_SOURCE_SHA \
  -f task='実行したい具体的な作業をここに書く'
```

このチャット自体のモデルを変更する設定ではない。通常動画制作にOpenCodeコーディングタスクを勝手に追加しない。

## 現在の達成状態

非公開Actionsでは3.8と3.5の実推論、3.8によるYouTube動画の直接解析まで成功済み。公開ClipForge側はActions定義をmainへ統合済みだが、公開側認証の移行は未完了。適用単価の公示額と実際のクレジット充当額を区別し、請求画面を確認せず実支払額を保証しない。モデルを別名へ自動置換しない。

## Geminiへの編集指示

1回の生成で、全体のテーマ分析、長い切り抜き区間の採用、見出し、内容要約、冒頭の見せ方、必要な前提、導入と結末の扱い、字幕の改行・用語・強調・誤認識候補をまとめて依頼する。提案は非公開のvertex-selection-receipt.jsonのeditorial_planに保存する。採用IDと見出しは制作JSONへ反映する。字幕補助は提案として保存し、元のASR字幕を自動で書き換えたとは扱わない。映像入力のない解析で映像確認済みと主張しない。

## URLからの自動制作

Geminiは`resolve_source_url`ツールへ対象URLを渡す。ツールは指定URLを正規化し、取得済みメタデータ・ASRとの対応を返す。別配信や署名付き素材URLをモデルに探させる方式ではない。取得済み素材の二重ダウンロードはしない。モデルの選定IDを元時刻へ対応付け、fetch_planとautomatic_selectionを小さな転送記録へ含める。実際の素材ダウンロード、字幕合成、FFmpeg/NVENCはworkerが行う。

```bash
uv run python tools/produce_with_gemini.py 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --source-ref FULL_SOURCE_SHA --output /private/new-production \
  --analysis-run SAVED_ASR_RUN --analysis-request SAVED_ASR_REQUEST
```

保存済みASRがない新しい配信はanalysisの2引数を省略する。YouTube・Twitch・直接HTTPSも同じ入口を使う。既定は5本・各5〜15分。別の本数はその本数を指定したコミット済みconfigを`--config-file`で選ぶ。選定から描画へ自動で進み、最終成果物を回収する。失敗・不明時は状態とRun IDを保存して停止し、再投入しない。同じ出力先の再起動も二重投入を拒否する。

通常の操作先は公開ClipForge。公開側への認証移行が済んでいない間は、既存の非公開実行経路を`--repository nyu1791-collab/ai-model-workers`で明示できる。実行場所の自動切替はしない。

## 2026-10-25の実行記録

- URLツール＋編集計画: private Run 40218907431。Gemini 3.8 Flashの2リクエスト成功、合計28.216秒。URL、保存済みASRの対応、5本のautomatic_selection、素材取得区間のfetch_planを返した。
- 描画: private Run 40218308069。先行Run 40217494566でGeminiが選定した5本をL4/NVENCで描画成功。Run全体549秒、描画バッチ437.54秒。新しいURLツール確認のために同じ5本を再描画していない。
- ASRは再利用。品質監査・承認待ち・自動再試行は実行していない。GPU解放ステップは成功。
- 公開ClipForgeの認証移行は引き続き未完了。今回は既存の認証済み非公開実行経路を明示して実行した。

## 3体の分担

| 役割 | モデル | 思考レベル | 担当 |
|---|---|---|---|
| 指示・編集判断 | Gemini 3.8 Flash | MEDIUM（標準） | 軽作業2件の指示、全体分析、区間選定、見出し・字幕補助 |
| 作業役1 | Gemini 3.5 Flash | MINIMAL | 指示に従いURL解決ツールを呼ぶ |
| 作業役2 | Gemini 3.5 Flash | MINIMAL | 字幕改行・短い見出しの整形ルールをまとめる |

3.5の2体は独立したAPI呼び出しとして並列実行する。3.8は返された結果を使って編集計画を仕上げる。モデルごと・役割ごとの時間、トークン、要求モデル名、実応答モデル名、指定思考レベルを記録する。失敗時に別モデルへ切り替えたり再送したりしない。

従来の動画APIはthinkingConfig未指定で、公式の3.8既定値MEDIUMに依存していた。今回から明示設定に変更した。公式仕様: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking

### 3体構成の実呼び出し

Run 40220449164で成功。3.8/MEDIUMの指示生成3.220秒、3.5/MINIMALのURL作業1.260秒、3.5/MINIMALの整形作業1.626秒（2体並列）、3.8/MEDIUMの編集判断24.843秒。計画全体29.695秒。要求・応答モデルはそれぞれgemini-3.8-flash / gemini-3.5-flashと一致し、5本を選定した。保存済みASRのみ使用し、GPU・再描画・自動再試行は実行していない。

## 個人Driveへの自動納品

ユーザー指定の宛先はn.yu1791@gmail.com。--deliver-driveで描画・回収後に自動転送する。Gemini/Vertexの認証と個人Driveの認証は別で、モデルへOAuth情報を渡さない。初回の本人接続は[TARGET_DRIVE.md](TARGET_DRIVE.md)を参照。

## YouTube動画を3.8で直接解析

vertex_video.pyはYouTube URLをfileDataとしてGemini 3.8 Flashへ渡す。思考はMEDIUM、低解像度・0.2fpsの入力で、映像と音声から要約・元時刻付きの根拠・切り抜き候補・字幕補助を1回のリクエストで取得する。VM側のダウンロード・Parakeet・GPUは不要。解析結果はMP4ではなく編集計画として保存する。動画入力のトークン内訳とモデル自身のアクセス申告を記録し、未取得の動画をURLだけから推測した結果と区別する。

公式仕様: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/video-understanding

直接動画解析の実行例: Run 40222184955。3.8 Flash / MEDIUM、51.720秒、動画・音声入力あり、5区間の提案を取得。YouTube素材のVM取得がログイン要求で止まっていても、公開YouTube URLを正規の動画入力として解析できた。これはMP4の取得・書き出しの成功を意味しない。
