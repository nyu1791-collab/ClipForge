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
