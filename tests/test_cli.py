import sys

import pytest

from clipforge import cli, media, pipeline


def test_cut_uses_stream_copy_without_loading_pipeline(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "clipforge",
            "cut",
            "input.mp4",
            "--start",
            "2",
            "--end",
            "5",
            "--output",
            "out",
        ],
    )
    monkeypatch.setitem(sys.modules, "clipforge.pipeline", None)
    calls = []

    def cut(*args):
        calls.append(args)
        return {"status": "complete", "mode": "stream_copy", "frame_accurate": False}

    monkeypatch.setattr(media, "cut", cut)
    assert cli.main() == 0
    assert calls[0][2:] == (2, 5)
    assert '"frame_accurate": false' in capsys.readouterr().out


@pytest.mark.parametrize(
    "option", ["--config", "--transcript", "--aspect", "--video-encoder", "--resume"]
)
def test_cut_rejects_removed_render_options(monkeypatch, option):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "clipforge",
            "cut",
            "input.mp4",
            "--start",
            "2",
            "--end",
            "5",
            "--output",
            "out",
            option,
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_copy_is_not_a_separate_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["clipforge", "copy"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


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
