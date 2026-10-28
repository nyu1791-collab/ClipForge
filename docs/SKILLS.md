# スキルの使い方と導入方針（Claude Code・Codex 共通）

調査日: 2026-10-28。情報は各 README・公式文書・紹介記事の要約によるもので、コードと LICENSE は未読。導入前に必ず自分で確認する。

## 1. 守ること（必須）

1. スキルは助言であり、強制ではない。必ず守らせたい禁止は `permissions.deny`・hooks で設定する（CLAUDE.md の方針）。
2. 外部スキルは、導入前に次を確認する。確認していないものは入れない。
   - ライセンス（MIT・Apache 2.0 など。非商用（CC BY-NC）は販売の作業に使わない。記載がないものは使わない）
   - `SKILL.md` と同梱スクリプトの全文（外部通信、APIキーの要求、ファイル削除、`curl | sh` がないか）
   - 経路との衝突（Gemini 以外の音声認識、字幕の言い換え、釣り見出し、単語ごとの強調を作るものは入れない）
3. 入れるときは、リポジトリの `.claude/skills/<名前>/` に、`LICENSE`・出典 URL・取得時のコミットを添えて置く（版を固定する）。更新は人が差分を読んで取り込む。
4. スキルが失敗しても、制作・納品の判断は AGENTS.md と `docs/REVIEW_POLICY.md` に従う。スキルが「問題なし」と言っても、「確認済み」とは書かない。
5. 制作の経路（GitHub Actions・Colab・Gemini）にスキルを組み込まない。スキルは、Claude Code・Codex のセッションの中だけで使う。

## 2. 置き場所と呼び出し

| | Claude Code | Codex |
|---|---|---|
| リポジトリ | `.claude/skills/<名前>/SKILL.md` | `.agents/skills/<名前>/SKILL.md`（カレント〜リポジトリ直下まで探す） |
| 個人（全プロジェクト） | `~/.claude/skills/<名前>/` | `~/.agents/skills/<名前>/` |
| 呼び出し | `/名前`。説明文が作業に合えば自動でも読み込む | `$名前` か `/skills`。説明文が合えば自動でも選ぶ |
| 自動を止める | frontmatter に `disable-model-invocation: true` | `agents/openai.yaml` に `allow_implicit_invocation: false` |

- 形式は共通（`SKILL.md` に `name` と `description`）。Codex は symlink を辿るので、正本を `.agents/skills/` に置き、`.claude/skills/` から symlink する方法もある。ただし、パスは文書によって記述が割れるため、使う前に各ツールの現行の文書で確認する。
- Claude Code の制限: 説明文は約 1,536 字で切られる。`SKILL.md` は 500 行以内にする。
- 追加したスキルは、新しいセッションで読み込まれるかを確かめる。
- Codex が自動で読まない場合は、AGENTS.md から対象の `SKILL.md` を直接読む。

## 3. 全プロジェクト共通の必須文（個人設定に貼る）

リポジトリ外の個人設定（Claude Code は `~/.claude/CLAUDE.md`、Codex は `~/.codex/AGENTS.md`。パスは要確認）に貼る。クラウドのセッションは使い捨てなので、ここから自動では反映されない。

```
## スキルの扱い（全プロジェクト共通）
- 外部のスキルは、ライセンスと中身（SKILL.md とスクリプト）を読んでから入れる。読んでいないものは入れない。
- スキルの結果は下書き。「確認済み」と書くのは、実際に確認した範囲だけ。
- 非商用ライセンスのものは、販売・納品の作業に使わない。
- スキルを足すより先に、既存のスキルで足りないかを確認する。入れすぎない（目安は 4〜6 個）。
- 必ず守らせたい禁止は、スキルではなく設定（permissions・hooks）で強制する。
```

## 4. 導入候補（汎用）

「効果」は各 README・紹介記事の記述で、未検証。入れすぎは文脈を圧迫するため、まず 1〜2 個から。

### 4.1 計画・実装・検証
| 候補 | 内容 | ライセンス | 注意 |
|---|---|---|---|
| obra/superpowers | 設計の確認、計画、TDD、原因調査、完了前の検証、サブエージェント実行、レビューを一式で。Claude Code と Codex の両方に導入手順あり | MIT | 手順が多く、呼び出しごとに文脈を使うとの指摘あり。ClipForge は「テストは依頼時だけ」の規則なので、TDD 系は使わない |
| Superpowers の `verification-before-completion` | 「直した」と言う前に、実際に動くかを確かめさせる | MIT（同上） | AGENTS.md の「完了と報告する前に確認」と一致。単体で取り出せるか未確認 |
| Superpowers の `systematic-debugging` / garrytan/investigate | 再現 → 原因の仮説 → 確認の順で調べる | MIT / 未確認 | 失敗の原因調査の型として使える |
| Planning with Files | 長い作業の前に、計画を markdown に書かせる | 未確認 | 作業が長くなる引き継ぎ（コンテキストの切れ目）に向く |
| trailofbits/ask-questions-if-underspecified | 要件が曖昧なとき、実装前に質問する | 未確認 | 「推奨を1つ決めて進める」の方針と衝突しうる。質問が増えるなら入れない |

