---
name: subtitle-qc
description: ClipForge の字幕ファイル（SRT）の読み速度・1行の長さ・行数・表示時間・字幕の間隔を検査する。納品前の見直しで「字幕を検査して」「読み速度を確認して」と頼まれたときに使う。報告だけで、字幕は変更しない。
---

# 字幕の検査（check のみ）

`scripts/subtitle_qc.py` は標準ライブラリだけの Python スクリプト。入力を上書きしない。ClipForge では **`check` だけ**を使う。`fix` と `apply` は、字幕を無音まで延ばす・短い字幕を結合する・言い換え案を出すため、規則（無音で字幕を消す、言い換えない）と合わない。

## 実行

```bash
python3 .claude/skills/subtitle-qc/scripts/subtitle_qc.py check <captions.srt> --cps 5 --max-chars 18 --max-lines 2 --min-duration 0.8 --worst 5
```

- 既定の閾値は日本語で 9 字/秒・16 字。ClipForge の目安は 5 字/秒・18 字・2 行・表示 0.8 秒以上なので、上のように指定する。
- 機械処理には `--json`（`over_cps`、`short`、`line_length`、`tight_gap`、`hard_problems`、`cue_reports`）。
- 5 字/秒は目安で、超えても制作は止めない。警告の目安は 7 字/秒（`--cps 7`）。

## 報告のしかた

1. 表の数（超過・短すぎ・行長・間隔）と、悪い字幕の上位を引用する。数字を自分で計算し直さない。
2. 「確認済み」とは書かない。検査したのは、読み速度・行長・行数・間隔だけで、語の正しさは見ていない。
3. `hard_problems`（重なり、終了が開始より前、順序の逆転）があれば最初に報告する。

## 注意

- 文字数には全角スペースと「？」「！」も含まれる。字幕の区切りに全角スペースを使う ClipForge では、実際の読みやすさより多めに出る。
- 語の確認（REVIEW_POLICY の「語を変えた行」）は、このスキルでは行えない。
