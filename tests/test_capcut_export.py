import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path("tools") / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = load("export_capcut_draft")
SRT = "1\n00:00:00,200 --> 00:00:01,200\nこんにちは\n\n2\n00:00:02,000 --> 00:00:03,000\n二行目\nです\n"
TIMELINES = ("draft_content.json", "draft_info.json", "template-2.tmp")


def make_clip(root, name="clip-001", width=360, height=640, srt=SRT, receipt=None):
    clip = root / name
    clip.mkdir(parents=True)
    body = {
        "captions": {"caption_mode": "editable"},
        "media": {"duration": 4.0, "width": width, "height": height},
    }
    body.update(receipt or {})
    (clip / "render.json").write_text(json.dumps(body), encoding="utf-8")
    (clip / "video.mp4").write_bytes(b"video")
    data = srt if isinstance(srt, bytes) else srt.encode("utf-8")
    (clip / "captions.srt").write_bytes(data)
    return clip


def make_package(root):
    package = root / "capcut-cli"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text('{"name":"capcut-cli","version":"0.32.0"}')
    cli = package / "dist" / "index.js"
    cli.write_text("")
    return cli


class FakeCli:
    """Writes the same files capcut-cli 0.32.0 writes, for the commands export uses."""

    def __init__(self, drafts, fail=(), drop_cue=False, register=True):
        self.drafts, self.fail, self.drop_cue, self.register = (
            drafts,
            fail,
            drop_cue,
            register,
        )
        self.calls, self.project = [], None

    def read(self):
        return json.loads((self.project / TIMELINES[0]).read_text(encoding="utf-8"))

    def write(self, document):
        for name in TIMELINES:
            (self.project / name).write_text(
                json.dumps(document, ensure_ascii=False), encoding="utf-8"
            )

    def add_text(self, document, track_name, text, y=0):
        track = next((t for t in document["tracks"] if t["name"] == track_name), None)
        if track is None:
            track = {"type": "text", "name": track_name, "segments": []}
            document["tracks"].append(track)
        material = str(uuid.uuid4())
        content = {
            "styles": [{"range": [0, len(text)], "size": 15, "bold": False}],
            "text": text,
        }
        document["materials"]["texts"].append(
            {
                "id": material,
                "type": "text",
                "font_size": 15,
                "text_color": "#FFFFFF",
                "content": json.dumps(content),
            }
        )
        track["segments"].append(
            {"material_id": material, "clip": {"transform": {"x": 0, "y": y}}}
        )

    def __call__(self, node, cli, *args):
        args = tuple(map(str, args))
        self.calls.append(args)
        if args[0] in self.fail:
            raise RuntimeError(f"capcut-cli {args[0]} failed: injected")
        if args[0] == "init":
            self.project = self.drafts / args[1]
            self.project.mkdir()
            size = {"9:16": (1080, 1920), "16:9": (1920, 1080)}[
                args[args.index("--ratio") + 1]
            ]
            self.write(
                {
                    "canvas_config": {"width": size[0], "height": size[1]},
                    "tracks": [],
                    "materials": {"videos": [], "texts": []},
                }
            )
            (self.project / "draft_meta_info.json").write_text(
                '{"draft_materials":[{"type":0,"value":[]}]}'
            )
            entries = (
                [{"draft_name": args[1], "draft_fold_path": str(self.project)}]
                if self.register
                else []
            )
            (self.drafts / "root_meta_info.json").write_text(
                json.dumps({"all_draft_store": entries})
            )
        elif args[0] == "add-video":
            assets = self.project / "assets" / "video"
            assets.mkdir(parents=True)
            target = assets / "video.mp4"
            shutil.copy2(args[2], target)
            document = self.read()
            document["materials"]["videos"].append({"path": str(target)})
            document["tracks"].append(
                {"type": "video", "name": "video", "segments": [{"clip": {}}]}
            )
            self.write(document)
            (self.project / "draft_meta_info.json").write_text(
                json.dumps(
                    {
                        "draft_materials": [
                            {"type": 0, "value": [{"file_Path": str(target)}]}
                        ]
                    }
                )
            )
        elif args[0] == "import-srt":
            cues = exporter.parse_srt(Path(args[2]).read_text(encoding="utf-8-sig"))
            document = self.read()
            for _, text in cues[: -1 if self.drop_cue else None]:
                self.add_text(document, "subtitle", text)
            self.write(document)
        elif args[0] == "add-text":
            document = self.read()
            self.add_text(
                document, "heading", args[4], float(args[args.index("--y") + 1])
            )
            self.write(document)
        return {"ok": True}


