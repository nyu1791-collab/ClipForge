---
name: ffmpeg-quality
description: 高品質な動画編集・書き出しのFFmpegレシピ集。縦型の埋め方（ぼかし背景）、字幕の焼き込みとスタイル、声の音量/ノイズ処理、BGMの自動ダッキング、サムネイル選定、無音カット（ジャンプカット）、静止画のKen Burns、シーン検出、x264/NVENCの画質設定、Web再生向けの仕上げを扱う。動画の描画・書き出し・切り抜き・字幕焼き込み・音量調整・エンコード設定を書く/直すときに使う。
---

# FFmpeg 品質レシピ

コードでは各リポジトリの `ffmpeg_recipes.py`（ClipForge は `src/clipforge/media.py`・`hardware.py`）を使い、同じ設定を手書きで増やさない。数値は目安。外れても止めず、再測定・再描画はしない。

## 書き出し（毎回これ）
- `delivery_args(quality, encoder)` = 映像 + AAC 192k/48kHz + `-movflags +faststart`（先頭にmoov＝即再生）。
- 画質プロファイル: `draft`（veryfast/CRF23, NVENC p3/CQ25）・`standard`（medium/CRF20, p5/CQ21）・`high`（slow/CRF18, p7/CQ19）。下書きは draft、納品は standard 以上。
- NVENC は `-preset p5 -tune hq -rc vbr -cq N -b:v 0 -spatial-aq 1 -rc-lookahead 20`。`p1` は文字の縁が崩れるので納品に使わない。
- `-pix_fmt yuv420p -profile:v high` と BT.709 タグ（`-colorspace/-color_primaries/-color_trc bt709`）を必ず付ける。無いとスマホで色が薄く見える。

## 画面
- 縦型に横素材を入れるときは `fit_frame(W,H,"blur")`：1/4サイズでぼかした背景＋lanczos縮小の前景＋軽いunsharp。黒帯は `"black"`。
- 拡大縮小は `flags=lanczos`。寸法は偶数にそろえる（`_even`）。
- 静止画の動きは `ken_burns()`。zoompan は等倍だとガタつくので4倍（draftは2倍）に拡大してから掛ける。
- フェードは `fades(duration)`（映像0.25秒/音声0.15秒）。クリックノイズと白飛びの切り替えを防ぐ。

## 字幕
- ASS が使えるなら `ass=` で焼く（位置・色・縁を完全に制御）。SRT は `subtitles=file.srt:force_style='...'`。force_style の値はカンマを含むので必ず `'...'` で囲む。
- SRT の force_style は 288px 高の基準で拡大される（FontSize 14 ≒ 1280px 高で約62px）。縦型は太字・縁1.5〜2・下余白は画面の15〜20%。
- 日本語は Noto Sans CJK JP を指定。フォントが無い環境では豆腐になるので `fc-list | grep -i cjk` で確認。

## 音声
- 声は `voice_chain()`：`highpass 70Hz → (afftdn) → acompressor 2.5:1 → loudnorm I=-14:TP=-1.5:LRA=11 → aresample 48k → alimiter`。
- loudnorm は内部で192kHzに上げるので、直後に `aresample=48000` を必ず置く。
- ノイズ除去 `afftdn` は環境音が多い素材だけ（音楽や効果音を削るため既定オフ）。
- 1パスで決め、完成後に測り直して再描画しない。
- BGMは `add_music(video, music, out, volume=0.18)`：ループ再生、声をキーにした `sidechaincompress` で話している間だけ下げ、最後1.5秒でフェードアウト、映像はコピー（再エンコードなし）。

## 編集
- 無音カット（auto-editor 方式）：`detect_silences()` → `keep_ranges(margin=0.15)` → `jump_cut_filter()`。字幕は `remap_time()` で同じだけ詰める。
- サムネイル：`thumbnail(video, image)`。15フレームおきに抜き、平均に最も近い（代表的な）1枚をJPEG高画質で保存。
- シーン境界：`scene_changes(path, 0.3)`（`select=gt(scene,0.3)`）。切り出し点やサムネ候補に。
- 速い切り出しは `-ss` を `-i` の前に置く（入力シーク）。時刻は0起点になる。
- 同じ設定で書いたクリップは concat demuxer + `-c copy` で無劣化結合。設定が違うなら再エンコード。

## 動きと構成（解説・ショート共通）
- 静止画の上に別レイヤーを重ねて動かす（3D- `render.layer_graph`）: pop（滑り込み＋フェード）・zoom（ゆっくり寄る）・focus（赤枠の位置へ寄る）・hold（寄ったまま次の行へ）・pan（長いページを流す）・steps（表を1行ずつ、棒グラフを伸ばす、要点を1つずつ）。背景・キャラ・字幕帯は静止画のまま、動かすのはパネルだけ。
- 画面に出す数字・引用は、元記事に同じ数字・同じ文字列があるものだけ（万・億も数値化して照合）。確認できなければ記事ページを出す。
- ショートは冒頭3秒の見出し帯（ASS の BorderStyle=3）と、下端の進捗バー（`drawbox` の `w='iw*t/長さ'`）で離脱を減らす。
- 量産判定を避ける: 形式を日替わりにし、事実と分けた考察を毎回入れ、合成音声であることと出典を説明欄に書く。
- 吹き替え（Translation `dub.py`）: 発話の開始に合わせ、収まらない行は最大1.3倍まで速める。重ねずに後ろへずらし、要確認にする。元音声は `sidechaincompress` で下げ、第2音声として残す。

## 確認（1回だけ・軽く）
```sh
ffprobe -v error -show_entries stream=codec_name,width,height,color_primaries,sample_rate -of compact out.mp4
ffmpeg -v error -ss 1 -i out.mp4 -frames:v 1 frame.png   # 見た目を1枚だけ見る
```
