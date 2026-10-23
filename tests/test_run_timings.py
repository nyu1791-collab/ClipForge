import json

from tools import run_timings


def test_timeline_lists_jobs_slow_steps_and_only_marked_log_lines(monkeypatch):
    run = {
        "conclusion": "success",
        "run_started_at": "2026-10-23T10:35:05Z",
        "updated_at": "2026-10-23T10:45:05Z",
    }
    step = lambda name, start, end, conclusion="success": {  # noqa: E731
        "name": name,
        "started_at": f"2026-10-23T{start}Z",
        "completed_at": f"2026-10-23T{end}Z",
        "conclusion": conclusion,
    }
    jobs = {
        "jobs": [
            {
                "id": 7,
                **step("colab-auto", "10:35:09", "10:45:05"),
                "steps": [
                    step("Acquire audio", "10:36:04", "10:38:05"),
                    step("Checkout", "10:35:12", "10:35:13"),
                    step("Single runtime", "10:38:07", "10:40:00", "skipped"),
                ],
            },
            {
                "id": 8,
                **step("previews", "10:45:05", "10:45:05", "skipped"),
                "steps": [],
            },
        ]
    }
    log = (
        "2026-10-23T10:38:46.75Z Colab runtime allocated for the verified account: T4 HIGH_RAM\n"
        "2026-10-23T10:38:47.00Z ASR progress: 120 of 7400 s\n"
        "2026-10-23T10:44:23.15Z Compute units used by this job: 0.18\n"
    )

    def gh(*arguments):
        path = arguments[1]
        if path.endswith("/jobs"):
            return json.dumps(jobs)
        if path.endswith("/logs"):
            assert path.endswith("/jobs/7/logs")
            return log
        return json.dumps(run)

    monkeypatch.setattr(run_timings, "gh", gh)
    lines = run_timings.timeline(1)
    assert lines[0] == "run 1 success: 10:35:05-10:45:05"
    assert lines[1] == "job colab-auto: 10:35:09-10:45:05 596 s success"
    assert "     121 s  Acquire audio" in lines
    assert not any(
        "Checkout" in line or "Single runtime" in line or "previews" in line
        for line in lines
    )
    assert (
        "  10:38:46  Colab runtime allocated for the verified account: T4 HIGH_RAM"
        in lines
    )
    assert not any("ASR progress" in line for line in lines)
    assert lines[-1] == "  10:44:23  Compute units used by this job: 0.18"