@pytest.fixture
def env(tmp_path, monkeypatch):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    fake = FakeCli(drafts)
    monkeypatch.setattr(exporter.shutil, "which", lambda _: "/node")
    monkeypatch.setattr(exporter, "command", fake)
    return {
        "tmp": tmp_path,
        "drafts": drafts,
        "fake": fake,
        "cli": make_package(tmp_path),
        "mp": monkeypatch,
    }


def run(env, clip, *extra, name="test-draft"):
    argv = [
        "export",
        str(clip),
        "--drafts-dir",
        str(env["drafts"]),
        "--cli-js",
        str(env["cli"]),
    ]
    argv += ["--name", name] if name else []
    env["mp"].setattr(sys, "argv", argv + list(extra))
    return exporter.main()


def output(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def tracks(fake):
    return {t["name"]: t for t in fake.read()["tracks"]}


def test_uses_new_project_and_editable_tracks(env, capsys):
    clip = make_clip(env["tmp"])
    assert run(env, clip) == 0
    calls = env["fake"].calls
    assert [call[0] for call in calls] == ["init", "add-video", "import-srt"]
    assert "--ratio" in calls[0] and "9:16" in calls[0]
    assert str((clip / "video.mp4").resolve()) in calls[1]
    assert str((clip / "captions.srt").resolve()) in calls[2]
    result = output(capsys)
    assert result["status"] == "created_unverified_in_capcut"
    for key in (
        "draft",
        "cli_version",
        "video",
        "subtitle_track",
        "media_must_remain_in_place",
    ):
        assert key in result
    assert result["heading_state"] == "absent"
    assert (
        result["verification"]["caption_segments"]
        == result["verification"]["srt_cues"]
        == 2
    )


def test_landscape_uses_16_9_and_other_shapes_are_rejected(env, capsys):
    assert run(env, make_clip(env["tmp"], "wide", 640, 360), name="wide-draft") == 0
    assert "16:9" in env["fake"].calls[0]
    assert output(capsys)["verification"]["canvas"] == [1920, 1080]
    with pytest.raises(SystemExit):
        run(env, make_clip(env["tmp"], "square", 500, 500), name="square-draft")
    assert "only 9:16 and 16:9" in capsys.readouterr().err
    assert not (env["drafts"] / "square-draft").exists()


def test_heading_sources_in_order(env, capsys):
    selection = {"selection": {"title": "選択の見出し", "candidate_id": "topic-1"}}
    clip = make_clip(env["tmp"], "a", receipt=selection)
    assert run(env, clip, "--heading", "指定 の\n見出し", name="h1") == 0
    assert output(capsys)["heading"] == "指定 の 見出し"
    assert run(env, clip, name="h2") == 0
    assert output(capsys)["heading"] == "選択の見出し"
    segment = tracks(env["fake"])["heading"]["segments"][0]
    assert segment["clip"]["transform"]["y"] > 0
    assert run(env, clip, "--no-heading", name="h3") == 0
    assert output(capsys)["heading"] is None


def test_heading_from_neighbouring_final_selection(env, capsys):
    root = env["tmp"] / "output" / "clips"
    (root / "audio-review").mkdir(parents=True)
    (root / "audio-review" / "final-selection.json").write_text(
        json.dumps({"clips": [{"title": "一本目"}, {"title": "二本目"}]}),
        encoding="utf-8",
    )
    clip = make_clip(root / "clips", "clip-002")
    assert run(env, clip) == 0
    assert output(capsys)["heading"] == "二本目"


def test_heading_failure_never_fails_the_draft(env, capsys):
    env["fake"].fail = ("add-text",)
    assert run(env, make_clip(env["tmp"]), "--heading", "見出し") == 0
    result = output(capsys)
    assert result["heading_state"] == "failed"
    assert result["status"] == "created_unverified_in_capcut"


def test_style_changes_only_verified_keys_and_keeps_backups(env, capsys):
    assert run(env, make_clip(env["tmp"]), "--heading", "見出し") == 0
    assert output(capsys)["style"] == "applied_unverified_in_capcut"
    fake = env["fake"]
    document = fake.read()
    backup = json.loads(
        (fake.project / "draft_content.json.prestyle.bak").read_text(encoding="utf-8")
    )
    assert backup["materials"]["texts"][0].get("border_color") is None
    assert (
        tracks(fake)["subtitle"]["segments"][0]["clip"]["transform"]["y"]
        == exporter.CAPTION_Y["9:16"]
    )
    allowed = {
        "id",
        "type",
        "font_size",
        "text_color",
        "content",
        "border_color",
        "border_width",
        "border_alpha",
    }
    for material in document["materials"]["texts"]:
        assert set(material) == allowed
        content = json.loads(material["content"])
        assert content["styles"][0]["bold"] is True
        assert set(content) == {"styles", "text"}
    mirrors = {(fake.project / name).read_text(encoding="utf-8") for name in TIMELINES}
    assert len(mirrors) == 1
    assert all((fake.project / f"{name}.prestyle.bak").is_file() for name in TIMELINES)


def test_no_style_leaves_plain_draft(env, capsys):
    assert run(env, make_clip(env["tmp"]), "--no-style") == 0
    assert output(capsys)["style"] == "skipped"
    assert not (env["fake"].project / "draft_content.json.prestyle.bak").exists()
    assert tracks(env["fake"])["subtitle"]["segments"][0]["clip"]["transform"]["y"] == 0


def test_style_failure_leaves_the_unstyled_draft(env, capsys):
    def explode(document, aspect):
        document["tracks"] = []
        raise ValueError("unexpected format")

    env["mp"].setattr(exporter, "style_document", explode)
    assert run(env, make_clip(env["tmp"])) == 0
    result = output(capsys)
    assert result["style"] == "failed"
    assert result["verification"]["caption_segments"] == 2
    assert tracks(env["fake"])["subtitle"]["segments"][0]["clip"]["transform"]["y"] == 0


def test_style_write_failure_restores_all_files(env, capsys, tmp_path):
    fake = env["fake"]
    fake.project = tmp_path / "project"
    fake.project.mkdir()
    fake.drafts = tmp_path
    fake("n", "c", "init", "project2", "--ratio", "9:16")
    fake("n", "c", "import-srt", "x", str(make_clip(tmp_path, "b") / "captions.srt"))
    before = fake.read()
    real_replace = os.replace
    seen = []

    def flaky(source, target):
        seen.append(target)
        if len(seen) == 2:
            raise OSError("disk full")
        real_replace(source, target)

    env["mp"].setattr(exporter.os, "replace", flaky)
    with pytest.raises(OSError):
        exporter.apply_style(fake.project, "9:16")
    assert fake.read() == before
    assert not list(fake.project.glob("*.style-tmp"))


@pytest.mark.parametrize(
    "encoded",
    [SRT.encode("utf-8-sig"), SRT.encode("cp932"), SRT.encode("utf-16")],
    ids=["bom", "shift-jis", "utf-16"],
)
def test_srt_encodings_import_the_same_text(env, capsys, encoded):
    assert run(env, make_clip(env["tmp"], srt=encoded)) == 0
    assert output(capsys)["verification"]["caption_segments"] == 2
    texts = [
        json.loads(m["content"])["text"]
        for m in env["fake"].read()["materials"]["texts"]
    ]
    assert texts == ["こんにちは", "二行目\nです"]


def test_empty_cues_are_skipped_with_a_warning(env, capsys):
    srt = "1\n00:00:00,000 --> 00:00:01,000\n\n2\n00:00:01,000 --> 00:00:02,000\n　\n\n3\n00:00:02,000 --> 00:00:03,000\n本文\n"
    assert run(env, make_clip(env["tmp"], srt=srt)) == 0
    result = output(capsys)
    assert result["verification"]["srt_cues"] == 1
    assert any("empty" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    "srt,message",
    [
        ("", "no cues"),
        ("1\n00:00:00,000 --> 00:00:01,000\n\n", "no cues"),
        ("subtitle", "bad timestamp"),
        ("1\n00:00:02,000 --> 00:00:01,000\nx\n", "not after start"),
        (b"\xff\xff\x81", "not UTF-8"),
    ],
)
def test_bad_srt_fails_before_any_draft_exists(env, capsys, srt, message):
    with pytest.raises(SystemExit) as stop:
        run(env, make_clip(env["tmp"], srt=srt))
    assert stop.value.code != 0
    assert message in capsys.readouterr().err
    assert env["fake"].calls == []


def test_caption_count_mismatch_is_reported_and_partial_kept(env, capsys):
    env["fake"].drop_cue = True
    assert run(env, make_clip(env["tmp"])) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip().splitlines()[-1])
    assert result["status"] == "incomplete" and result["failed_step"] == "verify"
    assert "2 cues" in result["error"]
    assert env["fake"].project.is_dir()
    assert "Retry" in captured.err


