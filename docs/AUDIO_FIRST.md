# 音声から話題を割り振り、候補の映像を判定する

対象は同じ配信から3〜5本の16:9・1920×1080横動画。Shortsは対象外。
順序は音声取得 → 全編ASRを共有 → 話題候補 → 候補映像の確認 → 採用区間だけ高画質取得 → 字幕・描画・品質検査。
音声だけで最終採用せず、映像を確認する停止点を持つ。外部LLM・Gemini・OpenCodeの推論は、この処理から呼び出さない。

## 段階と私有ファイル

| stage | 入力・出力 | 実施しない処理 |
| --- | --- | --- |
| `audio`（既定） | 公開Kick VOD → `audio.m4a`、取得receipt | ASR、話題選定、候補映像、高画質、描画 |
| `topics` | 保存音声 → 共有ASR、`topics.json`、ASR再利用receipt | 全編映像取得・場面検出、動画生成 |
| `previews` | 音声話題候補 → 上位候補の低解像度映像、`previews.json` | 高画質取得、最終採用の自動承認 |
| `render` | 分析と対応するレビュー済み区間JSON → 採用区間の高画質映像・横動画3〜5本 | 全編ASRや全編高画質の再取得、別モデルfallback |

`topics.json`は、発話の間と語彙の変化で割り振った区間とASR本文を含む。
意味的な話題の一致・完結や面白さを承認した結果ではない。プレビュー候補の優先順位は発話秒数による。
長い話題は現行上限180秒で区切る。20分等への尺拡張は行っていない。
音声専用renditionがあれば優先する。なければ最軽量HLS（映像を含む）を一時受信するため、通信量が音声のみになる保証はない。
音声を確定した後、一時的なTSとその取得キャッシュを削除し、以後は音声ファイルとASRを再利用する。

`workspace`全体、音声、ASR、区間本文、プレビュー、詳細品質記録は非公開に置く。公開Gitや公開Artifactsへ追加しない。
同じworkspaceの排他ロックで重複起動を防ぐ。異なる配信URLのworkspace共用を拒否する。
取得済みファイルはSHA-256に加え、元URL・開始・終了・音声専用フラグを照合し、不一致や片方だけ残ったキャッシュで先へ進まない。

## 音声取得と非公開workerでの話題分割

```bash
uv sync --locked --extra media-fetch --extra chat
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --workspace inputs/private-vod --stage audio
```

音声取得の完了後、重いASRは既存の非公開 `clipforge.yml` workerに渡せる。workerのworkflow変更は不要。
`--ref`はレビュー済みの完全なコミットSHAを指定する。

```bash
uv run --no-sync python tools/submit_job.py \
  --input inputs/private-vod/audio.m4a \
  --audio-analysis-url 'https://kick.com/CHANNEL/videos/VOD_ID' \
  --ref REVIEWED_COMMIT_SHA --config config.landscape.example.json
```

この分岐はGemini設定・キーの登録を要求せず、選定APIへ到達しない。既存workflowの環境変数にGemini Secretが渡される場合も、その値を読み取らない。
一時音声は非公開releaseへ送信し、既存workerが処理後に削除する。失敗時も同一依頼を自動再投入しない。
転送で401等が返った場合は、登録済みGitHub接続の認証を利用者側で復旧してから進める。APIキーをチャットへ貼らず、別アカウント・別経路で拒否を回避しない。
`STAGING_FAILED`はASRやworkflowが起動したことを意味しない。一時releaseや同じ依頼IDを確認してから再開を判断する。
依頼IDから `tools/submit_job.py --status ID`、成功後に `--collect ID --output PRIVATE_NEW_DIRECTORY` で回収する。
音声・ASR・話題・manifestのハッシュと依頼元SHAを検査する。ASRはlarge-v3-turbo・CPU/int8・2スレッド。
キャッシュが一致しない音声や別配信のASRを流用しない。長いASRは完了まで時間を要し、失敗後の途中チェックポイント再開はこの経路では未実装。

十分なCPUのある環境なら、同じworkspaceに対して `--stage topics` でも実行できる。
制御VMで `topics`・`previews`・`render`を実行しない。回収済み音声と `transcripts/` を対応付けて、十分なCPUのある私有環境へ渡す。
workerから回収したファイルをworkspaceへ統合する際は、`audio.m4a`のSHAと `source_url` の一致を先に確認する。

