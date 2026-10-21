# 採用したライブラリ

上流リポジトリは依存として利用し、コード一式をコピーしていません。Python依存の解決結果は `uv.lock` に固定しています。

| 用途 | 採用元 | 版 / ライセンス |
|---|---|---|
| CPU文字起こし・単語時刻・VAD | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 1.2.1 / MIT |
| 場面転換 | [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | `scenedetect-headless` 0.7.2 / BSD-3-Clause |
| 映像を含むGemini API呼び出し | [googleapis/python-genai](https://github.com/googleapis/python-genai) | 2.32.0 / Apache-2.0 |
| 日本語文節の改行候補 | [google/budoux](https://github.com/google/budoux) | 0.10.0 / Apache-2.0。内蔵日本語モデルをローカル利用 |
| 日本語の語境界 | [mocobeta/janome](https://github.com/mocobeta/janome) | 0.5.0 / Apache-2.0。内蔵辞書の許諾表記にも従う |
| フォント幅の計測 | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | 13.0.0 / MIT-CMU |
| フォント文字・ファミリー検査 | [fonttools/fonttools](https://github.com/fonttools/fonttools) | 4.67.0 / MIT |
| ASS/SRT編集 | [tkarabela/pysubs2](https://github.com/tkarabela/pysubs2) | 1.8.1 / MIT。Python 3.11対応版 |
| 任意の配信チャット取得 | [xenova/chat-downloader](https://github.com/xenova/chat-downloader) | 0.2.8 / MIT |
| 切り抜き・画面調整・エンコード | [FFmpeg/FFmpeg](https://github.com/FFmpeg/FFmpeg) | runner配布版。主にLGPL、libx264等を有効にした構成はGPL |
| ASS描画 | [libass/libass](https://github.com/libass/libass) | FFmpeg側で使用 / ISC |
| 日本語字幕フォント | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) | runnerの `fonts-noto-cjk` / SIL OFL 1.1 |
| 入出力検査 | [pydantic/pydantic](https://github.com/pydantic/pydantic) | 2.x / MIT |

Whisperモデルの重みは実行時に取得され、各モデルの配布条件にも従います。
ライブラリの利用許諾と、動画・楽曲・配信チャットを利用する権利は別です。

WhisperX、Demucs、顔追跡は初期実装に含めません。音源分離を追加しても著作権上の許可やContent IDの回避を保証するものではありません。