def test_unlisted_draft_is_reported(env, capsys):
    env["fake"].register = False
    assert run(env, make_clip(env["tmp"])) == 1
    assert "root_meta_info.json does not list" in output(capsys)["error"]


def test_missing_video_copy_is_reported(env, capsys):
    real = env["fake"]

    def lose_video(node, cli, *args):
        result = real(node, cli, *args)
        if args[0] == "import-srt":
            shutil.rmtree(real.project / "assets")
        return result

    env["mp"].setattr(exporter, "command", lose_video)
    assert run(env, make_clip(env["tmp"])) == 1
    assert "does not exist" in output(capsys)["error"]


def test_cli_failure_gives_incomplete_status_and_retry(env, capsys):
    env["fake"].fail = ("import-srt",)
    clip = make_clip(env["tmp"])
    assert run(env, clip) == 1
    result = output(capsys)
    assert result["status"] == "incomplete" and result["failed_step"] == "import-srt"
    assert result["draft"].endswith("test-draft")
    assert "--name" not in result["retry"] and str(clip) in result["retry"]


def test_never_overwrites_a_folder_or_listed_name(env, capsys):
    clip = make_clip(env["tmp"])
    (env["drafts"] / "taken").mkdir()
    (env["drafts"] / "taken" / "keep.txt").write_text("keep")
    with pytest.raises(SystemExit):
        run(env, clip, name="taken")
    assert "already exists" in capsys.readouterr().err
    assert (env["drafts"] / "taken" / "keep.txt").read_text() == "keep"
    (env["drafts"] / "root_meta_info.json").write_text(
        json.dumps({"all_draft_store": [{"draft_name": "listed"}]})
    )
    with pytest.raises(SystemExit):
        run(env, clip, name="listed")
    assert "already lists" in capsys.readouterr().err
    assert env["fake"].calls == []


