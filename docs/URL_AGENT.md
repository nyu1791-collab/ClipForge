# YouTube URL受付とDeepSeek補助エージェント

公開YouTube録画・処理済み配信アーカイブを対象とする追加経路です。従来のローカル動画CLIと、モデルを追加しない開発ハーネスは引き続き使えます。主担当はDeepSeekなしでも読み取り・修正・検証できます。今回の追加は、URL処理後の確認と小さな修正を別の補助エージェントにも任せるためのものです。

## 起動

VMの `clipforge-agent ping` で設定を確認し、`clipforge-agent submit 'https://www.youtube.com/watch?v=VIDEO_ID'` で1件を登録します。返されたIDを `clipforge-agent status ID` / `result ID` で確認します。ChatGPTとClaudeはそれぞれのDesktop Commander接続から同じCLIを使えます。ユーザーがチャットへURLを送った場合は、接続中の主担当が登録します。チャットの送信自体を監視するアプリ連携はありません。

待機中は推論を呼びません。VMは受付と状態照会のみで、動画・ASR・OpenCodeは非公開 `ai-model-workers` のGitHub-hosted CPU runnerで実行します。同時実行1件、受付待ち上限4件。dispatch直前に状態を永続化し、結果不明でも同じ依頼を自動再送しません。不明なdispatchは運用者がrunを照合するまで後続を止めます。

## 動画の範囲

- HTTPSの単一YouTube動画のみ。24秒〜60分、最大1GiB・1080p。プレイリストや進行中の配信は対象外です。
- Cookie・他アカウント・別サービスを使いません。ログイン要求、地域制限、HTTP拒否等は停止して報告します。
- 日本語Whisper small、切り抜き1本、縦720p・fit・字幕付き。候補3本、各 `min(60秒, 元動画長/8)`。Geminiに渡す合計は最大180秒、元動画長の37.5%以下を想定し、既存P1でも再検査します。設定はartifactに保存します。
- Geminiは `gemini-3.8-flash` の選定1回。別モデルfallback・HTTP再試行は行いません。入力原本は処理後に削除し、公開Gitへ保存しません。
- 出力MP4・ASS・SRT・manifestは非公開artifactに7日保存します。completeは技術処理の完了で、人間による内容承認ではありません。実素材で字幕・場面・権利・編集品質を人が確認してください。

## DeepSeekとOpenCode

`opencode-ai@1.19.0` のネイティブsessionを使用し、モデルは `opencode/deepseek-v4.1-flash-free` に固定します。名前にfreeがあっても将来の料金・無料枠は保証しません。別モデルに切り替えません。OpenCodeのretryイベント、モデル不一致、追加権限要求、8分超過で停止します。最大6回のツール使用可能ターンと、必要なら最後の文章のみのターン1回です。1セッション内の複数生成は、ツール結果を見て修正するために使います。通常CIはモデルを呼びません。

補助エージェントは新しいrunnerの専用Git worktreeでコードを読み、許可されたターミナル検査を行い、再現可能な不具合だけ小さく修正できます。編集対象は `captions.py` / `media.py` / `signals.py`、最大3ファイル・差分150行・30KB。設定・テスト・依存・workflow・Git履歴の変更や公開操作は許可しません。OpenCodeのpermission設定と事後のGit差分検査による制限であり、OSコンテナの隔離を保証するものではありません。

渡す診断はアプリ管理の状態・数値・型のみ。動画・音声・文字起こし・タイトル・Cookie・GitHub/Gemini認証を渡しません。成功時は監視報告だけで編集しません。ダウンロード拒否やquotaをコード修正で回避しません。編集後は資格情報を除いた環境でlint・format・offline unitを実行し、検査を通ったpatchだけ非公開artifactへ出します。mainや稼働中ソースに自動反映せず、主担当が差分・根拠・検査結果を確認して採用します。修正による動画の再生成も自動では行いません。

`clipforge-agent fixture` は意図的な小さなバグを専用worktreeに作り、実モデルによるファイル編集とターミナル検査を確認する明示的な検証です。動画・Geminiを使いません。実行の証拠はprivate runのreceiptで確認します。CLI導入やモデル一覧だけを推論成功と扱いません。

Opusは従来の `opus-bridge` から提案のみを受け取り、主担当が修正します。

## 配置

公開側の `deploy/clipforge-url.yml` が非公開workflowの原本です。VMサービスは標準ライブラリの `tools/url_bridge.py` を使い、root管理の `/etc/clipforge-agent/config.json` に検査済みの完全SHAと許可UID（1000、1001）を指定します。ソケットは `/run/clipforge-agent/bridge.sock`、状態は `/var/lib/clipforge-agent`。APIキーはVMのbridgeへコピーせず、Actions Secretを該当stepだけに渡します。

Gemini・DeepSeekの利用量、Actions枠、実素材の編集品質は別途確認が必要です。URL未提供の段階では実動画の品質確認は完了しません。
