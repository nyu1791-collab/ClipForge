# 縦型ショートと並列制作

ClipForgeの通常制作とは独立した設定で、9:16・15〜60秒を目安にしたショートを作ります。自然な発言の始まりと終わりを優先するため、秒数や本数は目標であり、未達で止めたり機械的に切ったりしません。`config.tiktok.example.json` は720×1280の縦フレーム、ぼかし背景、字幕を使用します。素材は従来どおり360p以下を優先して取得するため、出力サイズが大きくても素材の解像度が上がるわけではありません。

公開ClipForgeのActions、指定Vertex認証、非公開Releaseと既存の安全な再開方式を使用します。短い動画にするために厳格な品質検査や追加の査読モデルを挟みません。音声の文字起こし・字幕時刻は既存のTranscribe経路を使います。

```bash
# 縦型ショートのみ
python tools/produce_formats.py "$SOURCE_URL" --mode tiktok --output "$PRIVATE_OUTPUT"

# 従来の横型のみ
python tools/produce_formats.py "$SOURCE_URL" --mode long --output "$PRIVATE_OUTPUT"

# 横型と縦型を別の制作Runとして並行する
python tools/produce_formats.py "$SOURCE_URL" --mode both --output "$PRIVATE_OUTPUT"

# CapCutで字幕を重ねられる、字幕を焼き込まない縦型映像とSRT
python tools/produce_formats.py "$SOURCE_URL" --mode tiktok-capcut --output "$PRIVATE_OUTPUT"
```

本数を指定するときは `--clips 5`、ソースコードを固定するときは `--source-ref` に40桁のコミットを指定します。`--dry-run` は送信せず、固定したソースと出力ディレクトリを表示します。片方の制作が失敗または監視途中で切れた場合は、`long` または `tiktok` の各ディレクトリについて `python tools/produce_with_gemini.py --resume --output "$PRIVATE_OUTPUT/tiktok"` のように再接続します。既存の制作状態がある場合、この起動コマンドは再投入しません。

両方を並行すると独立したモデル呼び出し・音声処理・GPU描画が発生し、使用量や料金がそれぞれ発生し得ます。同時実行で429やGPU待ちが増える可能性があり、所要時間の短縮は保証されません。各Runで使用量・時間を別々に記録します。TikTokへのアップロードや投稿は自動実行しません。

## CapCutドラフト（実験的）

字幕を焼き込まない `tiktok-capcut` モードはMP4とSRT・ASSを保存します。ユーザーがWindowsに導入した非公式・MITライセンスの `capcut-cli` とNode.jsを指定すれば、次のコマンドで新しいネイティブドラフトを作り、映像とSRTを別トラックへ追加できます。

```powershell
# Node.jsとnpmがあるWindows PCで、専用のフォルダへインストール
npm install --prefix C:\clipforge-tools capcut-cli@0.32.0 --ignore-scripts --no-audit --no-fund
python tools/export_capcut_draft.py C:\my-output\tiktok-capcut\clips\clips\clip-001 `
  --cli-js C:\clipforge-tools\node_modules\capcut-cli\dist\index.js `
  --drafts-dir "$env:LOCALAPPDATA\CapCut\User Data\Projects\com.lveditor.draft"
```

`--capcut-cli-js` と `--capcut-drafts-dir` を `produce_formats.py` に指定すると、縦動画の回収後にドラフトを順次作ります。`--mode both` では横型が進行中でも、縦型の完了後にドラフト作成を始めます。SRTは字幕ごとの編集用で、カラオケ表示・CapCut Pro専用エフェクトの再現は保証しません。映像ファイルは移動・削除せず維持してください。

CapCutを閉じてから実行してください。指定したドラフト保存先のプロジェクト一覧が更新されますが、既存名は上書きしません。CapCut内部のドラフト形式は非公開でバージョン差があり、当ツールは非公式です。CLIでファイルを作成できても、最新版のWindows版CapCut Proで開けることは未確認です。失敗時もClipForgeの完成MP4・字幕は保持し、制作全体を再送せず `export_capcut_draft.py` だけを再実行してください。TikTokへのアップロード・公開は行いません。
