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
   curl -fsSL https://raw.githubusercontent.com/nyu1791-collab/ClipForge/b3898a6f3035817ace8bb0b1a04fb6d724c08789/tools/setup_vertex_actions.sh | bash
   ```

   Vertex APIの有効化、専用サービスアカウントと鍵なしGitHub OIDC接続を作る。サービスアカウントキー・APIキーは作らない。
3. 最後に表示されるプロジェクト番号を、private workerのActions変数`CLIPFORGE_VERTEX_PROJECT_NUMBER`へ登録する。番号は秘密情報ではない。

Google側で権限を与えるのは`nyu1791-collab/ai-model-workers`の`fix/pr18-verified-transfer`ブランチだけ。専用の権限はVertexの推論とそのプロジェクト利用に限定する。他のGoogleアカウントの資格情報は流用しない。mainの変更は行わない。

Cloud Shell:
https://console.cloud.google.com/home/dashboard?project=project-fdadb4dd-cb77-4270-9cd&cloudshell=true

GitHub変数の登録:
https://github.com/nyu1791-collab/ai-model-workers/settings/variables/actions/new

## ClipForge Actionsでの自動呼び出し

ASR保存済みの候補作成段階だけがVertexを呼ぶ。35前後の5〜15分の話題とASR本文を送り、指定本数のtopic ID・見出し・理由をJSONで得る。時刻は既存topic IDの値を使い、モデルが架空の時刻を作る方式にはしない。5本以上・各5分以上の設定を維持する。

1制作依頼で生成HTTPリクエストは1回。トークン上限4096。失敗・応答不明時は記録して停止し、別モデル・別課金経路へのfallbackや再生成はしない。候補作成を同じディレクトリで繰り返しても、完了記録は再利用し、不明状態では再送しない。モデル提供・権限のエラーもそのまま停止する。

資格情報はGitHub OIDCからの短期アクセストークンをそのステップだけに渡す。Vertexの資格情報をColab GPUやYouTube用コンテナへ送らない。ASR・描画・字幕に別途モデルを追加する処理ではない。品質監査や承認待ちは戻さない。

## OpenCodeハーネス

OpenCode CLIは`opencode-ai@1.19.4`。Vertexモデル名は`google-vertex/gemini-3.8-flash`。providerとsmall_modelも同じモデルに固定し、別モデルを使わせない。

```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/temporary-oidc-credentials.json \
python tools/opencode_harness.py --mode read --model vertex --task /path/to/task.md
```

ActionsでOpenCodeを実行する場合は、Google認証Actionの`create_credentials_file: true`が出す一時ファイルを使う。固定の秘密鍵を配布しない。モデルSDKが資格情報を読むためのパスとして使い、内容をモデルやログへ出さない。

Vertexハーネスは1タスク・同時1起動・起動再試行なし。ローカルのOpenCodeサーバーのイベントへ先に接続し、通信再試行の通知が来たらサーバーとクライアントを停止する。監視接続の再接続もしない。自動タイトル生成と自動コンテキスト要約は無効にする。コード編集やツール操作に伴うエージェントの複数ターンは、候補選定の1回のAPI呼び出しとは別に料金が発生し得る。OpenCodeからテスト・監査・追加生成を勝手に起動させない。

private workerの既存登録済みworkflowパス`clipforge-colab-status.yml`を、検証ブランチ上だけOpenCodeタスク用に置き換える。新規workflowをmainへ入れずに手動起動できるよう、ファイル名のみ既存のものを使う。旧ステータス監査は復活しない。

```bash
gh workflow run clipforge-colab-status.yml --repo nyu1791-collab/ai-model-workers \
  --ref fix/pr18-verified-transfer -f source_ref=FULL_SOURCE_SHA \
  -f mode=read -f task='実行したい具体的な作業をここに書く'
```

このチャット自体のモデルを変更する設定ではない。通常動画制作にOpenCodeコーディングタスクを勝手に追加しない。

## 現在の達成状態

コードと初回接続スクリプトは用意済み。Google側の初回連携・変数登録後に使用する。実API生成の成功や、当該モデルのVertex提供・クレジット適用は未確認。スクリーンショットの残高を無料保証とは扱わない。モデルを別名へ自動置換しない。
