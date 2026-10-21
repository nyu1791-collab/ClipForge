# 採用したライブラリ

上流リポジトリは依存として利用し、コード一式をコピーしていません。Python依存の解決結果は `uv.lock` に固定しています。

| 用途 | 採用元 | 版 / ライセンス |
|---|---|---|
| CPU文字起こし・単語時刻・VAD | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 1.2.1 / MIT |
| 場面転換 | [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | `scenedetect-headless` 0.7.2 / BSD-3-Clause |
| 映像を含むGemini API呼び出し | [googleapis/python-genai](https://github.com/googleapis/python-genai) | 2.32.0 / Apache-2.0 |
| ASS/SRT編集 | [tkarabela/pysubs2](https://github.com/tkarabela/pysubs2) | 1.8.1 / MIT。Python 3.11対応版 |
| 任意の配信チャット取得 | [xenova/chat-downloader](https://github.com/xenova/chat-downloader) | 0.2.8 / MIT |
| 切り抜き・画面調整・エンコード | [FFmpeg/FFmpeg](https://github.com/FFmpeg/FFmpeg) | runner配布版。主にLGPL、libx264等を有効にした構成はGPL |
| ASS描画 | [libass/libass](https://github.com/libass/libass) | FFmpeg側で使用 / ISC |
| 日本語字幕フォント | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) | runnerの `fonts-noto-cjk` / SIL OFL 1.1 |
| 入出力検査 | [pydantic/pydantic](https://github.com/pydantic/pydantic) | 2.x / MIT |
| 日本語の文節境界 | [google/budoux](https://github.com/google/budoux) | 0.10.0 / Apache-2.0。パッケージ内の日本語モデルをローカル使用 |
| 字幕の実フォント幅計測・確認用画像 | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | `uv.lock` で固定 / MIT-CMU |

## 上流から取り入れたコードと設計（0.3.0）

- [Cutawan `render.ts` の blur-fit](https://github.com/JeremySNR/cutawan/blob/main/src/main/pipeline/render.ts#L287) のフィルター構成を `layout.py` に翻案。確認したファイルのGit blob SHAは `ea46e550bc8352cc76d5873648f827349fd7423e`。Pythonへ移植し、背景の低解像度処理、スレッド制限、検証済みの矩形、元動画時刻のパンチインに変更。[MIT許諾・著作権表示](../third_party/Cutawan-LICENSE)を保持しwheelにも同梱します。
- Cutawanの[独立パネル合成](https://github.com/JeremySNR/cutawan/blob/main/src/main/pipeline/layoutFilters.ts)、[未達事項](https://github.com/JeremySNR/cutawan/blob/main/docs/adaptive-clipping-status.md)を参照。顔と画面内容を別々の元領域から同一デコードで合成し、音声は1トラックだけ出力します。アプリ全体・認証・自動投稿は取り込みません。
- [VideoLingoの意味分割](https://github.com/Huanshere/VideoLingo/blob/main/core/_3_2_split_meaning.py)、[日本語文字量判定](https://github.com/Huanshere/VideoLingo/blob/main/core/_5_split_sub.py)を設計参考にしました。コードの転載や追加LLM呼び出しは行わず、BudouXの文節境界とPillowの実幅計測で実装。翻訳・吹き替え・Qwen・WhisperX依存は追加しません。
- [ClippyMeの静止画角方針](https://github.com/fralapo/clippyme/blob/main/docs/architecture/reframe.md)を設計参考にしました。コードの転載・YOLO依存の追加は行わず、指定矩形を固定し、指定区間でだけ拡大します。

上流のREADMEやスコアを、視聴維持率・日本語精度・商用納品品質の実証とは扱いません。

Whisperモデルの重みは実行時に取得され、各モデルの配布条件にも従います。
ライブラリの利用許諾と、動画・楽曲・配信チャットを利用する権利は別です。

WhisperX、Demucs、顔追跡は初期実装に含めません。音源分離を追加しても著作権上の許可やContent IDの回避を保証するものではありません。