## 転送元GitHub認証の診断

`tools/submit_job.py`の`dispatch()`は、`gh release create`で一時release作成とファイル添付を行い、完了後にだけworkflowをdispatchする。
`gh()`は継承した環境でCLIを呼ぶ。この環境では`GH_TOKEN`が設定され、`GITHUB_TOKEN`は設定されていない。値は表示・取得しない。

2026-10-22の再診断では、同じ接続で以下を確認した。

| 操作 | 接続先 | 実測 |
| --- | --- | --- |
| 利用者・非公開repo・workflow読取 | `api.github.com` | 200 |
| 一時release作成 | `POST https://api.github.com/repos/nyu1791-collab/ai-model-workers/releases` | 201 |
| 同じreleaseへのファイル添付 | `POST https://uploads.github.com/repos/nyu1791-collab/ai-model-workers/releases/RELEASE_ID/assets` | 401 Bad credentials |

失敗は添付のアップロード段階であり、workflowのdispatchには未到達。
RESTの読み書きが成功しているため、全体のトークン失効や権限不足だけが原因とは断定できない。
アップロード先ホストへ有効な認証が届いているか、既存接続側の対応も確認する必要がある。
`gh auth status`は成功し、`GH_TOKEN`の使用を報告した。Git設定に流用可能なcredential helperは見つからず、Git push成功だけで添付APIの認証成功とは判断できない。
プロキシを外したり、注入済みの認証を無断で置き換えたりして解決しない。

復旧する場所は**転送元のこの作業環境のGitHub接続・シークレット設定**。workerリポジトリのActions SecretsにPATを追加するだけでは、転送元のCLIへは渡らない。
PATを使う場合は、利用者が安全な設定画面で登録する。Fine-grained PATは指定の非公開repoを対象とし、Contents: Read and write（release/添付）とActions: Read and write（dispatch/回収）を設定する。
classic PATは通常`repo`が必要で、workflowファイル自体を変更する場合は別途`workflow`の条件も確認する。
トークン値はチャット・コマンド履歴・文書へ貼らない。

診断で作成した私有releaseには添付がなく、ASRは未起動。同じ依頼IDの記録を`UPLOAD_FAILED_AUTH`として保全した。
復旧後は、そのreleaseの添付状態と同じIDのworkflowの有無を確認してから、不足した添付とdispatchだけを進める。
一から新しい依頼を作る、音声を再取得する、ローカルASRへ切り替える操作は行わない。

## 候補映像の判定と採用

`--stage previews`で音声の話題候補（既定最大8件）の区間だけ低解像度映像を取得する。
映像の内容・発話との一致・画面の見やすさ・話題の始まりと結びを確認し、採用区間JSONを私有領域に作る。
`video_reviewed: true`はその確認を行った担当者の申告。プログラム自身が人の視聴を証明する値ではない。

```json
{
  "analysis_key": "topics.jsonのanalysis_key",
  "clips": [
    {
      "candidate_id": "topic-001",
      "start": 120.0,
      "end": 240.0,
      "title": "実際に確認した話題名",
      "reason": "話題と映像を確認した根拠",
      "confidence": 0.8,
      "video_reviewed": true
    }
  ]
}
```

上は1区間の書式例。実行時は要求本数と一致する、異なる話題の非重複3〜5区間が必要。
時刻は配信全体の秒数で、対応する音声話題内に収める。
確認していない候補、別ASRの分析キー、改変されたプレビュー、重複した話題を拒否する。

```bash
uv run --no-sync python tools/kick_audio_first.py \
  --url 'https://kick.com/CHANNEL/videos/VOD_ID' --workspace inputs/private-vod \
  --stage render --review-file inputs/reviewed-topics.json
```

採用区間だけ最大1080pを取得し、全編ASRの単語時刻を区間内へ移す。区間ごとのASRは実行しない。
単語時刻のない発話の途中切断は推測で補わず拒否する。
48kHz、全復号、音量・真ピーク、ASS/SRT一致等は既存strictゲートを使う。
strict通過は警告0件や人の読みやすさの承認を意味しない。
読み速度は目標4字/秒・警告4字/秒のまま。4/7/10字超過数、実フォントでの行幅等を記録する。
共有用 `delivery/e2e-summary.json` は数値・件数とコード/設定/フォント等の識別情報のみ。詳細警告中の発話例や素材パスをコピーしない。
