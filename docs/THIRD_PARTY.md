# 採用したライブラリ

上流リポジトリは依存として利用し、コード一式をコピーしていません。Python依存の解決結果は `uv.lock` に固定しています。

| 用途 | 採用元 | 版 / ライセンス |
|---|---|---|
| CPU文字起こし・単語時刻・VAD | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 1.2.1 / MIT |
| 既定の文字起こしモデル | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo)（OpenAI Whisper large-v3-turbo の変換版） | リビジョン `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` 固定 / MIT（Hugging Face の表記） |
| 場面転換 | [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | `scenedetect-headless` 0.7.2 / BSD-3-Clause |
| 映像を含むGemini API呼び出し | [googleapis/python-genai](https://github.com/googleapis/python-genai) | 2.32.0 / Apache-2.0 |
| ASS/SRT編集 | [tkarabela/pysubs2](https://github.com/tkarabela/pysubs2) | 1.8.1 / MIT。Python 3.11対応版 |
| 任意の配信チャット取得 | [xenova/chat-downloader](https://github.com/xenova/chat-downloader) | 0.2.8 / MIT |
| 切り抜き・画面調整・エンコード | [FFmpeg/FFmpeg](https://github.com/FFmpeg/FFmpeg) | runner配布版。主にLGPL、libx264等を有効にした構成はGPL |
| ASS描画 | [libass/libass](https://github.com/libass/libass) | FFmpeg側で使用 / ISC |
| 日本語字幕フォント | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) | runnerの `fonts-noto-cjk` / SIL OFL 1.1 |
| 入出力検査 | [pydantic/pydantic](https://github.com/pydantic/pydantic) | 2.x / MIT |

Whisperモデルの重みは実行時に取得され、各モデルの配布条件にも従います。
ライブラリの利用許諾と、動画・楽曲・配信チャットを利用する権利は別です。

WhisperX、Demucs、顔追跡は初期実装に含めません。音源分離を追加しても著作権上の許可やContent IDの回避を保証するものではありません。

## 品質確認用の合成音声

通常の動画処理とは別に、CIの検証素材をeSpeak NGとOpen JTalkで生成します。
日本語の検証音声には **HTS voice nitech_jp_atr503_m001** を使用します。
Copyright 2003–2012 Nagoya Institute of Technology, Department of Computer Science;
2003–2008 Tokyo Institute of Technology, Interdisciplinary Graduate School of Science and Engineering。
音声モデルは [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)、配布元は
[Open JTalk](https://open-jtalk.sourceforge.net)です。音声モデル自体はこのリポジトリに複製しません。
生成した検証素材の `fixture.json` にもクレジットとライセンスURLを記録します。
