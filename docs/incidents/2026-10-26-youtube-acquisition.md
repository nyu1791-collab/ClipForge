# 指定YouTube動画の制作試行

ユーザーの「全体を迅速に整理した後、指定動画で実行」の依頼により、PR #38で起動と入口文書を整理してから通常制作を一度投入した。監査・strict品質検査・テストは行っていない。

| 項目 | 記録 |
|---|---|
| Run | [40347517742](https://github.com/nyu1791-collab/ClipForge/actions/runs/40347517742) |
| request ID | `2f4e0714a6ce4db6a2809f6a23838f1e` |
| コードSHA | `9a51a2bc89ed92d4559b80f46dc289eb5368b625` |
| 動画ID | `ZuYtwtf6kM8` |
| 設定 | 最大360p素材、720pキャンバス、品質ゲートoff |
| 到達点 | runner準備後、元音声取得のメタデータ取得で停止 |
| 理由 | YouTubeの `Sign in to confirm you're not a bot` 応答 |
| 実行状態 | Gemini呼び出し0、GPU描画未開始、完成動画0本 |
| 制御側の経過時間 | 約52.1秒 |

新方式の音声取得ステップだけ `YOUTUBE_COOKIES_B64` の受け渡しが抜けていたため、既存の認証入力へ接続した。Cookieの登録有無・有効性・この接続での取得成功は未確認。接続済み端末からSecret名一覧を取得する操作も権限不足（403）だったため、未登録と断定しない。

ログイン確認を回避する取得経路・別アカウントへの変更や、自動再投入は行っていない。正規のYouTubeログイン接続が利用可能になったうえで、改めて実行する必要がある。旧Runを成功や品質不合格と報告しない。

制御側の実行記録は `/workspace/private/ZuYtwtf6kM8-production-20261026/production-state.json`。既存のこの出力先を新規投入に使い回さない。

## Cookie接続反映後の継続

ユーザーの再実行指示でRun [40348258357](https://github.com/nyu1791-collab/ClipForge/actions/runs/40348258357)、request `7fa3a26af5c743eb9be7f6c5ef6ad748` をソース `8533537be815b01a696635e25ec4493615f6d263` で一度投入した。

- 受付15:19:54 UTC、ジョブ生成15:35:03 UTC、runner起動15:35:05 UTC。受付からジョブ生成まで909秒、生成から起動まで2秒。遅延の層はGitHubのジョブ生成前。内部理由はAPIに出ておらず、共有concurrencyが今回の原因だったとは確認できていない。
- 待機中はRunがqueuedの一方、通常キャンセルはcompleted、強制キャンセルはnot queued yet、再実行はalready runningと拒否された。変更は受理されず、同じattempt 1が後で開始した。追加dispatchは行っていない。
- 音声取得は15:35:16〜15:36:05に成功。Vertex認証も15:36:05に成功し、話題・字幕工程へ進んだ。初回の音声取得不能と、再実行の開始待ちは別の問題として扱う。完成動画の生成・回収成功とはまだ区別する。
- ローカルの旧監視は読み取りAPI失敗で終了し、遠隔Runが継続しているのにfailed_or_unknownを残した。新しい監視では読み取りのみを再試行し、期限切れをmonitoring_pausedと区別する。`--resume-existing-run` で元Runへ再接続できる。
- 通常制作のconcurrencyをrequest別にし、無関係な制作との共通ロックを除去した。GitHub内部の遅延そのものを修正・解消したとは主張しない。進行中Runへコード更新のための再投入はしない。

再接続先は `/workspace/private/ZuYtwtf6kM8-cookie-connected-20261026/`。保存済みの動画・字幕・モデル応答を公開Gitへ含めない。
