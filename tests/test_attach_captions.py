import json

import pytest

from tools import attach_captions


def review():
    return {
        "analysis_key": "k",
        "clips": [{"candidate_id": "topic-001", "start": 1.0, "end": 9.0}],
    }


def test_captions_attach_to_the_matching_clip(tmp_path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review()))
    captions = tmp_path / "captions.json"
    row = {"candidate_id": "topic-001", "start": 1.0, "end": 9.0, "captions": "eJw="}
    captions.write_text(json.dumps({"analysis_key": "k", "clips": [row]}))
    attach_captions.main(path, captions)
    assert json.loads(path.read_text())["clips"][0]["captions"] == "eJw="
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "captions",
    [
        {"analysis_key": "other", "clips": []},
        {
            "analysis_key": "k",
            "clips": [
                {"candidate_id": "topic-001", "start": 1.0, "end": 8.0, "captions": "x"}
            ],
        },
        {
            "analysis_key": "k",
            "clips": [
                {"candidate_id": "topic-002", "start": 1.0, "end": 9.0, "captions": "x"}
            ],
        },
    ],
)
def test_mismatched_captions_are_refused(captions):
    with pytest.raises(SystemExit):
        attach_captions.attach(review(), captions)
