# 現時点の照合結果

2026-10-31 UTC。この表は初回修正の記録。後続の修正と現在の確認範囲は[再見直し結果](../recheck-20261031.md)を参照。Antigravity「4 アルゴン」Highの回答は未取得。以下は担当者によるソース照合であり、外部モデルの意見として記録しない。

| 発見 | 根拠 | 採否と実装 |
|---|---|---|
| モデル呼び出し前にassertで失敗する設定 | browser-use `b49a68e4060a` の `llm/google/chat.py` は `max_retries >= 1` と `range(max_retries)` を使う | 採用。ChatGoogleは1、Google SDKのHTTP attemptsも1 |
| Agentの既定で追加モデル要求が起こり得る | 同SHAの `agent/service.py`: use_judge=True、max_failures=5、final_response_after_failure=True、空応答への再要求 | 採用。judge/失敗後応答/履歴要約を無効化、失敗1回で終了。1ステップ1要求・合計5要求を送信前に制限 |
| 少数の除外だけでは任意JavaScript等が残る | 同SHAの `tools/service.py`: evaluate、select_dropdown、write_file、read_file、extract。`tools/registry/service.py` に除外APIがある | 採用。明示的な閲覧ツールの許可リスト。未知のツールも除外し、Agent初期化後にも適用 |
| 初期化失敗時にブラウザを閉じない | 修正前の `workstation/research.py`: Agentの構築がtryの外 | 採用。構築もtry内へ移動しfinallyで終了 |
| doneを成功と混同する | 修正前は `completed=result.is_done()`。終了と成功は別 | 採用。completedは `is_successful() is True`、finishedを別に保存 |

反映対象は任意browser-useを持つClipForge、MoneyPrinterTurbo-Foundation、voiceBox-Pipelineの3件。制作本体のモデル、再試行規則、字幕、GPU、納品経路の変更ではない。

修正コミット:

- ClipForge: `36f4a2cee66c9342827db43d8775f8d0222b5832`
- MoneyPrinterTurbo-Foundation: `0fffc87c3c2d61dd822fb306d13e13255be5a47a`
- voiceBox-Pipeline: `b967d2c420a87d376680ab34832cbdd0475bffd9`

確認は模擬ブラウザ/APIを使った各13件、計39件とruff、git diff --check。内訳は既存8件+追加5件。API拒否や暗黙の再要求で追加の模擬provider呼び出しが発生しないこと、上限、初期化失敗時の終了、未知ツール除外、done失敗の扱いを確認した。実Chromium・有料モデル・Windows実機は未確認。

外部回答の採否欄は、実際の回答とモデルID、High設定、対象SHAを記録できた時点で追加する。現時点で外部回答、請求額、映像品質の判定はない。
