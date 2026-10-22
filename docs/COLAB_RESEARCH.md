# Colab自動起動の調査記録

調査日: 2026-10-22。一次資料と実際の上流コード・公開パッケージを確認しました。

| 調査対象 | 分かったことと採否 |
| --- | --- |
| [Google公式Colab CLI](https://github.com/googlecolab/google-colab-cli) | T4等の割り当て、コード実行、ファイル転送、終了をサポート。0.7.5の実パッケージを導入し、使用するSDK関数の引数を実際に確認。採用 |
| [公式認証・自動化設計](https://github.com/googlecolab/google-colab-cli/blob/main/docs/04_automation_and_utility.md) | Google end-user ADC/OAuthとVM内部認証は別。既存ブラウザのログインはCLI認証の代わりにならない。必要スコープを採用 |
| [公式Colab API](https://developers.google.com/colab/api/reference/rest) | APIは存在する。現在ベータの許可制であり、契約だけで利用可能とは限らない。公式CLI経由と混同しない |
| [公式runtime作成仕様](https://developers.google.com/colab/api/reference/rest/v1beta/runtimes/create) | GPU指定・割り当て拒否・リクエストIDの扱いを確認。別GPU・別アカウントへの自動fallbackは採用しない |
| [Google公式Colab MCP](https://github.com/googlecolab/colab-mcp) | ローカルエージェントとブラウザのColabを橋渡しする用途。無人Actionsの制御はCLI SDKを採用 |
| [SebastianGilPinzon/colab-mcp](https://github.com/SebastianGilPinzon/colab-mcp) | OAuthとGPU変更ツールを提供する派生版。ただし追加の認証準備は必要。派生コードは取り込まず公式SDKを採用 |
| [anluin/colab-mcp](https://github.com/anluin/colab-mcp) | 初回OAuth、無人実行、実GPU情報取得、分割転送とSHA-256検査の設計を調査。コードをコピーせず、境界の検証と同様の転送原則を実装 |
| [automate-colab-script-service](https://github.com/asgaralipq/automate-colab-script-service) | WebDriverでノートブックを開いて実行する方式。ブラウザ・UI依存を避け、公式CLIを採用 |
| [Colab FAQ](https://research.google.com/colaboratory/faq.html) | 利用枠・切断・GPU提供は可変。契約・残高・実際の割り当てを区別 |
| [GitHub Releases API](https://docs.github.com/en/rest/releases/assets) | 認証付きの大容量転送とキャッシュに利用。512MiB以下のチャンク、SHA-256、許可したCDNへのリダイレクト時の認証ヘッダ除去を実装 |
| [NVIDIA: TuringのNVENC最小幅](https://forums.developer.nvidia.com/t/minimum-width-in-turing-gpus/155566) | H.264の最小幅145px。128pxの検出映像ではT4を誤って利用不可と判定するため、320×240の実エンコードで検出 |

公式CLIリポジトリの調査時コミット: `4025416a03361d2ff34d06a4523ae76ce4c9bbaf`。
実行時はPyPIの `google-colab-cli==0.7.5` と `tools/colab-control-requirements.txt` のハッシュを使います。上流READMEと既定認証方式の説明に差があるため、SDKへ認証済みセッションを明示的に渡し、対話ログインをCIで起動しない構成にしています。

設定と実行手順は [COLAB.md](COLAB.md) を参照してください。
