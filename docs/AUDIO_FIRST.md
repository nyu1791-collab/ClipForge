# 段階の詳細

運用の順序は [GPU_ROUTE.md](GPU_ROUTE.md)。ここには各段階の入力・出力・規則を書く。音声だけで最終採用せず、映像を確認する停止点を持つ。外部の生成AIはこの処理から呼ばない。

## 音声の取得

- 対応する入力：Kick の配信アーカイブ、YouTube の単一動画、Twitch のアーカイブ・クリップ、直接 HTTPS の mp4・m4a・mp3・webm・HLS m3u8。**あなたが権利を持つファイルを置いた場所の URL** を指定する。署名クエリを含む元 URL は非公開の同一性検証にだけ使い、ログには出さない。
- Kick は専用の取得器（音声専用の rendition を優先、無ければ最軽量の HLS を一時取得）。YouTube・Twitch・直接ファイルは yt-dlp。HLS は終了済みの media playlist が必要。ライブとプレイリストの一括取得は受け付けない。
- 投入前に制御VMでメタデータと最低画質のプレイリストだけを読み、終了済みか・配信長が上限内かを確かめる。超えていれば runner も GPU も起動しない。
- runner は音声を16 kHz・モノラル・AAC 48 kbps の m4a にして、長さ・サイズ・SHA-256 を確定してから文字起こしへ渡す。Kick の HTTP 取得は 429・5xx・接続失敗で最大3回まで待って再試行し、403 は1回だけ再試行する。
- MP3・M4A など音声だけの入力は解析まで。`--auto-previews` は付けない。
- 予算：`max_source_seconds`（横用設定で21,600秒＝6時間、最大43,200秒）、取得時の一時転送 `max_transport_bytes`（既定6 GiB、最大16 GiB）、保存音声 `max_input_bytes`（既定1 GiB、最大2 GiB）。

## 話題分け

全編の文字起こし（[ASR.md](ASR.md)）を、発話の間と語彙の変化で区切る（`topics.py`）。`topic_min_seconds` 未満の話題は隣と併合し、`max_seconds` を超える話題は割る。結果は `topics.json`（区間と本文）。意味の一致・完結や面白さを判断した結果ではない。

話題を長く取るときは `config.landscape-long.example.json`（話題120秒以上、切り抜き60〜1,800秒、karaoke 字幕、左上の見出し1.4倍）を使う。話題の分析キーには `topic_min_seconds` と `max_seconds` が入るので、解析と描画には同じ値の設定を渡す。

## 候補映像と未承認の下書き

`--auto-previews` を付けた解析は、同じ run の CPU ジョブで候補の低画質映像・静止画シート（`preview-contact-sheet.jpg`、10%・50%・90%地点）・`review-draft.json`・`review-draft-diagnostics.json` を作る。

- 候補の順位はヒント（`topic_hints.py`）による。本文の文字3-gram の類似度が0.5以上の話題は `near_duplicates` として示し、発話密度の低い方に0.2の減点を一度だけ付ける。音量の突出（前後60秒の中央値との差、上位3窓の平均）は重み0.05で加点する。どれも自動では除外しない。
- 下書きは `settings.clips` 件（3〜5件）。全クリップが `video_reviewed: false`、`confidence: 0.0`、見出しは `要確認：<topic-id>`。単語の途中で切れる候補は除き、足りなくても補充しない（diagnostics の `shortfall`・`skipped`）。
- 静止画シートは動画全体を視聴したことを意味しない。

話題を選び直して候補映像を作り直すときは、話題 ID の一覧を渡す。

```bash
python tools/submit_audio_stage.py --stage previews \
  --previous-run ANALYSIS_RUN_ID --previous-request ANALYSIS_REQUEST_ID \
  --source-ref "$SHA" --review-file /private/topic-shortlist.json   # {"topic_ids": ["topic-001", ...]}
```

### 候補映像の段が失敗したとき

解析だけ成功して候補映像のジョブが失敗した場合、受付票は `status: PARTIAL_FAILED` になり、`analysis_status` と `previews_status` を分けて記録する。解析だけを回収できる。

```bash
python tools/submit_job.py --collect REQUEST_ID --collect-stage analysis --output /private/analysis-RECOVERED
```

## 承認ファイル

下書きを別名にコピーして直し、確認して採用するクリップだけを残す。

```json
{
  "analysis_key": "topics.json の analysis_key",
  "clips": [
    {
      "candidate_id": "topic-001",
      "start": 120.0,
      "end": 240.0,
      "title": "実際に確認した話題名",
      "reason": "話題と映像を確認した根拠",
      "confidence": 0.8,
      "video_reviewed": true,
      "crop_anchor": "auto",
      "caption_edits": [{"at": 12.5, "from": "誤", "to": "正"}]
    }
  ]
}
```