def test_unreadable_root_meta_stops_before_writing(env, capsys):
    (env["drafts"] / "root_meta_info.json").write_text("{broken")
    with pytest.raises(SystemExit):
        run(env, make_clip(env["tmp"]))
    assert "unreadable" in capsys.readouterr().err
    assert env["fake"].calls == []


def test_running_capcut_stops_managed_store_only(env, capsys):
    env["mp"].setattr(exporter, "capcut_running", lambda: True)
    managed = env["drafts"] / "com.lveditor.draft"
    managed.mkdir()
    env["fake"].drafts = managed
    clip = make_clip(env["tmp"])
    original = env["drafts"]
    env["drafts"] = managed
    with pytest.raises(SystemExit):
        run(env, clip)
    assert "CapCut is running" in capsys.readouterr().err
    assert env["fake"].calls == []
    env["drafts"] = original
    env["fake"].drafts = original
    assert run(env, clip) == 0


def test_editor_refusal_from_the_cli_is_explained(tmp_path, monkeypatch):
    def refuse(*_, **__):
        raise subprocess.CalledProcessError(
            1, "node", stderr="refused [editor-open]: CapCut.exe is running."
        )

    monkeypatch.setattr(exporter.subprocess, "run", refuse)
    with pytest.raises(RuntimeError, match="CapCut is running"):
        exporter.command("node", "cli", "add-video")


