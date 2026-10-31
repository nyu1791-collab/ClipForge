# 新しいPCでの導入・移行

2026-10-31。Core i7 / メモリ16GB / 有線LANを想定する。GPU・Windowsの版・空きディスク・CapCutの版は未確認。Windowsネイティブの編集を基本にし、Dockerを使う場合だけDocker Desktop/WSL2を追加する。

## 最初に用意するもの

- Python 3.12、Git。uvを使うプロジェクトはuvも導入する。MoneyPrinterはuvが独立したPython 3.11環境を用意する。
- 動画処理にはFFmpeg/FFprobe（ASS字幕対応ビルド）、日本語フォント。CapCutはPCへ本人のProアカウントで導入する。Pro契約だけで外部投稿APIや非公式ドラフト形式の互換性が保証されるわけではない。
- ソースとデータを分ける。例: `C:\Automation\repos` と `C:\AutomationData`。各リポジトリはそれぞれの `.venv` を持ち、動画・DB・秘密をGitに入れない。Linuxの `.venv` をWindowsへコピーして使わない。

必要ならWindowsのwingetで `Python.Python.3.12`、`Git.Git`、`astral-sh.uv`、`Gyan.FFmpeg`、`OpenJS.NodeJS.LTS` を本人の端末へ導入する。Dockerは任意。今回ここからPCへインストールしたわけではない。

## このリポジトリだけ導入する

```powershell
py -3.12 workstation/manage.py doctor
# まず導入コマンドを見る。モデル/API/制作は起動しない。
py -3.12 workstation/manage.py setup
# 表示されたこのリポジトリの環境だけを実際に導入する
py -3.12 workstation/manage.py setup --apply
```

`workstation/run-windows.cmd` からも同じ引数を使える。`doctor` の資格情報欄は環境変数の存在だけで、認証・モデル提供・残高・制作成功の証明ではない。設定を作る場合は従来の `.env.example` や設定例からPC内の非公開ファイルを作る。

## 完成物を別PC・Deliveryへ渡す

```powershell
py -3.12 workstation/manage.py package C:/completed/clips C:/AutomationData/publish-kit-001 --mode tiktok
```

完成MP4/MOV/MKV/WebM、対応するSRT/ASS、既存カバーだけをコピーする。本文・字幕時刻・元ファイルを変更せず、動画の再エンコードやAI呼び出しはしない。新しい出力先だけを使う。投稿文は既存名または `--title` の内容であり、AIが新しく考えた文ではない。

字幕は動画と同名の `動画名.srt/.ass` を優先する。`captions.srt/.ass` や `subtitles.srt/.ass` は、そのフォルダの動画が1本で候補も1つの場合だけ使う。対応が曖昧な字幕は付けず、結果と `package.json` の `warnings` に動画名・候補名を記録する。必要なら元字幕を保ったまま動画と同名の複製を用意する。出力先はフォルダリンクや `..` を解決した実際のパスで判定し、元フォルダ内へは作らない。

`manifest.json` は相対パスなのでフォルダごと移せる。ClipForge-Deliveryの「素材の一括取り込み」でこのファイルを指定する。Deliveryが対応するMP4だけがその取り込み一覧に入り、MOV等は変換したふりをせずパック内にそのまま保持する。フォルダには `package.json` と「投稿用メモ.txt」も作る。内部のファイルを保ったままフォルダ全体を移す。

## CapCutや制作状態の素材パスを移行する

```powershell
py -3.12 workstation/manage.py relink C:/transfer/draft_content.json C:/transfer/draft_content-moved.json --old-root C:/old-media --new-root D:/AutomationData/media
```

JSON内の明示的なパス項目だけを書き直し、ID・時刻・字幕本文・他フォルダの参照は保持する。元JSONを上書きせず、素材ファイル自体は移動しない。CapCutは終了してバックアップした複製プロジェクトで使い、新PCの素材が揃ってから対応するJSONを置き換える。非公式形式のため、実際にCapCutで開けるかはそのPCの版で確認する。CapCutの設定画面にある実際のドラフトフォルダを使い、別製品の中国版Jianying形式をそのまま流用しない。

## 投稿先へは下書きとして受け渡す

```powershell
py -3.12 workstation/manage.py postiz-draft --integration-id MY_CHANNEL --content "投稿文" --media-id UPLOADED_MEDIA_ID --media-url https://example.test/media.mp4 --output C:/AutomationData/postiz-draft.json
```

Postizへアップロード済みのID/URLを指定する。ローカルの `C:/...mp4` は公開メディアURLにはならない。このコマンドはPostizのCreatePostDtoに対応する `type=draft` JSONを作るだけで、ネットワーク送信・予約・公開はしない。APIへ渡すときは、自分のPostiz接続と各SNSのアプリ/OAuth設定が別途必要。YouTube投稿枠・TikTokの公開権限・X等のAPI条件は別であり、CapCut Proでは代用できない。

## 引き継ぐもの

1. ソースはGitから取得し直す。既存作業は先に保存し、`workspace-lock.json` に記録されたSHAを参照する。
2. 動画・字幕・素材パック・アプリのDBとログ・再開用の状態ファイルを、PCの非公開データフォルダへコピーする。DBはアプリを終了してからコピーする。
3. 状態内の旧絶対パスがある場合は複製JSONへrelinkを使う。元媒体・完了済み動画・親版を保持する。
4. GitHub接続はPCで `gh auth login`、Googleは各アプリの説明に従って既存アカウントでADC等を用意する。ActionsのOIDCトークンはPCへ移せない。サービスアカウントの長期鍵を新規に発行したり、別アカウントへ自動切替したりしない。
5. 既存Actions Runがある場合はそのIDから監視・回収を再開する。PC移行だけを理由に制作や有料APIを再投入しない。

