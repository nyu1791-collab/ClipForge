# Vertex Gemini の接続と使用モデル（現行）

## 接続

- Google 所有者: `n.yu1791@gmail.com`（Colab 用の `n.yu17919191@gmail.com` とは別）。
- プロジェクト: `project-fdadb4dd-cb77-4270-9cd`、ロケーション `global`。
- 認証は GitHub OIDC の短期トークンを使う（鍵なし）。サービスアカウントキー・API キーは作らない。
- Google 側の許可先は `nyu1791-collab/ClipForge` の `main` と `fix/pr18-verified-transfer` だけ。
- AI Studio の無料枠、別の API キー、OpenCode Go/Zen へ自動で切り替えない。

## 初回の設定（本人が一度行う）

1. 指定アカウントで Cloud Shell を開く: https://console.cloud.google.com/home/dashboard?project=project-fdadb4dd-cb77-4270-9cd&cloudshell=true
2. 次を実行する（Vertex API の有効化、専用サービスアカウント、鍵なし OIDC 接続を作る）:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/nyu1791-collab/ClipForge/fix/pr18-verified-transfer/tools/setup_vertex_actions.sh | bash
   ```

3. 最後に表示されるプロジェクト番号を、ClipForge の Actions 変数 `CLIPFORGE_VERTEX_PROJECT_NUMBER` に登録する（秘密情報ではない）: https://github.com/nyu1791-collab/ClipForge/settings/variables/actions/new

## 通常経路で使うモデル

| モデル | 経路 | 用途 | 思考レベル |
|---|---|---|---|
| Gemini 3.8 Flash | Antigravity SDK（同じプロジェクト・global） | 話題選定と清書 | HIGH |
| Gemini 3.5 Transcribe | Vertex 直接の `generateContent` | 採用部分の文字起こし（ja-JP・VERBATIM・単語時刻・話者分離） | — |

- Gemini 3.5 Flash は通常経路では使わない（用語調査は経路から外した）。
- 起動入力と確認方法は [ANTIGRAVITY_ROUTE.md](ANTIGRAVITY_ROUTE.md)。
- 再試行は Gemini の一時的な障害（通信障害・429・5xx）に限る（[QUICK_START.md](QUICK_START.md)）。

## OpenCode（依頼されたときだけ）

- `tools/opencode_harness.py` は利用者が明示的に依頼したときだけ使う。通常の制作には使わない。
- 使うモデルと課金の経路は利用者の指定に従い、別の経路へ自動で切り替えない。

## 請求

- 通常経路のモデルはすべて同じプロジェクト（請求先）で動く。API から特定のプロモーションクレジットは選べず、適用の順序と残額は Cloud Billing 側で決まる（[QUICK_START.md](QUICK_START.md) の請求先の節）。
