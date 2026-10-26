"""One paid Gemini request using fictional Japanese caption text only."""

import json
from pathlib import Path
import tempfile
import time
import urllib.error

from clipforge.audio_understanding import Client, clean_selected_clip
from clipforge.models import SelectedClip

SOURCE = [
    {"start": 0.0, "end": 3.2, "text": "えー、昨日さ、駅前の喫茶てん行ったんだけど"},
    {
        "start": 3.5,
        "end": 7.0,
        "text": "あのー、コヒーを二杯、いや一杯だけ頼んだんだよね",
    },
    {
        "start": 7.3,
        "end": 11.4,
        "text": "でも値段はたぶん千二百円くらいで、まだ確認してないんだよね",
    },
    {"start": 11.8, "end": 13.8, "text": "えー！売り切れだったの？"},
    {
        "start": 14.0,
        "end": 17.9,
        "text": "いや、それはちがうと思う。雨が降らなければ明日行くよ",
    },
    {
        "start": 18.2,
        "end": 22.0,
        "text": "あー、あの、その店、あしたは休業してるって友だちから聞いた",
    },
]


def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="clipforge-synthetic-") as directory:
        client = Client(Path(directory))
        clip = SelectedClip(
            start=0,
            end=23,
            candidate_id="synthetic",
            title="喫茶店の会話",
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
