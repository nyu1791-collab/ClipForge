# 音声からの話題選定と、採用話題ごとの清書字幕（現行の音声経路）

接続・制作・再接続は [QUICK_START.md](QUICK_START.md)、修正・再利用は [PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)、Antigravity 経路の起動入力は [ANTIGRAVITY_ROUTE.md](ANTIGRAVITY_ROUTE.md)。この文書は音声経路の工程・要求数・保存・再試行だけを扱う。全編 Parakeet ASR の旧経路は [ASR.md](ASR.md) と [AUDIO_FIRST.md](AUDIO_FIRST.md) に復旧用として残す。

## 工程

1. runner が元配信の音声だけを取得する（YouTube・Twitch・Kick・直接HTTPS）。検証済みの保存音声があれば、同じ素材であることを照合して再利用する。
2. Gemini 3.8 Flash / MEDIUM（Antigravity SDK 経由）で話題・見出し・時刻・理由を得る。録画長に応じて 1 体（1 時間以下）・2 体（2 時間以下）・4 体に分け、各体は持ち主の範囲の前後 600 秒を聞く。境界近くの話題は持ち主の体だけが採用する。全文の文字起こしや字幕は作らない。
3. 採用話題ごとに、前 0.3 秒・後 0.5 秒の余白を一度だけ加えて音声を切り出す。話題の音声は 24 kbps の Ogg/Opus（音声向けの低負荷設定）で符号化する。
4. Gemini 3.5 Transcribe（Vertex 直接）で採用部分だけを文字起こしする。日本語（ja-JP）・VERBATIM・単語時刻・話者分離を指定する。898 秒以内の話題は 1 要求、超える話題は 2 秒重ねた窓に分ける。窓の境界では文字が完全に一致し時刻が重なる重複だけを除く。
5. Gemini 3.8 Flash / MEDIUM（Antigravity SDK 経由）へ、採用区間の音声と文字起こし（文字列・発話時刻・話者ラベル）を話題ごとに 1 回渡す。モデルは誤字脱字と明らかな誤認識だけを直す。話者判定・効果音・感情の指示はしない（`speaker_assignments` と `sound_effects` は空配列、感情は neutral）。
6. コードが応答の `source_ids` から元の発話時刻を引き継ぐ。話者は Transcribe のラベルを、クリップ内の出現順に `speaker_1`〜`speaker_6` へ対応させる。ラベルがない・枠を超える発話は `unknown`。
7. 選定区間・見出し・字幕は非公開の編集データ（`final-selection.json`・`selected-transcripts.json`）に保存し、非公開 Release へ置く。
8. Colab GPU / NVENC で描画する。素材は最大 360p、通常の描画は 360p。制御側と runner では ASR や描画を行わない。

## 要求数と記録

- 新規のモデル要求数は、全体把握の音声窓数＋Transcribe 窓数＋採用話題の清書数。用語調査（3.5 Flash）、空白補助の Transcribe 要求、効果音・感情・見出しだけの追加要求、時刻照合・査読の要求は作らない。
- 受領記録（`audio-editor-receipt.json`）には、入力の同一性、各要求の状態（`status`）、`error_class`（`retryable` / `non_retryable`）、`transport`（`antigravity_sdk_vertex` など）を残す。利用量は Antigravity SDK が報告しないため `not_reported_by_antigravity_sdk` と記録し、推定の金額は書かない。
- 話題選定の窓と持ち主の範囲は `overview_plan` として受領記録に保存し、再開時の照合に使う。

## 再利用・再試行・エラー

- 保存済みの応答は、入力の同一性（取得元 URL・音声の SHA-256・設定）、依頼文とルールのハッシュ（`prompt_sha256`）、`finish_reason = STOP` がすべて一致する場合だけ再利用する。
- 規則ファイル（`src/clipforge/prompts/`）を変えると依頼文のハッシュが変わり、既存の保存済み応答は再利用されない。その工程は新しく要求し直す。
- 一時的な通信障害・429・5xx は同一要求につき合計 3 回まで試す。待ちは 30 秒と 90 秒（ジッター付き）。認証拒否・無効な要求は再試行しない。Antigravity SDK の一時障害（429・500・502・503・504・RESOURCE_EXHAUSTED・UNAVAILABLE・DEADLINE_EXCEEDED・INTERNAL）と空応答は再試行の対象。
- 成否不明のまま再送すると重複課金の可能性がある。Run の自動再投入はしない。
- 音声が無い・音声へアクセスできない応答・壊れた JSON・認証や通信の失敗・入力の取り違えは実行不能なエラーとして記録する。本数・尺・文字数の不足を理由に停止や再生成はしない。

## 字幕の方針

- 要約・同義語への言い換え・情報圧縮・推測の補完はしない。言い淀み・相槌・反復は削除しない。
- 清書済みの字幕は `caption_preserve_text` で描画し、機械的な ASR 反復除去（`asr_cleanup`）を通さない。単語単位のカラオケ追跡は付けない。
- 時刻は Transcribe の発話時刻を引き継ぐ。清書後の単語精度・強制アライメントは保証しない。
- 感情は neutral（段階 0）。話者色は Transcribe のラベルに基づき、判定はしない。
- 効果音は現行の清書では指示しない。
- 方針の識別子は `subtitle_policy = meaning_preserving_cleanup_v1`。旧 `meaning_preserving_compact_v1` の保存済みデータは描画のために読めるが、新しい清書の実績には数えない。

## 未確認

- 3.8 Flash の清書を実音声で動かした Run はまだ無い。Antigravity 経由の音声入力と JSON 応答は次の本番 Run で確かめ、結果を [ANTIGRAVITY_ROUTE.md](ANTIGRAVITY_ROUTE.md) の末尾に追記する。
