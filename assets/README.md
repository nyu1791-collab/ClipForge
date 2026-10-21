# 編集アセット

`fonts/`、`se/`、`bgm/`、`overlays/`をGit管理できます。利用許諾とライセンス文を一緒に保存してください。
このリポジトリの`demo-soft.wav`、`demo-hook.wav`、`demo-frame.png`は
`tools/create_demo_assets.py`が生成する独自の簡易素材で、CC0-1.0として提供します。
既存楽曲・商用音源・配信者ロゴの転載ではありません。実運用では配信者に合わせて差し替えてください。

フォントのバイナリは同梱していません。runnerではfonts-noto-cjkをインストールします。
外部フォントを追加する場合はSIL OFL等の同梱条件を確認し、ライセンス文を残してください。
個別アセット32MiBまで、動画・キャッシュ・生成物は`workspace/`へ置きます。
外部取り込みは`tools/sync_assets.py`でSHA-256・配置先・licenseを指定して実行します。
