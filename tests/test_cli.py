import sys

import pytest

from clipforge import cli, pipeline


@pytest.mark.parametrize(
    "args",
    [
        ["run", "source.mp4", "--output", "out"],
        [
            "run",
            "source.mp4",
            "--output",
            "out",
            "--gemini-model",
            "test",
            "--clips",
            "0",
        ],
        ["run", "source.mp4", "--output", "out", "--gemini-model", "bad/model"],
    ],
)
def test_cli_configuration_failures_return_nonzero(monkeypatch, capsys, args):
    monkeypatch.setattr(sys, "argv", ["clipforge", *args])
    assert cli.main() == 1
    assert "failed" in capsys.readouterr().err.lower()


def test_cli_redacts_unknown_exception(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["clipforge", "run", "input", "--output", "out", "--gemini-model", "test"],
    )
    monkeypatch.setattr(
        pipeline,
        "run",
        lambda *a: (_ for _ in ()).throw(RuntimeError("private provider body")),
    )
    assert cli.main() == 1
    assert "private provider body" not in capsys.readouterr().err


def test_cli_interrupt_is_not_success(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["clipforge", "run", "input", "--output", "out", "--gemini-model", "test"],
    )
    monkeypatch.setattr(
        pipeline, "run", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    assert cli.main() == 130
    assert "Interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["analyze", "run"])
def test_offline_cli_needs_no_model_selection(monkeypatch, command):
    args = [command, "input.mp4", "--output", "out"]
    if command == "run":
        args += ["--selection-file", "selection.json"]
    monkeypatch.setattr(sys, "argv", ["clipforge", *args])
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "status": "analyzed" if command == "analyze" else "complete",
            "rendered": [],
        }

    monkeypatch.setattr(pipeline, "run", run)
    assert cli.main() == 0
    assert calls[0][0][2].gemini_model == "manual-no-api"
    assert calls[0][1].get("analyze_only", False) is (command == "analyze")


def test_manual_marker_without_selection_fails_before_pipeline(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "clipforge",
            "run",
            "input.mp4",
            "--output",
            "out",
            "--gemini-model",
            "manual-no-api",
        ],
    )
    monkeypatch.setattr(pipeline, "run", lambda *a: pytest.fail("pipeline started"))
    assert cli.main() == 1
