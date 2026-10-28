# 字幕ルールの適用と記録（現行）

字幕の表現規則の本文は [gemini_editorial_rulebook_ja.md](../src/clipforge/prompts/gemini_editorial_rulebook_ja.md)。この文書は、その規則が実際のリクエストへどう入り、何が記録されるかだけを説明する。過去の版の規則（GPU 文字起こしと 3.5 Flash の整文）は現行の経路では使わない。

## 読み込まれる規則

- 共通本文: `src/clipforge/prompts/gemini_editorial_rulebook_ja.md`。すべての音声工程の systemInstruction に入る。
- 話題選定: 共通本文と `gemini_topics_ja.md` を連結する。
- 清書: 共通本文と `gemini_subtitles_ja.md` を連結する。
- Transcribe には自由文の規則を送らない。設定は `transcription_config()` に定義し、[TRANSCRIBE_RULES.md](TRANSCRIBE_RULES.md) に従う。
- 読み込みは `audio_understanding.Client.generate` が行う。規則ファイルが欠けている・空の場合はデプロイの不備として停止し、規則なしで制作を続けない。
- モデルの「読みました」という自己申告や読了の検査は行わない。

## 記録

- `*.prompt.txt`: 実際に送った systemInstruction と依頼文。
- 受領記録 `audio-editor-receipt.json`: `prompt_rules_sources`（各規則のパスと SHA-256）、`prompt_rules_sha256`（連結した本文）、`prompt_sha256`（依頼文全体）。
- 規則を変えると `prompt_sha256` が変わり、保存済みの応答は再利用されない。これは意図どおりの動きである。
- 規則の内容をモデルが守ったかは、ハッシュでは保証しない。

## 清書の出力と描画

- 応答は `segments[].source_ids` と `text`。時刻は `source_ids` から Transcribe の発話区間をコードが引き継ぐ。
- 新規の編集データには `subtitle_policy = meaning_preserving_cleanup_v1` を保存する。
- 描画は plain の字幕で、`caption_preserve_text` を使う。清書した本文を機械的な反復除去に通さず、単語カラオケは付けない。
- 話者色は Transcribe のラベルによる。感情は neutral。
- 効果音は現行の清書では指示しない（`sound_effects` は空配列）。描画側の効果音処理は、修正パッチで指示が与えられた場合だけ働く。

## 見出し（title）と感情の修正

- 見出しは話題選定の応答の `title` をそのまま使う。見出しだけを作る追加の呼び出しはしない。
- 見出しや感情を変えるときは、修正パッチで保存済みの編集データを直し、同じ描画へ進む。元の音声理解へは戻らない（[PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)）。

## 未確認

- 3.8 Flash の清書が規則どおりに返すかは、実音声の Run でまだ確認していない。
