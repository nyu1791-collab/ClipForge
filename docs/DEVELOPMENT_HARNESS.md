# OpenCodeを組み込んだ開発の進め方

主担当が計画・編集・検証・採否判断を行い、Opus 5.5は提案だけを返します。OpenCodeも既存の非公開 `ai-model-workers` を通して利用します。会話だけで進捗を覚える代わりに、`tools/dev_harness.py` が作業領域と証跡を保持します。

## 取り入れた機能

- **作業の分離**: 明示したrefから専用ブランチとworktreeを作り、通常checkoutや未コミット作業を上書きしません。既存の作業先や状態ディレクトリは再利用せず拒否します。
- **目標とチェックポイント**: `job.json` に目標・元コミット・作業ブランチ・検査・モデル依頼・採否を保存します。状態は一時ファイルから置き換え、同じタスクへの書き込みをロックします。`status` は処理中でも読めます。
- **実装と検証の反復**: 主担当が編集してcommitし、lint・format・unit coverage 90%以上・lock・wheelを検査します。変更があれば古い検査の再利用を拒否します。`check --resume` は同じソースの成功済み検査だけを再利用します。
- **CIの証跡**: 小容量VMでは `check-ci` を使い、同じSHAに対するQualityのPython 3.11/3.12・videoジョブとBuild packageの成功をGitHubから照合します。別SHA、実行中、失敗、skipped、必要ジョブ欠落は通しません。Qualityの `uv sync --locked` とBuild packageのwheel作成が、ローカルのlock/wheel検査に相当するCI経路です。
- **OpenCode連携**: 検査済みかつcommit済みのソースから、指定したファイルだけを依頼文へ添付します。コミット・ファイルSHA-256・prompt SHA-256・モデル・結果パスを記録し、別コミットの結果を現在の成果として扱いません。
- **失敗と再開**: failed/interrupted/unconfirmedを区別して記録します。モデルの自動再送・fallbackはありません。同じチェックポイントに対して重ねて依頼せず、未確定のrunを先に確認します。
- **採否の共有**: 提案を受け取って主担当が `decide` で採否を記録し、必要な修正を自分で実装します。採用記録だけではファイル変更・commit・push・mergeを行いません。

これはこの会話から使う開発用の道具です。ChatGPT内部のモデルやシステム設定を置き換えたり、アプリの許可判定を無効化するものではありません。

## 1. 目標と作業領域を作る

Linux/Python 3.11以上・Gitが必要です。VMにも標準ライブラリだけで設置できます。

```bash
python3 tools/dev_harness.py start \
  --source /path/to/ClipForge --ref origin/fix/analysis-budget \
  --workspace /path/to/new-worktree --state /path/to/new-task-state \
  --name bounded-inputs --goal 'Gemini送信量の上限を実装し、送信前停止を検証する'
```

状態はworktreeの外へ置きます。`job.json` は作業者自身の非公開ファイルです。主担当がこのworktreeで修正し、対象と差分を確認してcommitします。認証ファイルや動画を追加しません。

## 2. 検査する

十分な開発環境では、依存を `uv sync --locked --extra chat` で導入した後に実行します。unitは外部LLMを呼びません。ログは状態ディレクトリに保存し、失敗した検査以降を止めます。

```bash
python3 tools/dev_harness.py check --state /path/to/task-state
python3 tools/dev_harness.py check --state /path/to/task-state --resume
```

制御VMでは動画・ASRを実行しません。PRのQualityと手動Build packageが同じコミットで成功したrun IDを指定します。CIを勝手に成功扱いにせず、GitHubから検証します。

```bash
python3 tools/dev_harness.py check-ci --state /path/to/task-state \
  --repository nyu1791-collab/ClipForge \
  --quality-run QUALITY_RUN_ID --build-run BUILD_RUN_ID
```

## 3. OpenCodeへ相談する

VMの既存 `ai-models` と認証済み `gh` が必要です。公式OpenCode CLIは非公開runnerで動き、保護されたSecretsを利用します。キーをこのリポジトリやタスクへコピーしません。CLIがインストール済みでも、生成・無料枠の継続・利用枠を保証しません。明示したモデルが使えなければ停止します。

```bash
python3 tools/dev_harness.py review --state /path/to/task-state \
  --model opencode/deepseek-v4.1-flash-free \
  --context src/clipforge/analysis_budget.py \
  --context src/clipforge/selection.py \
  --question '送信上限の抜け道や、失敗時の後処理に不具合がないか確認して'
```

現在のworkerの上限は依頼文全体で16,000文字です。添付が大きければ送る前に拒否します。ファイルを黙って切り詰めたり、repo全体・最新mainを暗黙に送ったりしません。選べるのは明示したcommitの追跡済みソース・テスト・文書です。秘密らしい文字列の簡易検査も行いますが、完全な検出は保証しません。添付内容は作業者が確認します。
大きいファイルは `--context tools/dev_harness.py:120:250` のように行範囲を明示できます。元ファイルと送信した範囲の両方のSHA-256を記録し、部分的な資料であることをモデルに示します。範囲外や逆順は送信前に拒否します。

OpenCode側にはツール実行・編集を依頼せず、提案を受け取ります。元の作業を行う主担当は、通常の編集・Git・テストを利用できます。OpenCodeやOpusの提案を検証せずに自動適用しません。

`review_dispatched` は完成ではありません。`ai-models` のrunと戻ったprovider/model、空でない回答を検査し、成功したときだけ `review_complete` になります。待ち時間超過はworkerが停止した保証ではないため、記録したrunを確認します。モデル結果の診断ログは非公開にします。

## 4. 採用・修正・引き継ぎ

```bash
python3 tools/dev_harness.py status --state /path/to/task-state
python3 tools/dev_harness.py decide --state /path/to/task-state \
  --decision accept --note '指摘1を確認したので修正する。指摘2は既存仕様により不採用。'
```

主担当が修正したら新しいcommitで検査をやり直します。別のcommitについての提案は、検証済みの修正として扱いません。PRへは目的・実際の変更・検証結果・制約を書き、公開Gitへタスクの生ログ・認証・入力動画を入れません。

ClaudeとChatGPTからOpusへ依頼・追加質問・結果取得する共通窓口は、別途VMの `/usr/local/share/ai-access/OPUS_BRIDGE.md` にあります。OpenCode用タスク状態はVMのowner領域に置くため、`dcremote` にownerホーム全体へのアクセスを追加する仕組みではありません。

公式仕様: [OpenCode CLI](https://opencode.ai/docs/cli/)・[agents](https://opencode.ai/docs/agents/)・[permissions](https://opencode.ai/docs/permissions/)。
