# 経路の確認記録（2026-10-28）

文書に書かれた具体的な主張を、コードと照合した結果です。コードの読み取りと検索で確認し、テストや実行は行っていません。

| 経路 | 文書の主張 | コードの根拠 | 結果 |
|---|---|---|---|
| 取得（Kick） | 一時的な障害（429・5xx・通信障害）だけ 10・30・60 秒の間隔で最大 3 回再試行 | `kick_media.py` の `MEDIA_RETRY_DELAYS=(10, 30, 60)` と一時障害の判定 | 一致 |
| 取得（Kick） | メタデータ API の 404 のときだけ公開ページへ切り替える | `kick_media.py`：`api_reason != "http_404"` の場合は拒否 | 一致 |
| 再開 | 保存した依頼 ID が完全一致する Run だけを探す | `actions_monitor.py`：表示タイトルが一致しない Run は採用しない | 一致 |
| 再開 | 401・403・429 は監視を一時停止し、HTTP 状態だけを保存する | `actions_monitor.py`（`rate_limited` / `access_denied`）、`produce_with_gemini.py`（`monitoring_paused`） | 一致 |
| 再開 | 依頼 ID ごとに待ち行列を作る。進行中の Run は自動キャンセルしない | `clipforge-stages.yml` の `concurrency`：group は依頼 ID ごと、`cancel-in-progress: false` | 一致 |
| 収集 | `completed/clip-NNN` に保存し、保存済みの結果は再利用する | `streaming_collection.py` の `collect_clip` | 一致 |
| 収集 | 記録されたハッシュがあれば照合する。なければ動画のハッシュは照合しない | `release_transfer.py`：期待値が `None` の場合は照合しない | 一致 |
| 修正 | 状態は `prepared` → `publishing_or_unknown` → `published` | `editorial_revision.py` | 一致 |
| 修正 | `--reuse-render` は render-only と、直前の完成 Run・依頼の組が必要 | `produce_with_gemini.py` の入力検査 | 一致 |
| 字幕 | 参考画像は最大 3 場面。品質の判定ではない | `visual_reference.py`（3 枚、説明文） | 一致 |
| 字幕 | 文字は plain、2 行まで、1 行 18 字、読み速度 5 字/秒、無音 0.5 秒で字幕を消す | 編集経路の設定ファイル、`captions.py`（`max_tail_ms=500`） | 一致（既定値ではなく設定ファイルの値） |
| 描画 | 同時の GPU 枠は 5 | `gpu_slots.py`（`SLOT_COUNT=5`） | 一致 |
| 文字起こし | 窓は 898 秒、2 秒重なり。重複整理の条件 | `transcribe_pipeline.py` | 一致（条件を文書に反映済み） |
| 納品 | 納品ワークフローは 2 つの Drive アカウントを許可する | `clipforge-drive-delivery.yml` → `clipforge_release_delivery.py`（`TARGET_ACCOUNTS`） | 一致 |
| 納品 | Drive の初回登録 | `clipforge_drive_delivery.py` は `n.yu1791@gmail.com` だけを受け付ける | 注記を追加 |
| 清書 | 語の置き換えは音で確かめられる場合だけ | `gemini_subtitles_ja.md` | 指示を追加（効果は次の実行で確認） |

## 未確認
- Colab の割り当て順（L4 → T4 high-RAM → T4 standard）：`COLAB.md` の記述。実行ログでは T4 STANDARD の割り当てを確認したが、順番は未確認。
- 強調の黄色：未実装（既に文書に記載）。
- Actions の入力一覧（`QUICK_START.md`）：一部のみ確認。
- Kick のチャット欄の表示：未確認。
