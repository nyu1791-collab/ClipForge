import json

from clipforge.storage import write_json
from tools import compare_asr


def output(root, ref, segments, topics, split=None):
    write_json(root / "request-receipt.json", {"source_ref": ref})
    asr = {"source": "split_asr" if split else "asr"}
    if split:
        asr["split"] = {"silence": split}
    write_json(root / "asr-receipt.json", asr)
    write_json(root / "topics.json", {"topics": topics})
    write_json(root / "transcripts" / ("k" * 64 + ".json"), {"segments": segments})
    return root


def segment(start, text, words=None):
    return {"start": start, "end": start + 2, "text": text, "words": words or []}


def topic(start, end):
    return {"start": start, "end": end}


def test_compare_reports_numbers_only_and_splits_at_the_cut(tmp_path, capsys):
    base = output(
        tmp_path / "base",
        "a" * 40,
        [segment(0, "あいう"), segment(100, "かきく"), segment(200, "さしす")],
        [topic(0, 90), topic(90, 180), topic(180, 260)],
    )
    duplicate = [
        {"start": 151.0, "end": 152.0, "text": "か"},
        {"start": 151.5, "end": 152.5, "text": "か"},
    ]
    other = output(
        tmp_path / "other",
        "a" * 40,
        [
            segment(0, "あいう"),
            segment(100, "かきく"),
            segment(151, "か", duplicate),
            segment(200, "さしせ"),
        ],
        [topic(0, 92), topic(92, 180), topic(180, 260)],
        split=[140.0, 160.0],
    )
    compare_asr.main([str(base), str(other)])
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert "あいう" not in captured.out and captured.err == ""
    assert report["same_code"] is True and report["cut_s"] == 150.0
    assert report["topics"] == [3, 3]
    assert report["first_differing_segment_s"] == 151
    assert report["before_cut"]["text_similarity"] == 1.0
    assert (
        report["before_cut"]["within_2s"] == 1 and report["before_cut"]["max_s"] == 2.0
    )
    assert report["after_cut"]["within_0.5s"] == 1
    assert report["around_cut"]["overlapping_words"] == [0, 1]
    assert report["around_cut"]["topic_spanning_cut"] == [[[90, 180]], [[92, 180]]]


def test_compare_warns_when_the_baseline_came_from_other_code(tmp_path, capsys):
    rows = [segment(0, "あ")], [topic(0, 10), topic(10, 20)]
    compare_asr.main(
        [
            str(output(tmp_path / "b", "a" * 40, *rows)),
            str(output(tmp_path / "o", "b" * 40, *rows)),
        ]
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["same_code"] is False
    assert "different code" in captured.err
