# 文字起こし（Parakeet を最大4台で）

文字起こしは NVIDIA Parakeet（`nvidia/parakeet-tdt_ctc-0.6b-ja`、NeMo 3.0.0）だけを使う。全編を1回起こし、その結果を話題分けと字幕の両方に使う。区間ごとの起こし直しはしない。

## 流れ

1. **runner（`tools/split_asr_job.py`・`asr_split.py`）**：取得した音声を16 kHz・モノラルの WAV に一度だけ復号し、`asr_parts`（既定4、1〜4）個に分ける。分け目は各等分点の前後60秒の中で最も静かな0.2秒の中心に置く。部分は重ならず、全体を一度ずつ覆う。各部分を WAV と依頼 JSON にして、部分ごとの一時 Release に置く。
2. **割り当て**：部分ごとに Colab を1台取る。長い2つは L4 → T4 high-ram → T4 standard、残りは T4 high-ram → T4 standard → L4 の順に試す。アカウントの同時上限（T4 2台・L4 2台、2026-10-22 の実測）を超えると拒否されるので、そのときは60秒おきに最大45分待ち、他の部分が解放した GPU を使う。台数が足りなくても、待てば全部分が終わる。
3. **Colab（`tools/colab_remote_job.py` の `asr`・`asr.py`）**：プロジェクトの環境（uv・CUDA ライブラリ）は入れない。Colab 標準の Python で、まず NeMo の導入とモデルの取得を裏で始め、その間に部分の音声を取得して SHA-256 を照合する。取得が済むと、部分を約8分ずつ静かな位置で分け、まとめて認識する（`batch_size=1`）。文字ごとの時刻を0.35秒以上の間で区切り、30字を超える塊は最も広い間で割って、字幕の行と単語（1文字＝1語）にする。
4. **結合（runner）**：各部分の結果の依頼 ID・コード SHA・音声 SHA・ファイルのハッシュを照合し、部分の開始時刻だけずらしてつなぐ。全体の文字起こしを `transcripts/KEY.json` に置き、話題分けを一度だけ行う。

部分の結果・時間・GPU の種類は、ログの `ASR part N: … on L4 HIGH_RAM, held … s (input … s, prefetch_wait … s, recognise … s)` と、最後の `Compute units used by all ASR parts:` に出る。CU は残高の差なので、ほかの run と重なると `not determinable` になる。

## 文字起こしの鍵

`transcripts/` のファイル名は、元音声の SHA-256・長さ・開始位置・言語・モデル・NeMo の版・`asr.py` と `asr_split.py` のハッシュ・分け目から作る。後の段階（プレビュー・描画）は、分析が記録した鍵のファイルを読むだけで、認識をやり直さない。鍵が合わなければ止まる。

## 失敗したとき

- 部分のどれかが失敗すると、全体を失敗にする。片側だけで話題分けはしない。自動の再送もしない。
- 取得済みの部分の結果は、非公開の `clipforge-partial-REQUEST_ID` artifact に7日残る。
- Colab 側で NeMo の導入や認識が失敗すると、ログの末尾（ASCII の行だけ。日本語の本文は含めない）が受領ファイルに残り、Actions のログに `Colab log:` として出る。
- 部分の一時 Release は、ジョブが止まっても workflow の後始末で消す（`staged-tags` の一覧）。

## 時間の目安（推定）

Colab 側は、NeMo の導入とモデルの取得（初回の数分）と、認識（部分あたり1〜2分程度）。Whisper 時代にあった環境構築（1台33〜62秒）は無い。実配信での実測はまだ無い。