def test_missing_prerequisites_have_clear_errors(env, capsys):
    clip = make_clip(env["tmp"])
    env["mp"].setattr(exporter.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit):
        run(env, clip)
    assert "Node.js is required" in capsys.readouterr().err
    env["mp"].setattr(exporter.shutil, "which", lambda _: "/node")
    missing = dict(env, drafts=env["tmp"] / "absent")
    with pytest.raises(SystemExit):
        run(missing, clip)
    assert "does not exist" in capsys.readouterr().err
    burned = make_clip(
        env["tmp"], "burned", receipt={"captions": {"caption_mode": "plain"}}
    )
    with pytest.raises(SystemExit):
        run(env, burned)
    assert "burned-in captions" in capsys.readouterr().err


def test_default_name_is_safe_for_a_japanese_clip_folder(env, capsys):
    clip = make_clip(env["tmp"], "切り抜き-001")
    assert run(env, clip, name=None) == 0
    assert Path(output(capsys)["draft"]).name.startswith("ClipForge-001-")


REAL_CLI = Path(
    os.environ.get(
        "CAPCUT_CLI_JS",
        Path(os.environ.get("LOCALAPPDATA", "absent"))
        / "yu-lab-tools"
        / "capcut-cli"
        / "node_modules"
        / "capcut-cli"
        / "dist"
        / "index.js",
    )
)


@pytest.mark.skipif(
    not REAL_CLI.is_file() or not shutil.which("node"),
    reason="capcut-cli is not installed",
)
@pytest.mark.parametrize("width,height,ratio", [(360, 640, "9:16"), (640, 360, "16:9")])
def test_real_cli_builds_the_expected_tracks_from_a_japanese_path(
    tmp_path, monkeypatch, capsys, width, height, ratio
):
    clip = make_clip(tmp_path / "日本語の フォルダ", width=width, height=height)
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    source = clip / "video.mp4"
    argv = [
        "export",
        str(clip),
        "--drafts-dir",
        str(drafts),
        "--cli-js",
        str(REAL_CLI),
        "--name",
        "real-draft",
    ]
    monkeypatch.setattr(sys, "argv", argv + ["--heading", "本物の見出し"])
    assert exporter.main() == 0
    result = output(capsys)
    project = drafts / "real-draft"
    document = json.loads((project / "draft_content.json").read_text(encoding="utf-8"))
    assert document["canvas_config"]["ratio"] == ratio
    assert [(t["type"], t["name"], len(t["segments"])) for t in document["tracks"]] == [
        ("video", "video", 1),
        ("text", "subtitle", 2),
        ("text", "heading", 1),
    ]
    assert result["heading_state"] == "added" and result["style"].startswith("applied")
    assert Path(result["draft_video"]).parent == project.resolve() / "assets" / "video"
    source.unlink()  # The draft holds its own copy, so the source clip is not needed afterwards.
    assert Path(result["draft_video"]).is_file()
    with pytest.raises(SystemExit):
        monkeypatch.setattr(sys, "argv", argv)
        exporter.main()


# tools/produce_formats.py


@pytest.fixture
def producer(tmp_path, monkeypatch):
    module = load("produce_formats")

    class Finished:
        def wait(self):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", lambda command: Finished())
    monkeypatch.setattr(module.shutil, "which", lambda _: "/node")
    return module


def produce(module, monkeypatch, tmp_path, *extra):
    argv = [
        "produce",
        "https://example.test/v",
        "--output",
        str(tmp_path / "out"),
        "--mode",
        "tiktok-capcut",
    ]
    argv += [
        "--source-ref",
        "a" * 40,
        "--capcut-drafts-dir",
        str(tmp_path),
        "--capcut-cli-js",
        str(tmp_path / "c.js"),
    ]
    monkeypatch.setattr(sys, "argv", argv + list(extra))
    return module.main()


