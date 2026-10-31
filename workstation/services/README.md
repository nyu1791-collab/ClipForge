# 任意の監視サービス

Docker Desktop/WSL2を使うWindows PC用。通常の動画環境には導入しない。初期値は1ワーカー・最短60秒・メモリ上限768MB・AI機能無効。定期巡回なので遅延ゼロやミリ秒検知ではない。対象サイトの応答・制限・PCのスリープに依存する。

```powershell
docker compose -f workstation/services/compose.yml --profile monitor up -d
```

PC自身のブラウザで `http://127.0.0.1:5000` を開く。初回はパスワードを設定し、監視したい公開ページとCSS要素を登録する。初期デモ監視があれば不要なものを削除する。Discord/LINE等への通知は本人の通知先を設定した場合だけ有効になる。こちらでは通知送信・商品購入・価格監視を実行していない。

```powershell
docker compose -f workstation/services/compose.yml --profile monitor down
```

データはComposeの `monitor-data` ボリュームに保持する。PC移行時は停止してDocker Desktopのボリューム書き出し/復元機能を使う。`down -v` はデータを削除するので移行手順では使わない。

PostizはPostgreSQL/Redis等を伴う別サービスであり、16GB PCでCapCutと同時常駐させる既定構成には入れない。投稿は作成した `postiz-draft` JSONを、自分のPostizのメディアID・接続先へ明示して取り込む。Skyvernも同様に別サービスとして今回は保留する。
