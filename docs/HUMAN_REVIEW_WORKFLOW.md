# 話題ごとの確認を挟む制作経路

話題選定・文字起こし・清書の後、字幕を焼き込む前の話題別動画と、全字幕・確認候補を含む `review-package.zip` を非公開Releaseに保存し、利用者に回収する。展開して `review.html` を開くと、各行の時刻からその動画を再生できる。表示時刻は話題の動画の先頭からの目安。

話者不明・別の話者・話者交代、清書で変化した語、数字や一部の断定表現、文字起こしの窓の境界付近を候補として示す。候補が近ければ確認区間をまとめ、各区間は少し手前から再生する。語の比較には清書が実際に採用した文字起こしの元IDを使い、対応を特定できない行や、字幕への対応がない元発言は別に示す。変更がない行から最大10行を参考として再現可能な無作為抽出で示す。話者ラベルは推定で、人の同一性の証明ではない。固有名詞などは利用者が必要に応じて確認・修正する。候補の件数や不確かな表記だけで制作を止めず、候補の生成を人の確認と扱わない。

## 新規の確認用パッケージ

以下は、この変更を含むワークフロー定義が実行ブランチ `main` に反映された後の手順。ワークフロー定義は `main`、実行コードは指定した40桁SHAから取得される。投入前に利用者がURL・本数・設定・コードSHAを指定していることを確認する。勝手に実行しない。

```sh
uv run --no-sync python tools/produce_with_gemini.py "$SOURCE_URL" \
  --source-ref "$CODE_SHA" --clips "$CLIPS" \
  --config-file config.landscape-long-preview.example.json \
  --output "$PRIVATE_OUTPUT"
```

確認用の区間映像を取得・作成するため、映像の取得と軽いプレビューのエンコードが発生する。GPUによる完成版の字幕描画は行わない。完了後の `review-package.zip` は非公開ファイルとして扱い、公開Artifactへ載せない。Run/request ID、編集Release、パッケージのasset ID・サイズ・SHA-256は `production-state.json` に保存する。中断時は `--resume --output "$PRIVATE_OUTPUT"` で同じRunを監視・回収し、再投入しない。失敗したRunも自動で再投入しない。

確認用の映像と完成版の素材は同じURLから別々に取得する。URL先の映像が後から差し替わっていないことまでは、この経路では検証できない。確認用動画には字幕を焼き込まないため、完成版の字幕位置・表示時間・同期は描画後に確認する。

## 利用者の確認後

利用者はこの会話で、動画番号・時刻・修正内容を伝える。字幕番号を併記すると明確。必要な修正を既存の明示パッチにまとめ、修正後の内容について利用者が描画を承認してから描画する。承認を受けていない段階では `--review-approved` を付けない。パッチの仕様は [PRODUCTION_AND_REVISION.md](PRODUCTION_AND_REVISION.md)。

```sh
uv run --no-sync python tools/produce_with_gemini.py "$SOURCE_URL" \
  --source-ref "$CODE_SHA" --clips "$CLIPS" \
  --config-file config.landscape-long-preview.example.json \
  --output "$NEW_PRIVATE_OUTPUT" --render-only --review-approved \
  --review-state "$PRIVATE_OUTPUT/production-state.json" \
  --editorial-release "$EDITORIAL_RELEASE" \
  --analysis-run "$REVIEW_RUN" --analysis-request "$REVIEW_REQUEST" \
  --revision-file "$PRIVATE_PATCH"
```

修正が不要なら `--revision-file` を省く。`--review-approved` は人が承認したという明示入力であり、映像を実際に確認したことの自動証明ではない。パッケージのSHA-256と親編集データを照合し、承認後の選定・字幕データのSHA-256も描画側で照合する。render-onlyでは既存の編集データを使い、新しいモデル要求を行わない。並列描画入口も `--review-approved` と `--review-state` が必要で、確認した元の編集Releaseを対象とする。描画後の字幕表示や納品前の確認は引き続き必要。

共通ルールの矛盾を修正したため、ルールのハッシュが以前と異なる。以前の応答はそのまま保管されるが、新ルールのモデル要求には同じ応答を再利用できない。確認パッケージの生成・修正・描画自体はモデルを追加で呼ばない。

回収に失敗した場合はRunと非公開Releaseを保持して原因を確認する。認証拒否を別経路や別アカウントで回避せず、成功したと報告しない。
