import json

import pytest

from tools import submit_captions


def write_review(tmp_path, clips):
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"analysis_key": "k", "clips": clips}))
    return path


def test_review_payload_drops_attached_captions(tmp_path):
    clip = {"candidate_id": "topic-001", "start": 1, "end": 2, "video_reviewed": True}
    path = write_review(tmp_path, [{**clip, "captions": "x" * 70000}])
    assert json.loads(submit_captions.review_payload(path))["clips"] == [clip]


@pytest.mark.parametrize("clips", [[], [{"video_reviewed": False}], [None]])
def test_unreviewed_clips_are_refused(tmp_path, clips):
    with pytest.raises(submit_captions.CaptionsError, match="video review"):
        submit_captions.review_payload(write_review(tmp_path, clips))


@pytest.mark.parametrize(
    ("run", "sha"), [("abc", "a" * 40), ("12", "short"), ("1;2", "a" * 40)]
)
def test_invalid_identities_dispatch_nothing(monkeypatch, run, sha):
    monkeypatch.setattr(submit_captions, "gh", lambda *a: pytest.fail("reached GitHub"))
    with pytest.raises(submit_captions.CaptionsError):
        submit_captions.dispatch(run, sha, "{}")


def test_dispatch_returns_the_new_run(monkeypatch):
    calls = []
    listings = iter([[{"id": 5}], [{"id": 7}, {"id": 5}]])

    def gh(*arguments):
        calls.append(arguments)
        if arguments[1] == "-X":
            return ""
        return json.dumps({"workflow_runs": next(listings)})

    monkeypatch.setattr(submit_captions, "gh", gh)
    assert submit_captions.dispatch("12", "a" * 40, '{"clips": []}') == 7
    post = next(c for c in calls if c[1] == "-X")
    assert "inputs[previous_run_id]=12" in post and "ref=main" in post