def collected(tmp_path, caption_mode="editable"):
    clip = tmp_path / "out" / "tiktok-capcut" / "clips" / "clips" / "clip-001"
    clip.mkdir(parents=True)
    (clip / "render.json").write_text(
        json.dumps({"key": "k", "captions": {"caption_mode": caption_mode}})
    )
    return clip


def test_capcut_failure_keeps_production_success_and_prints_retry(
    producer, tmp_path, monkeypatch, capsys
):
    clip = collected(tmp_path)
    failed = subprocess.CompletedProcess(
        [], 1, stdout='{"status": "incomplete"}\n', stderr="boom\n"
    )
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: failed)
    assert produce(producer, monkeypatch, tmp_path) == producer.CAPCUT_RETRY_EXIT
    captured = capsys.readouterr()
    statuses = json.loads(captured.out.strip().splitlines()[-1])["statuses"]
    assert statuses["tiktok-capcut"]["production"] == 0
    assert "制作の成功" not in captured.err and "動画の制作は成功" in captured.err
    retry = [line for line in captured.err.splitlines() if line.startswith("再実行: ")]
    assert (
        len(retry) == 1
        and "export_capcut_draft.py" in retry[0]
        and str(clip) in retry[0]
    )


def test_capcut_crash_does_not_hide_production(producer, tmp_path, monkeypatch, capsys):
    collected(tmp_path)

    def crash(*_, **__):
        raise OSError("gone")

    monkeypatch.setattr(producer.subprocess, "run", crash)
    assert produce(producer, monkeypatch, tmp_path) == producer.CAPCUT_RETRY_EXIT
    assert "OSError" in capsys.readouterr().err


def test_plain_clip_says_captions_are_burned(producer, tmp_path, monkeypatch, capsys):
    collected(tmp_path, "plain")

    def unused(*_, **__):
        raise AssertionError("export must not run")

    monkeypatch.setattr(producer.subprocess, "run", unused)
    assert produce(producer, monkeypatch, tmp_path) == producer.CAPCUT_RETRY_EXIT
    assert "焼き込み済み" in capsys.readouterr().err


def test_capcut_success_returns_zero(producer, tmp_path, monkeypatch, capsys):
    collected(tmp_path)
    done = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"draft": str(tmp_path / "d")}) + "\n", stderr=""
    )
    monkeypatch.setattr(producer.subprocess, "run", lambda *a, **k: done)
    assert produce(producer, monkeypatch, tmp_path) == 0


def test_capcut_config_selection(producer, tmp_path, monkeypatch, capsys):
    default = producer.commands("u", tmp_path, "tiktok-capcut", "a" * 40)[0][1]
    assert "config.tiktok-capcut.example.json" in default
    chosen = producer.commands(
        "u",
        tmp_path,
        "tiktok-capcut",
        "a" * 40,
        capcut_config="config.tiktok-capcut-360p.example.json",
    )
    assert "config.tiktok-capcut-360p.example.json" in chosen[0][1]
    other = producer.commands(
        "u",
        tmp_path,
        "tiktok",
        "a" * 40,
        capcut_config="config.tiktok-capcut-360p.example.json",
    )
    assert "config.tiktok.example.json" in other[0][1]
    monkeypatch.setattr(producer, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit):
        produce(producer, monkeypatch, tmp_path, "--capcut-360p", "--dry-run")
    assert "does not exist" in capsys.readouterr().err
    (tmp_path / "config.tiktok-capcut-360p.example.json").write_text("{}")
    assert produce(producer, monkeypatch, tmp_path, "--capcut-360p", "--dry-run") == 0
    with pytest.raises(SystemExit):
        produce(
            producer,
            monkeypatch,
            tmp_path,
            "--config",
            "config.tiktok.example.json",
            "--dry-run",
        )
    assert "--config must name" in capsys.readouterr().err
    argv = [
        "produce",
        "u",
        "--output",
        str(tmp_path / "x"),
        "--mode",
        "tiktok",
        "--capcut-360p",
        "--dry-run",
    ]
    monkeypatch.setattr(sys, "argv", argv + ["--source-ref", "a" * 40])
    with pytest.raises(SystemExit):
        producer.main()
    assert "only to --mode tiktok-capcut" in capsys.readouterr().err
