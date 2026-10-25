# 明示依頼時のGemini音声文字起こしと早口査読

通常制作の品質監査を一律に復活させず、利用者が指定した音声の文字起こし・早口字幕査読だけを追加する。

`python -m clipforge.gemini_audio_editor --analysis ASR_DIRECTORY --output NEW_DIRECTORY --config config.landscape-long.example.json`は、保存済みの音声・Parakeet全文と時刻を使い、次の3リクエストを各1回実行する。

1. Gemini 3.5 Flash / MINIMALに配信全体の音声を渡し、全文を文字起こしする。
2. Gemini 3.8 Flash / MEDIUMに全体文字起こしとParakeet時刻を渡し、300〜900秒の区間を5本選定する。
3. Gemini 3.8 Flash / HIGHに採用5本の音声を渡し、熟考した逐語文字起こしと早口部分の査読を行う。

モデル・プロジェクトは既定のVertex接続を使い、GitHub OIDCから得た短期トークンを認証ヘッダだけに使う。認証情報やCookieをプロンプトへ含めない。失敗時のAPI再送・モデル変更は行わない。

全体音声はモノラル16kHz Opus 12kbps、採用部分は24kbpsで入力用コピーを用意する。元音声・元ASRは変更しない。入力は20MiB以内、出力は8MiB以内に制限し、モデルが全文処理完了や音声アクセスを返せなかった場合は停止する。

早口候補は、Parakeetの発話区間で「空白・句読点以外の文字数 / 区間秒数」が7以上の行から抽出する。この値は音声の実測話速ではなく査読対象を探す目安である。3.8は採用部分を音声から文字起こしし、指定した早口候補を全件査読する。元時刻とsegment IDを維持し、すべての行を返したことを確認する。

原音が不明な行は不明と記録して自動訂正しない。変更可能な字幕は、元の単語境界と時刻を維持できる場合だけ`caption_edits`へ反映する。適用・不適用・不確実な行は音声査読記録へ残す。動画全体の再走査、音量監査、査読を理由とした自動再描画は行わない。

`audio-editor-receipt.json`にAPIごとのモデル・思考レベル・時間・トークン使用量を記録し、音声準備の時間と区別する。並列時間の合計を実時間としない。全体文字起こし・区間選定・採用部分の文字起こし・早口査読・最終採用JSONを非公開Releaseへ保存する。

描画の制御側へ`CLIPFORGE_AUDIO_REVIEW_RELEASE=clipforge-kick-editorial-RUN_ID`を指定すると、完成した査読記録をGPUへ渡し、字幕・制作記録と一緒に最終成果物へ保存する。大きな字幕修正JSONはGitHubのworkflow入力欄へ詰め込まず、同じ非公開Releaseから受け渡す。
## HIGH通信切断からの再開

`gemini_audio_recovery`は、保存済みのASR、全体文字起こし、区間選定、失敗したHIGH記録を検証し、採用5区間の音声だけを再準備してHIGHを1回だけ呼び出す。3.5全文文字起こし、3.8 MEDIUM区間選定、Kick取得、Parakeet ASRは再実行しない。出力先は前回と分け、前回の記録と原音を保持する。

HIGHはVertex AIの`streamGenerateContent?alt=sse`で受信し、受信イベントをローカルの非公開作業領域へ逐次保存する。正常完了は`finishReason=STOP`、JSONの解析、対象segmentの全件一致、早口候補全件の査読ID一致を確認してから記録する。接続断・不完全応答時は部分ストリーム、例外型・メッセージ、HTTP状態、応答識別ヘッダー、取得済みusageを記録し、再送はしない。切断後の課金・サーバー側完了状態はusageが受信できない場合は不明とする。

以下のコマンドは、既存HIGHの失敗が確認でき、ユーザーが追加のHIGH試行を明示的に許可した後に限り実行する。許可フラグは機械的なガードであり、API呼び出しを自動で許可する運用ではない。

```bash
python -m clipforge.gemini_audio_recovery \
  --analysis ASR_DIRECTORY \
  --prior-review FAILED_EDITORIAL_DIRECTORY \
  --prior-run-id 40237163869 \
  --output NEW_RECOVERY_DIRECTORY \
  --config config.landscape-long.example.json \
  --confirm-new-high-attempt
```

今回の切断は約289.8秒後の`RemoteDisconnected`として引き継がれている。既存の`generateContent`は900秒をクライアント上限に設定しており、この上限到達では説明できない。一方で既存例外処理は元の詳細を記録せず、GitHub Actionsログにも`PipelineError`しか残らないため、サーバー側の切断理由までは確定できない。ストリーミングは応答全体を待つ接続方式を改め、途中データと詳細診断を残す対策であり、外部サービスの切断を完全に保証排除するものではない。