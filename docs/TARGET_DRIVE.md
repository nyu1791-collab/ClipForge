# 指定された個人Driveへの納品

ユーザーはn.yu1791@gmail.comに加え、n.yu17919191@gmail.comの個人Driveも許可した。既存のDrive認証がこの2アカウントのいずれかに対応する場合だけ使う。Vertexサービスアカウントを個人Driveの代わりにしない。同じGoogleでも、Vertexの推論権限で個人Driveのファイル作成権限は取得できない。

通常の転送は専用のGOOGLE_DRIVE_ADC_JSONだけを使う。COLAB_ADC_JSONへのfallbackは廃止した。転送開始時にDriveのuser(emailAddress)で宛先本人を一度確認し、一致しなければフォルダも動画も作らず停止する。動画の再走査や読み戻し監査はしない。

## 初回接続（本人のCloud Shell）

n.yu1791@gmail.comで次を開く。

https://console.cloud.google.com/home/dashboard?project=project-fdadb4dd-cb77-4270-9cd&cloudshell=true

```bash
curl -fsSL https://raw.githubusercontent.com/nyu1791-collab/ClipForge/main/tools/setup_target_drive.sh -o /tmp/clipforge-drive-setup.sh
bash /tmp/clipforge-drive-setup.sh /path/in/cloudshell/desktop-oauth-client.json
```

先に[対象プロジェクトのOAuthクライアント画面](https://console.cloud.google.com/auth/clients?project=project-fdadb4dd-cb77-4270-9cd)で「デスクトップアプリ」のOAuthクライアントを作成し、JSONをCloud Shellに置く。チャットへ送らない。Googleの同意画面がテスト公開の場合はn.yu1791@gmail.comをテストユーザーに含める。Cloud SDK標準クライアントのままDriveスコープを追加する方法は使わない。

このスクリプトはDrive APIの有効化、本人のGoogle同意、GitHub所有者ログイン、専用Drive資格情報のSecrets登録を行う。Google・GitHubの同意画面は本人が操作する。資格情報や認可コードはチャットへ送らない。Drive権限はアプリが作成したファイルを扱うdrive.fileを使う。Drive用ADCにはCloudスコープを含めず、指定プロジェクトで作成した専用OAuthクライアントのquotaを使う。APIキーやサービスアカウント秘密鍵は作らない。既存のColab認証は変更しない。

## 制作から転送まで

```bash
uv run python tools/produce_with_gemini.py 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --source-ref FULL_SHA --output /private/production --deliver-drive
```

描画成功後だけDrive転送を1回実行する。アップロード失敗時も回収済みMP4を保持し、再描画・自動再転送はしない。実行場所は公開ClipForgeが既定。公開側の移行前は--repository nyu1791-collab/ai-model-workersを明示する。

## 2026-10-25のYouTube依頼

対象huUd9Qw015wのRun 40220968442は音声取得のメタデータ取得段階で、YouTubeからログイン要求（botでないことの確認）を返され停止した。GPU・ASR・Geminiは未起動、MP4は0本。再投入していないためDriveアップロードも未実施。既存の別アカウントへ誤って保存する経路は使わない。

Google公式: https://docs.cloud.google.com/docs/authentication/application-default-credentials

upload-analysisモードは3.8の動画解析結果をDriveに保存する。MP4転送とは別に記録し、解析JSONだけを動画納品成功と報告しない。

## 追加指示後の結果

ユーザーの追加指示に従い、Run 40222184955で3.8 FlashがYouTube URLを直接解析した。映像28,380トークン・音声53,711トークンを含む入力で、51.720秒・API1回で成功した。通常の素材取得は再投入していない。

ユーザーがn.yu17919191@gmail.comも許可した後、Run 40222710144でn.yu17919191@gmail.comへ解析レポート・解析JSON・使用量記録の3件を非公開で保存した。MP4は作成・転送していない。GoogleのDriveアップロードAPIの応答を保存し、全量読み戻しや品質監査は行っていない。
