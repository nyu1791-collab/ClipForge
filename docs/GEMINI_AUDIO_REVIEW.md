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