- 時刻は配信全体の秒数で、対応する話題の中に収める。単語の途中で切らない。異なる話題・重ならない区間を、設定の本数（3〜5本）だけ並べる。
- `title` は左上の見出しになる。1.4倍では360pで1行おおむね17字まで（入らなければ60%まで縮め、それでも入らなければ描画を止める）。
- `crop_anchor`（任意、`auto`・`left`・`center`・`right`）は、チャット欄 fill で映像の左右位置を固定する。
- `caption_floor`（任意、0〜0.32）は、その区間の字幕を画面の高さに対してこの割合以上の位置に置く。自動判定で拾えないワイプの対策に使う。
- `caption_edits`（任意）は字幕の誤りを直す。`at` はクリップ内の秒、`from` は直したい文字列、`to` は直した文字列。`at` の前後10秒で `from` を含む最も近い行だけを置き換え、見つからなければ映像を取得する前に止まる。
- 確認していない候補、別の分析キー、変わったプレビュー、重複した話題は拒否する。承認ファイル全体は60,000バイトまで。

## 描画

採用区間だけを最大360pで remux（再符号化なし）して取得し、全編の文字起こしの時刻を区間内へ移して字幕にする。先行取得も同じ画質で、高画質の旧キャッシュは再利用しない。区間ごとの認識はしない。字幕の焼き込みを含む最終描画は360pの再エンコードで、CPUフィルターとNVENCを使う。

- 設定は `--config-file NAME`（既定 `config.landscape.example.json`）。`--source-ref` のコミットにある `config.英小文字・数字・ハイフン.example.json` だけを受け付ける。設定名は依頼 ID に入る。
  - `config.landscape-long-preview.example.json`：長めの話題、360p・NVENC（確認用と納品の基本）
  - `config.landscape-long.example.json`：長めの話題、360p（従来名も同じ画質へ統一）
  - `config.landscape-chat.example.json`：右にチャット欄
- 単一クリップの成果物は、描画後に作業用のファイル（`ranges/`・`audio.m4a`・`previews/`・`transcripts/` など）を除く。結合はクリップと receipt の鍵で行い、文字起こしの本文は使わない。

### チャット欄

`chat_panel: true` で、出力の右に `chat_panel_width`（既定480 px、出力幅の20〜40%、偶数）のチャット欄を置く。チャットは Kick の URL 経路だけで、各クリップの元の時刻のものを読む。

- `chat_panel_fit` の既定 `fill` は、映像を高さいっぱいに拡大して左右を切り、字幕を映像の上（中央下）に重ねる（360pで24 px 程度、1行約18字）。チャット列は160 px。`top` は16:9の映像を左上に置き、下の帯に字幕を出す。`crop` は中央から左右を削り、`letterbox` は縮めて上下に帯を置く。
- 下側に HUD など動かない細部が混んでいるときは、その上まで字幕を持ち上げる（上限は高さの32%）。判定の詳細と限界は [QUALITY_DECISIONS.md](QUALITY_DECISIONS.md) の「字幕の位置」。
- チャット欄の縦型出力は設定検証で拒否する。映像・音声の無音カットは行わず、元配信の時刻を保持する。取得に失敗したクリップは欄を空で描き、`render.json` の `chat.reason` に `chat_unavailable` を記録する。`chat_anonymize: true` で発言者名を匿名化する。

## YouTube の Cookie

- Actions では非公開 worker の Secret `YOUTUBE_COOKIES_B64`（Netscape 形式ファイルを Base64 にした値）を使う。例：`base64 -w0 /private/cookies.txt | gh secret set YOUTUBE_COOKIES_B64 --repo nyu1791-collab/ai-model-workers`。
- 手元の事前確認は `submit_job.py --cookies-file /private/cookies.txt`、runner での取得は `tools/acquire_audio_url.py --cookies-file` で渡せる。YouTube/Google 以外のドメインを含むファイル・壊れた形式・1 MiB 超は拒否する。取得中だけ権限600のコピーを作り、終了時に消す。
- Cookie の内容をコマンド引数・設定 JSON・Release・Artifact に入れない。音声取得と描画時の映像取得にだけ渡し、文字起こしのランタイムには渡さない。
- WARP 経路（`--youtube-audio-transport warp`）と診断（`probe_only`）は [YOUTUBE_TRANSPORT.md](YOUTUBE_TRANSPORT.md)・[YOUTUBE_DIAGNOSTICS.md](YOUTUBE_DIAGNOSTICS.md)。Cookie・PO Token・WARP による成功は保証しない。
