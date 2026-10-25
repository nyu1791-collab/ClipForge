# 採用したライブラリ

上流リポジトリは依存として利用し、コード一式をコピーしていません。Python依存の解決結果は `uv.lock` に固定しています。

| 用途 | 採用元 | 版 / ライセンス |
|---|---|---|
| 文字起こし（Colab のみ、実行時に導入） | [NVIDIA/NeMo](https://github.com/NVIDIA/NeMo) | `nemo_toolkit[asr]` 3.0.0 / Apache-2.0 |
| 文字起こしのモデル | [nvidia/parakeet-tdt_ctc-0.6b-ja](https://huggingface.co/nvidia/parakeet-tdt_ctc-0.6b-ja) | 実行時に取得 / Hugging Face のモデルカードの条件に従う（未確認） |
| 静かな位置の探索・音声の配列 | [numpy/numpy](https://github.com/numpy/numpy) | 2.x / BSD-3-Clause |
| ASS/SRT編集 | [tkarabela/pysubs2](https://github.com/tkarabela/pysubs2) | 1.8.1 / MIT。Python 3.11対応版 |
| 任意の配信チャット取得 | [xenova/chat-downloader](https://github.com/xenova/chat-downloader) | 0.2.8 / MIT |
| 公開Kick VODのHTTP/HLS取得（media-fetch extra） | [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi) | 0.13.0 / MIT。libcurl等の同梱依存の条件も適用 |
| YouTube・Twitch・直接HTTPSメディア取得 | [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2026.10.21 / Unlicense。default extraの依存を `uv.lock` に固定 |
| YouTube取得のJS実行環境 | [nodejs/node](https://github.com/nodejs/node) | Actionsは22系、Colabの不足時は22.23.4の公式Linux x64配布物をSHA-256確認後に導入 / MITほか同梱条件 |
| runnerでの音声復号・変換・長さ確認 | [PyAV-Org/PyAV](https://github.com/PyAV-Org/PyAV)（`av`） | Python 3.11は18.1.0、3.12以上は19.0.2（`uv.lock`） / BSD-3-Clause。同梱FFmpegの条件も適用 |
| 切り抜き・画面調整・エンコード | [FFmpeg/FFmpeg](https://github.com/FFmpeg/FFmpeg) | runner配布版。主にLGPL、libx264等を有効にした構成はGPL |
| ASS描画 | [libass/libass](https://github.com/libass/libass) | FFmpeg側で使用 / ISC |
| 日本語字幕フォント | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) | runnerの `fonts-noto-cjk` / SIL OFL 1.1 |
| 字幕の改行候補（文節の区切り） | [google/budoux](https://github.com/google/budoux) | 0.10.0 / Apache-2.0 |
| 文末の判定（形態素解析） | [polm/fugashi](https://github.com/polm/fugashi) | 1.5.2 / MIT AND BSD-3-Clause |
| 形態素解析の辞書 | [polm/unidic-lite](https://github.com/polm/unidic-lite) | 1.0.8 / パッケージ表記は MIT。同梱辞書の条件は要確認 |
| Colabの起動・実行・解放 | [googlecolab/google-colab-cli](https://github.com/googlecolab/google-colab-cli) | 0.7.5 / Apache-2.0。制御専用のハッシュ付き依存ロック |
| 入出力検査 | [pydantic/pydantic](https://github.com/pydantic/pydantic) | 2.x / MIT |

モデルの重みは実行時に取得され、モデルの配布条件にも従います。
ライブラリの利用許諾と、動画・楽曲・配信チャットを利用する権利は別です。
