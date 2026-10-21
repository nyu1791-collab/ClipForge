# モデルを追加しない開発ハーネス

OpenCodeの作業分離・チェックポイント・検査とフィードバックの方式を取り入れ、主担当のChatGPT／Claudeが使う道具です。DeepSeekを含め、モデルの選択・呼び出し・自動再送機能はありません。ChatGPT内部の実行基盤をOpenCodeへ交換する機能でもありません。

Opus 5.5への提案依頼は、ユーザーが認めた範囲で既存の `opus-bridge` から別に行います。ハーネスは受け取った提案を保存し、主担当が根拠を確認して修正します。

## 作業全体に取り入れた機能

- 明示したrefから専用ブランチ・worktreeを作り、通常checkoutと未コミット作業を保持する。
- 目標・元コミット・作業ブランチ・検査・提案・採否を `job.json` に保存する。
- 状態を一時ファイルから置き換え、並行書き込みをロックする。`status` は作業中にも読める。
- 主担当が編集・commitし、lint・format・unit coverage 90%以上・lock・wheelを検査する。
- ソースのコミット・差分・未追跡ファイルの内容を指紋化し、変化したら古い検査結果を再利用しない。
- `check --resume` は同じソースの成功済み検査だけを再利用し、失敗箇所以降をやり直す。
- 制御VMでは `check-ci` で同じSHAのCI成功を照合し、重い動画・ASRはGitHub runnerへ任せる。
- 手動で受け取った提案をソースSHAと結び付けて保存し、採否を記録する。記録だけでコード変更・push・mergeはしない。

## 1. 目標と作業領域を作る

Linux/Python 3.11以上・Gitが必要です。標準ライブラリだけで起動できます。

```bash
python3 tools/dev_harness.py start \
  --source /path/to/ClipForge --ref origin/fix/analysis-budget \
  --workspace /path/to/new-worktree --state /path/to/new-task-state \
  --name bounded-inputs --goal 'Gemini送信量の上限を実装し、送信前停止を検証する'
```

状態はworktreeの外へ置きます。既存の作業先や状態ディレクトリを上書きしません。主担当がこのworktreeで修正し、差分を確認してcommitします。認証ファイルや動画を追加しません。

## 2. 検査する

十分な開発環境で `uv sync --locked --extra chat` により依存を導入した後、実行します。unitは外部LLMを呼びません。検査ログは状態ディレクトリに保存し、失敗した検査以降を止めます。

```bash
python3 tools/dev_harness.py check --state /path/to/task-state
python3 tools/dev_harness.py check --state /path/to/task-state --resume
```

制御VMでは動画・ASRを実行しません。QualityとBuild packageが同じcommitで成功したrun IDを指定します。

```bash
python3 tools/dev_harness.py check-ci --state /path/to/task-state \
  --repository nyu1791-collab/ClipForge \
  --quality-run QUALITY_RUN_ID --build-run BUILD_RUN_ID
```

GitHubからSHA・workflow名・状態・必要ジョブを検証します。別SHA、実行中、失敗、skipped、ジョブ欠落は拒否します。Qualityの `uv sync --locked` とBuild packageのwheel作成を、lock/wheelのCI経路として用います。CLIのインストールや、モデルの応答をCI成功と混同しません。

## 3. 受け取った提案を取り込む

提案をローカルのテキストファイルへ保存してから、その提案が対象とするcommitを明示します。ハーネスからモデルへ送信する処理はありません。

```bash
python3 tools/dev_harness.py proposal --state /path/to/task-state \
  --source-commit FULL_COMMIT_SHA --author 'Opus 5.5 / owner supplied' \
  --file /path/to/proposal.md
```

ソースが検査済み・commit済みで、そのSHAが一致する場合だけ受け取ります。提案はUTF-8・200KB以下で、本文のSHA-256と非公開のコピーを保存します。作者名は作業者の申告であり、モデル由来を自動検証した証拠にはしません（`origin_verified=false`）。秘密・認証情報は取り込みません。

## 4. 採用して主担当が修正する

```bash
python3 tools/dev_harness.py status --state /path/to/task-state
python3 tools/dev_harness.py decide --state /path/to/task-state \
  --decision accept --note '指摘1を確認して修正する。指摘2は既存仕様により不採用。'
```

主担当が修正したら新しいcommitで検査をやり直します。モデルの提案や採用記録を、実装・検証完了として扱いません。PRへは目的・実際の変更・検証結果・制約を書き、生ログ・認証・入力動画は公開Gitへ入れません。

ClaudeとChatGPTからOpusへ明示的に依頼する窓口は、VMの `/usr/local/share/ai-access/OPUS_BRIDGE.md` を参照してください。ハーネスの検査・再開・提案取り込みにその接続は不要です。

URL処理に限ってユーザーが明示的に追加したDeepSeek補助エージェントは、[URL受付](URL_AGENT.md)に記載しています。`dev_harness.py` 自体のモデル呼び出し機能は追加していません。
