# 公開ClipForgeへの実行入口の統合

コードと制作Actions、段階再開、OpenCodeの定義を公開ClipForgeへ集約する。動画・ASR・作業レポートは公開しないため、ai-model-workersは非公開Release保存庫として残す。通常の操作先はClipForgeとなる。既存成果物は移動・削除しない。

## 初回移行

- Google Cloud Shellで最新版の`tools/setup_vertex_actions.sh`をもう一度実行する。Google側の許可先をClipForgeのmainと検証ブランチに変更する。
- ClipForgeのActions変数へ`CLIPFORGE_VERTEX_PROJECT_NUMBER`を登録する。旧リポジトリの変数は自動継承されない。
- ClipForgeのSecretsへ`CLIPFORGE_PAT`（非公開保存庫のContents読み書き）、`COLAB_ADC_JSON`、必要なら`YOUTUBE_COOKIES_B64`を登録する。GitHubは既存Secretの値を取得・コピーするAPIを提供しないため、元の値を所有者が登録する。値はチャットへ送らない。
- ユーザーがmainへのマージを承認済み。PR #19（233c4f0）で4つのworkflowをmainへ登録済み。実動画制作は設定移行後に実行する。

公開ActionsはRelease転送のみ対応し、公開Artifactへの音声・動画アップロードは置かない。PATがない場合はGPU確保前に停止する。既存の非公開Runを操作する場合は`CLIPFORGE_ACTIONS_REPO=nyu1791-collab/ai-model-workers`を指定する。

## 現在の範囲と未完了

動画制作・段階再開・OpenCodeのworkflow定義と投入CLIを統合した。Google許可先の再設定、Secrets/変数登録、default branchへのworkflow登録は完了。Drive納品のupload-release経路も公開ClipForgeへ移した。Drive用Secretsも所有者による登録が必要。初回OAuthの認可コード交換は公開ログへ移さず、従来の非公開設定経路を残す。実API・GPU・動画生成は実行していない。品質監査・承認待ち・自動再生成は追加しない。

設定先: [ClipForge Secrets](https://github.com/nyu1791-collab/ClipForge/settings/secrets/actions)、[ClipForge変数](https://github.com/nyu1791-collab/ClipForge/settings/variables/actions/new)。現在の接続では既存変数の読み取りも拒否されており、プロジェクト番号の自動コピーは未実施。Google Cloud Shellでは最新ブランチのセットアップスクリプトを実行する。

## 見直しで修正した点

公開Actionsでも分割ASRを開始できるよう、実行元の判定と非公開保存庫の判定を分離した。時間集計ツールの既定参照先もClipForgeへ変更した。deployの配布用テンプレートは公開Actions定義と揃え、旧設定への巻き戻りを防ぐ。旧Runを集計する場合はCLIPFORGE_ACTIONS_REPOで非公開側を明示する。

過去の非公開Runから段階再開する際は、公開側にRunが見つからない（HTTP 404）場合だけ、同じRun IDを旧実行場所で照合する。403や通信障害で別経路へ切り替えず、ASRも再実行しない。
