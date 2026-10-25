# Vertex Gemini 3.8 Flash と OpenCode

ユーザー指定の接続:

- Google所有者: `n.yu1791@gmail.com`（Colab用`n.yu17919191@gmail.com`とは別）
- プロジェクト: `project-fdadb4dd-cb77-4270-9cd`
- モデル: `gemini-3.8-flash` 固定
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

ユーザーの追加指示により、候補計画ではURL解決ツールの呼び出しと編集計画の生成を2回のHTTPリクエストで行う。前者の出力上限は1024、後者は8192トークン。各リクエストは1回のみで再試行しない。失敗・応答不明時は記録して停止し、別モデル・別課金経路へのfallbackや再生成はしない。候補作成を同じディレクトリで繰り返しても、完了記録は再利用し、不明状態では再送しない。モデル提供・権限のエラーもそのまま停止する。

資格情報はGitHub OIDCからの短期アクセストークンをそのステップだけに渡す。Vertexの資格情報をColab GPUやYouTube用コンテナへ送らない。ASR・描画・字幕に別途モデルを追加する処理ではない。品質監査や承認待ちは戻さない。

## OpenCodeハーネス

OpenCode CLIは`opencode-ai@1.19.4`。Vertexモデル名は`google-vertex/gemini-3.8-flash`。providerとsmall_modelも同じモデルに固定し、別モデルを使わせない。

```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/temporary-oidc-credentials.json \
python tools/opencode_harness.py --mode read --model vertex --task /path/to/task.md
```

ActionsでOpenCodeを実行する場合は、Google認証Actionの`create_credentials_file: true`が出す一時ファイルを使う。固定の秘密鍵を配布しない。モデルSDKが資格情報を読むためのパスとして使い、内容をモデルやログへ出さない。

Vertexハーネスは1タスク・同時1起動・起動再試行なし。ローカルのOpenCodeサーバーのイベントへ先に接続し、通信再試行の通知が来たらサーバーとクライアントを停止する。監視接続の再接続もしない。自動タイトル生成と自動コンテキスト要約は無効にする。コード編集やツール操作に伴うエージェントの複数ターンは、候補選定の1回のAPI呼び出しとは別に料金が発生し得る。OpenCodeからテスト・監査・追加生成を勝手に起動させない。

OpenCodeの実行定義はClipForgeの`clipforge-opencode.yml`へ統合する。PR #19でdefault branchへの登録は完了。実行にはGoogle/Secretsの移行設定が必要。移行状態は[PUBLIC_ACTIONS.md](PUBLIC_ACTIONS.md)を参照。

```bash
gh workflow run clipforge-opencode.yml --repo nyu1791-collab/ClipForge \
  --ref fix/pr18-verified-transfer -f source_ref=FULL_SOURCE_SHA \
  -f mode=read -f task='実行したい具体的な作業をここに書く'
```

このチャット自体のモデルを変更する設定ではない。通常動画制作にOpenCodeコーディングタスクを勝手に追加しない。

## 現在の達成状態

コードと初回接続スクリプトは用意済み。Google側の初回連携・変数登録後に使用する。実API生成の成功や、当該モデルのVertex提供・クレジット適用は未確認。スクリーンショットの残高を無料保証とは扱わない。モデルを別名へ自動置換しない。

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
