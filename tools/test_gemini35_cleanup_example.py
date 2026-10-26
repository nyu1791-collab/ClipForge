"""One paid Gemini request using fictional Japanese caption text only."""

import json
from pathlib import Path
import tempfile
import time
import urllib.error

from clipforge.audio_understanding import Client, clean_selected_clip
from clipforge.models import SelectedClip

SOURCE = [
    {
        "start": 0.0,
        "end": 5.5,
        "text": "えー、あの、昨日の朝さ、駅まえの図書館に行こうとしたんだけど、ちょっと変なことがあったんだよね。"
    },
    {
        "start": 6.0,
        "end": 11.5,
        "text": "まず家を出たのが九時、いや九時半だったかな。時計をちゃんと見てなかったから、そこははっきり覚えてない。"
    },
    {
        "start": 12.0,
        "end": 17.5,
        "text": "あー、それで、雨がふりそうだったから、青い傘を持って行ったんだけど、途中でぽつぽつ降ってきちゃって。"
    },
    {
        "start": 18.0,
        "end": 23.5,
        "text": "バス停についたら、次のバスまであと十五分って書いてあってさ。十分じゃなくて十五分ね。"
    },
    {
        "start": 24.0,
        "end": 29.5,
        "text": "だから歩こうかなって思ったんだけど、えっと、荷物が重かったから、結局そのまま待つことにした。"
    },
    {
        "start": 30.0,
        "end": 35.5,
        "text": "そしたら近くのおばあさんが、あのー、このバスは図書かんに行きますかって聞いてきたの。"
    },
    {
        "start": 36.0,
        "end": 41.5,
        "text": "僕はたぶん行くと思いますって答えたけど、実際に乗ったことはなかったから、運転手さんに確認した方がいいですよって言った。"
    },
    {
        "start": 42.0,
        "end": 47.5,
        "text": "で、バスが来て、運転手さんに聞いたら、えー！その便は図書館の前を通らないって言うんだよ。"
    },
    {
        "start": 48.0,
        "end": 53.5,
        "text": "同じ番号でも、午前中の一部の便は別の道を通るらしくて、それはぜんぜん知らなかった。"
    },
    {
        "start": 54.0,
        "end": 59.5,
        "text": "おばあさんは急いでなかったみたいで、次の便を待ちますって言ってた。僕も一緒に待つことにした。"
    },
    {
        "start": 60.0,
        "end": 65.5,
        "text": "それから二十分くらい、いや、正確には測ってないから二十分前後だと思うけど、やっと次のバスが来た。"
    },
    {
        "start": 66.0,
        "end": 71.5,
        "text": "料金は大人二百四十円で、おばあさんの分を僕が払ったわけじゃないよ。それぞれ自分で払った。"
    },
    {
        "start": 72.0,
        "end": 77.5,
        "text": "図書館に着いたら、入り口に、今日は空調の点検のため二階は使えません、って紙が貼ってあった。"
    },
    {
        "start": 78.0,
        "end": 83.5,
        "text": "僕が探してた本は一階の棚にあるはずだったから、まあ大丈夫だろうと思って中に入ったんだけど。"
    },
    {
        "start": 84.0,
        "end": 89.5,
        "text": "えーと、タイトルが「星の地図と小さな舟」っていう架空の小説で、作者はたしか水野ゆきさん、だったと思う。"
    },
    {
        "start": 90.0,
        "end": 95.5,
        "text": "あ、でも作者の名前はうろ覚えだから、違うかもしれない。そこは後で確認しないといけない。"
    },
    {
        "start": 96.0,
        "end": 101.5,
        "text": "棚を見ても見つからなくて、司書さんに聞いたら、先週返却されたけど、まだ整理中かもしれませんって。"
    },
    {
        "start": 102.0,
        "end": 107.5,
        "text": "つまり、貸し出し中だとは言ってなかったんだよね。見つかるかどうかは、その時点ではわからなかった。"
    },
    {
        "start": 108.0,
        "end": 113.5,
        "text": "十分くらい待ったら、司書さんが奥から持ってきてくれて、あー、これこれ、って思わず声が出ちゃった。"
    },
    {
        "start": 114.0,
        "end": 119.5,
        "text": "本の表紙には小さい船と、青い星が三つ描いてあった。四つじゃなくて三つだったと思う。"
    },
    {
        "start": 120.0,
        "end": 125.5,
        "text": "それで借りようとしたら、図書カードを家に忘れてたことに気づいてさ。いやー、ほんとにまいった。"
    },
    {
        "start": 126.0,
        "end": 131.5,
        "text": "でも司書さんが、本人確認ができれば今日は仮の手続きができますよって教えてくれた。"
    },
    {
        "start": 132.0,
        "end": 137.5,
        "text": "免許証は持ってたから、名前と住所を確認してもらって、無事に一冊だけ借りることができた。"
    },
    {
        "start": 138.0,
        "end": 143.5,
        "text": "友だちに頼まれたもう一冊は、タイトルを正確に覚えてなかったので、探さずに帰ることにした。"
    },
    {
        "start": 144.0,
        "end": 149.5,
        "text": "帰り道に駅前の喫茶てんに寄って、コヒーを二杯、いや一杯だけ頼んだ。二杯頼もうとしてやめたんだよ。"
    },
    {
        "start": 150.0,
        "end": 155.5,
        "text": "値段はたぶん六百八十円くらい。レシートをなくしたから、ぴったりその金額だとは言い切れない。"
    },
    {
        "start": 156.0,
        "end": 161.5,
        "text": "店員さんが、来週の月曜日は休みだと話してたけど、火曜日も休みだとは言ってなかった。"
    },
    {
        "start": 162.0,
        "end": 167.5,
        "text": "もし火曜日に雨が降らなければ、借りた本を持ってまた行こうかな。ただし、仕事が早く終わった場合だけね。"
    },
    {
        "start": 168.0,
        "end": 173.5,
        "text": "あの、その、本が面白いかどうかはまだ読んでないからわからない。でも表紙はすごく好きだった。"
    },
    {
        "start": 174.0,
        "end": 179.5,
        "text": "えー、そんなわけで、朝はちょっとばたばたしたけど、最終的には本を借りられてよかったな、という話でした。"
    }
]


def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="clipforge-synthetic-") as directory:
        client = Client(Path(directory))
        clip = SelectedClip(
            start=0,
            end=180,
            candidate_id="synthetic",
            title="図書館へ行った日の話",
            reason="架空の整文例",
            confidence=1.0,
        )
        try:
            displayed = clean_selected_clip(client, {"segments": SOURCE}, 1, clip, [])
        except Exception as error:
            report = {
                "status": "failed",
                "error_type": type(error).__name__,
                "http_status": error.code
                if isinstance(error, urllib.error.HTTPError)
                else None,
                "wall_seconds": round(time.monotonic() - started, 3),
                "request_count": client.record["new_generation_requests"],
            }
            print(
                "SYNTHETIC_RESULT=" + json.dumps(report, ensure_ascii=False), flush=True
            )
            raise SystemExit(1) from None
        row = client.record["requests"][0]
        response = json.loads((Path(directory) / (row["phase"] + ".json")).read_text())
        report = {
            "status": "complete",
            "model": row["model"],
            "thinking_level": row["thinking_level"],
            "model_seconds": round(row["elapsed_seconds"], 3),
            "wall_seconds": round(time.monotonic() - started, 3),
            "request_count": client.record["new_generation_requests"],
            "usage": row.get("usage", {}),
            "input_segments": SOURCE,
            "model_response": response,
            "display_segments": displayed["segments"],
        }
        print("SYNTHETIC_RESULT=" + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