各リポジトリは独立して動作する。他のリポジトリのPython、DB、コードを必須にしていない。統合するのはファイル受け渡しとPC上の配置・導入方法である。

## ClipForge の選定

採用区間だけを処理する既存Gemini/Transcribe経路と再開方式を維持。新しく加えたのはPCの導入計画、投稿・納品用の相対パス素材パック、CapCut用JSONの素材パス移行、Postizの下書きJSON。

PCは制作の制御・完成物整理・CapCut編集を担当する。現行のColab/NVENC経路を、GPU不明のi7へ暗黙で切り替えない。

導入後の既存入口: `uv run --no-sync python tools/produce_with_gemini.py --help`。

## 任意のWeb資料収集・ブラウザ調査

本体と別の `.venv-research` へ固定コミットのCrawl4AI/browser-useを入れる。通常制作の依存にはしない。

```powershell
py -3.12 -m venv .venv-research
.venv-research\Scripts\python -m pip install -r workstation/requirements-research.txt
.venv-research\Scripts\python -m playwright install chromium
.venv-research\Scripts\python workstation/research.py collect https://example.com --output C:/AutomationData/sources-001
```

Crawl4AIが公開ページをMarkdownと `sources.json` に保存する。ロボット規約を確認し、モデルを呼ばず、収集は1ページずつ行う。ログイン・CAPTCHA・アクセス拒否は突破しない。資料の内容と出典を確認してから、既存の制作入口へ必要な部分を渡す。情報を取得しただけで正しい事実と保証しない。

browser-useは明示したモデル/認証経路の5ステップ以内の調査用。まず実行計画だけを表示する。

```powershell
.venv-research\Scripts\python workstation/research.py browse https://example.com --task "ページの説明を整理" --provider vertex --project YOUR_PROJECT --model YOUR_EXACT_MODEL --output C:/AutomationData/browser-result.json
```

`--execute` を追加した場合だけ有料推論が発生し得る。AI Studioは `--provider ai-studio` と本人の `GEMINI_API_KEY` を明示した場合だけ。モデル要求は全体で最大5回・1ステップ1回、HTTP試行は各要求1回。追加のjudge・失敗後の最終応答・履歴要約・別モデルへのfallbackを無効にし、失敗1回で終了する。固定版のChatGoogleでは `max_retries=1` が総試行1回を意味し、Google SDKにも `attempts=1` を渡す。モデル要求の試行数は結果JSONに記録する。これは請求額や成功を保証する値ではない。

許可ドメインは指定URLのホストに限定する。閲覧・スクロール・ページ内検索などの明示的な許可リストを使い、入力・クリック・アップロード・キー送信・任意JavaScript・ファイル操作・追加LLM抽出と未知のツールを外す。既定拡張機能とCAPTCHA補助も無効化する。初期化に失敗してもブラウザを閉じ、`done` だけでは調査成功と扱わない。実ブラウザとGeminiの組合せは今回未実行。

任意環境へ導入した実際のbrowser-use/Google SDKの接続処理だけを確認する場合は、次を使う。ブラウザ起動を置き換え、外部通信を禁止してHTTP応答を模擬する。通常の制作・setupからは呼ばない。

```powershell
.venv-research\Scripts\python workstation/check_research_integration.py -v
```

## 9つの公開リポジトリをPCへ配置する

最初にPCへGitとPython 3.12を用意し、専用ブランチを取得する。

```powershell
git clone --branch feat/portable-automation-20261031 https://github.com/nyu1791-collab/ClipForge.git
cd ClipForge
```

[各リポジトリの反映先](DEPLOYMENTS.md)・[外部候補の採用判断](ADOPTION.md)・[実施した確認](VALIDATION.md)。

[今回の見直し結果](reviews/recheck-20261031.md)に、字幕の誤対応・出力先・ブラウザの既定機能の修正と、実SDKを使うオフライン確認の範囲をまとめた。

追加の意見照会用に、[Antigravityへ貼り付ける現状説明](reviews/antigravity-20261031/REQUEST.md)と[実行状態](reviews/antigravity-20261031/STATUS.json)を保存した。指定「4 アルゴン」Highの回答は未取得。独自に発見して直した内容は[照合結果](reviews/antigravity-20261031/ASSESSMENT.md)に分けて記録している。

このCloneは軽い導入コマンドで、本体の制作や投稿は実行しない。既存フォルダを上書き・reset・自動更新しない。

```powershell
py -3.12 workstation/manage.py workspace --root C:/Automation
# 計画を確認したあとソースを取得する
py -3.12 workstation/manage.py workspace --root C:/Automation --apply
# 最初は使うものだけに絞ってもよい
py -3.12 workstation/manage.py workspace --root C:/Automation --project shorts-only --project ClipForge-Delivery --apply --install
```

選定対象はGitHubプロフィールで確認できた公開9件。非公開の全リポジトリ一覧はAPIの接続制限で取得できなかった。既知のai-model-workersは以前の保存・復旧用で、新PCの必須実行環境へ追加しない。依然として未完了の制作データ公開移管は、このPC導入とは別の保留作業として保持している。
