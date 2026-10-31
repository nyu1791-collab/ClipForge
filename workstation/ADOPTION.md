# 外部自動化リポジトリの整合性と採用結果

確認日: **2026-10-31**。全8候補の公開ページ、実際のGit checkout、LICENSE、README、関連ソースを確認した。APIホストはネットワークで拒否されたため、GitHub APIやDesktop Commanderで調査したとは主張しない。スター数は確認時点の表示値で、採用判断の基準にしない。

## 外部候補

| 候補 | 確認したstars | ライセンス | 判断 |
|---|---:|---|---|
| [Hao0321/video-autopilot-kit](https://github.com/Hao0321/video-autopilot-kit) | 2246 | MIT | 採用：設計の一部 |
| [zhouxiaoka/autoclip](https://github.com/zhouxiaoka/autoclip) | 9880 | MIT | 採用：publish kitの考え方 |
| [rushindrasinha/youtube-shorts-pipeline](https://github.com/rushindrasinha/youtube-shorts-pipeline) | 2342 | MIT | 採用：出力段の分離 |
| [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io) | 35411 | Apache-2.0 | 採用：任意の独立サービス |
| [browser-use/browser-use](https://github.com/browser-use/browser-use) | 119617 | MIT | 採用：明示実行の調査入口 |
| [unclecode/crawl4ai](https://github.com/unclecode/crawl4ai) | 86621 | Apache-2.0 | 採用：任意の出典付き収集 |
| [gitroomhq/postiz-app](https://github.com/gitroomhq/postiz-app) | 40259 | AGPL-3.0 | 一部採用・本体の常駐は保留 |
| [Skyvern-AI/skyvern](https://github.com/Skyvern-AI/skyvern) | 23317 | AGPL-3.0 | 今回は保留 |

### Hao0321/video-autopilot-kit

Windows前提の素材パス、独立した設定、投稿・納品用素材の分離。既存CapCut出力と組み合わせ、manage.pyのrelink/packageへ独立実装した。大きな運用フレームワークやスキル指示・品質ゲートは移植しない。

確認した版: [`a50c73bde0c5`](https://github.com/Hao0321/video-autopilot-kit/tree/a50c73bde0c5bcb5b82e4d28f812e780e20cb1a7)。参照: src/project_paths.py、src/platform_compat.py。

### zhouxiaoka/autoclip

既存動画・字幕・カバー・投稿文をフォルダで渡すpackageを追加。字幕時刻と原発言を維持する。AutoClipのASR・別プロバイダ・全フロントエンドを重ねて動かさない。

確認した版: [`d13c8343d3dc`](https://github.com/zhouxiaoka/autoclip/tree/d13c8343d3dcc453230f079af2175ec3df9e10d4)。参照: backend/services/studio/publish_kit.py、README-EN.md。

### rushindrasinha/youtube-shorts-pipeline

現行名はVerticals v3。研究→制作→投稿素材を分離する構成を参考にした。台本生成やWhisperを重ねず、Postiz向け下書きJSONだけを用意する。YouTubeの実投稿は実装・実行していない。

確認した版: [`48835dea9c0c`](https://github.com/rushindrasinha/youtube-shorts-pipeline/tree/48835dea9c0cc9d119e19ad9365717006d6f59a6)。参照: README.md、scripts/setup_youtube_oauth.py。

### dgtlmoon/changedetection.io

ClipForgeと配送リポジトリに同じ任意のComposeを置いた。公式0.60.10をdigest固定、1ワーカー/60秒/768MB/AI無効。価格・在庫ページは本人が設定。ミリ秒検知、遅延ゼロ、各ECの対応を保証しない。

確認した版: [`7a925eb5e338`](https://github.com/dgtlmoon/changedetection.io/tree/7a925eb5e3382297e7a87932fe5429cb40b058bc)。参照: docker-compose.yml、changedetectionio/store/、changedetectionio/api/。

### browser-use/browser-use

ClipForge、MoneyPrinter、voiceBoxの独立research環境だけにGit SHA固定で追加。モデル・Vertex/AI Studioを明示し、計画が既定。--execute時だけ最大5ステップ、再送なし。資料調査用に入力/クリック/アップロード/キー送信を除外する。

確認した版: [`b49a68e4060a`](https://github.com/browser-use/browser-use/tree/b49a68e4060acb226da8f784194fef301c53a6d7)。参照: browser_use/llm/google/chat.py、browser_use/tools/service.py、browser_use/browser/profile.py。

### unclecode/crawl4ai

同じ3リポジトリに独立したcollect入口を追加。公開ページを1ページずつMarkdownと出典JSONへ保存し、LLM要求0件。通常制作や他のアプリのPython環境へ依存させない。

確認した版: [`888299fb5f0b`](https://github.com/unclecode/crawl4ai/tree/888299fb5f0b82181897dc0e61bfb4b036f0aea5)。参照: crawl4ai/async_configs.py、README.md。

### gitroomhq/postiz-app

CreatePostDtoに合わせるtype=draftのJSONを全リポジトリで作成可能。投稿先/アップロード済みmedia-id/URLは本人の接続を指定。AGPL本体は組み込まず、別サービスとして扱う。16GBでCapCutと重いSNS基盤を同時常駐させない。

確認した版: [`35a482901583`](https://github.com/gitroomhq/postiz-app/tree/35a482901583ed4219ff43afffde184e8ee373a6)。参照: libraries/nestjs-libraries/src/dtos/posts/create.post.dto.ts、libraries/nestjs-libraries/src/dtos/media/media.dto.ts。

### Skyvern-AI/skyvern

ブラウザ自動化の役割がbrowser-useと重なり、サービス構成とメモリ消費が増える。AGPL。必要な具体的業務が決まったときだけ独立サービスとして再評価する。

確認した版: [`48efb4adc395`](https://github.com/Skyvern-AI/skyvern/tree/48efb4adc395b556615bf96a6a9dd317313932f0)。参照: README.md、LICENSE、docker-compose系設定。

## 自分のリポジトリへの反映

| リポジトリ | 今回入れたもの | 維持する分離 |
|---|---|---|
| ClipForge | PC配置カタログ、素材パック、CapCut素材パス移行、Postiz下書き、任意の収集/ブラウザ/監視入口 | 既存モデル・再開・Colab経路 |
| shorts-only | 独立導入、字幕時刻を保つ素材パック、CapCut素材パス移行、投稿下書き | 独立した制作環境 |
| 3D- | 独立導入、図解動画/字幕の素材パック、ドラフトのパス移行 | 図解生成と切り抜き制作は別 |
| Translation | 独立導入、翻訳字幕と元時刻の保持、素材パック | 翻訳側のADC/WIF・DB |
| StreamCut | 基本環境の導入と完成物の移行 | CUDA推論とColab。CPUへ暗黙切替しない |
| MoneyPrinterTurbo-Foundation | uv/Python3.11導入、素材パック、任意の資料収集/ブラウザ | 独立MPT_HOME、既存のモデル・TTS |
| voiceBox-Pipeline | 独立導入、素材パック、出典付き収集/明示ブラウザ調査 | CPU VOICEVOXと既存Gemini入口 |
| ClipForge-Delivery | 独立導入、相対manifestの受け取り、投稿下書き | 完成物だけで連携し、他アプリのコード/DBに依存しない |
| discord-digital-delivery | PCの開発入口、任意のchangedetection.io、ローカル秘密の除外 | Worker/R2/D1と商品・決済データを直接結合しない |

各 `workstation/manage.py` は標準ライブラリだけで動き、相互にimportしない。同じ実装を各repoへ配置したため、単体でcloneして使える。参照したフレームワークのコードやスキルルールはそのまま複製していない。MIT/Apacheライブラリは独立環境や公式コンテナから使い、AGPLアプリは本体を取り込まない。メインの依存/モデル/字幕ルール/品質ゲートを増やしていない。

## 16GB PCでの現実的な構成

通常は**使う制作アプリ1つ＋CapCut＋必要なブラウザ**。監視は任意の1コンテナ。Crawl4AIとbrowser-useのChromiumは1つずつ動かし、Postiz/Skyvern/ローカル大規模モデルは既定の常駐にしない。i7の型番、GPU、VRAM、ストレージ、Windows版が未確認なので生成速度やNVENCを保証しない。

「完全放置で収益化」「数行で受託5〜15万円」「任意のサイトが必ず読める」「構造が変わっても止まらない」は確認できず、採用条件にしない。監視は巡回間隔と相手サイトに依存する。SNS投稿はアカウントのOAuth・API権限・枠を別に準備する。CapCut Proの契約だけで外部APIや内部JSON互換性は保証されない。

## 状態と限界

実際に確認した内容は `VALIDATION.md`。Windowsの実機、CapCut Proでの読み込み、実Gemini推論、SNS投稿、Discord通知、サイトの実監視、GPU計算、既存制作データの公開移管は今回実行していない。PC移行を理由に同じ制作を再投入しない。

公開プロフィールから確認できた9件を対象とした。非公開リポジトリ全件の列挙はできておらず、「非公開を含む全件調査済み」とは主張しない。既知のai-model-workersの旧保存庫・未完了公開移管は別の履歴として保持する。