### 4.2 レビュー・セキュリティ
| 候補 | 内容 | ライセンス | 注意 |
|---|---|---|---|
| anthropics の security-review（組み込み）/ trailofbits/differential-review | 差分のセキュリティ確認 | 未確認 | 組み込みのものがこのセッションにもある。まず組み込みを使う |
| trailofbits/insecure-defaults | ハードコードの秘密情報、既定の認証情報、弱い暗号の検出 | 未確認 | 資格情報を扱う ClipForge に向く |
| openai/gh-fix-ci、openai/gh-address-comments | CI 失敗のログ調査、PR のレビュー指摘への対応 | 未確認 | GitHub は MCP 経由で、`gh` は使えない環境がある。そのままは動かない可能性 |
| openai/yeet | ステージ、コミット、push、PR 作成 | 未確認 | AGENTS.md の「PR は依頼されたときだけ」と衝突しうる |

### 4.3 文書・データ・調査
| 候補 | 内容 | ライセンス | 注意 |
|---|---|---|---|
| anthropics の pdf / docx / pptx / xlsx | 文書・表の作成と編集 | ソース公開のみ（オープンソースではない） | このアカウントには同期済み。見積書・価格表・確認表の作成に使える |
| anthropics/doc-coauthoring | 文書の共同編集 | 未確認 | 長い文書の整理 |
| garrytan/document-release | 変更に合わせて文書を更新する | 未確認 | 文書と実装のずれ（過去に何度も発生）の点検に向く |
| duckdb/query | ファイルやデータベースを SQL で調べる | 未確認 | 字幕 JSON・Run の記録の集計に向く |
| sanjay3290/deep-research | 複数段階の調査 | 未確認 | Gemini の Deep Research を使う（外部サービス）。このアカウントには deep-research スキルが同期済み |

### 4.4 文脈・トークンの節約
| 候補 | 内容 | 注意 |
|---|---|---|
| 公式の運用（`/clear`、`/compact`、出力を絞る） | 無関係な作業の間は文脈を消す。コマンド出力は `tail`・`head`・`grep` で絞る | CLAUDE.md の「読み方と出力」と同じ方針。スキルを足さずに実行できる |
| hanfang/claude-memory-skill | ファイルに記憶を残す | 引き継ぎ文書（docs/）と役割が重なる。入れない |
| Caveman、RTK 系の出力圧縮 | 出力を短くする | 効果の数字は紹介記事のもので未検証。日本語の字幕・報告の品質を落とすおそれがあり、入れない |

### 4.5 動画・字幕（前回までの調査）
| 候補 | 内容 | ライセンス |
|---|---|---|
| scribetoany/skills（subtitle-qc） | 字幕の読み速度・行長・間隔の検査。`check` だけ使う（`fix` は規則と衝突） | MIT |
| ridelink0/video-watch | タイムコード付きの連絡表 | MIT |
| kajisho5/subtitle-skill | 字幕の検証（閾値を指定できる） | MIT |
| max1874/skills（deep-read） | 長い文字起こしを順に読み、読んだ範囲を宣言する | 記載なし（要確認） |
| kajisho5/ffmpeg-skill | FFmpeg の 42 ツール。描画の本線には入れない | MIT と報告（未確認） |

## 5. 入れないもの

- appariciojunior/video-editor-skills（CC BY-NC 4.0。非商用）
- kwindla/skill-caption-clip（Deepgram のクラウド音声認識。Gemini 経路と合わない）
- AgriciDaniel/claude-youtube、Jakeschincariol/youtube-agent-skill・master-skills（フック・煽りの型。釣り見出しの禁止と衝突しやすい）
- Whisper 系の字幕生成（universal-subtitle-translator、premiere-skills）

## 6. 導入の手順

1. 候補のリポジトリを開き、LICENSE と `SKILL.md`・スクリプトを読む。
2. このファイルの「1. 守ること」に照らし、問題がなければ `.claude/skills/<名前>/` に置く（出典・コミット・LICENSE を添える）。
3. このファイルの 4 章の表に、確認した日付・結論（導入した／見送った）と理由を書く。
4. Codex でも使う場合は、`.agents/skills/<名前>` に symlink を作るか、AGENTS.md から直接読む。
5. 新しいセッションで、実際に読み込まれるかを確かめる。

## 7. 未確認

- 各候補のコード、LICENSE、最終更新日（README の要約のみ）。
- Codex のスキル置き場が `.agents/skills` で確定か（紹介記事に `.codex/skills` と書くものもある）。個人設定のパス（`~/.codex/AGENTS.md`）も要確認。
- Codex が AGENTS.md を自動で読む範囲（公式のスキル文書には記載なし）。
- スキルを実際に入れたときの処理時間と文脈の増加量（未計測）。
