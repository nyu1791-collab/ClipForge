# 査読用の抜粋（2026-10-26、HEAD の行番号）

このファイルの中身だけで判断してください。ほかのファイルは開かず、ツールも使わないでください。

音声査読Release名の検査は、tools/colab_actions.py（192, 400行）と tools/colab_remote_job.py（372行）の3か所だけです。

## src/clipforge/worker_audio.py 326-345
```python
  326:             elif mode == "render" and review_file is not None:
  327:                 saved_review = json.loads(review_file.read_text())
  328:                 if saved_review.get("selection_basis") in {"saved_video_analysis", "saved_asr_audio_review"}:
  329:                     if saved_review.get("source_url") != url:
  330:                         raise PipelineError("Saved video analysis belongs to another source")
  331:                     # Keep ASR and its clock; use the already selected video topics.
  332:                     # Review-only keys are not selection fields; audio_first.render reads them from the file.
  333:                     selected = landscape.validate_selection(
  334:                         {
  335:                             "clips": [
  336:                                 {
  337:                                     k: v
  338:                                     for k, v in row.items()
  339:                                     if k
  340:                                     not in (
  341:                                         "video_reviewed",
  342:                                         "crop_anchor",
  343:                                         "caption_floor",
  344:                                         "caption_edits",
  345:                                     )
```

## src/clipforge/models.py 18-52
```python
   18: class Record(BaseModel):
   19:     model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
   20: 
   21: 
   22: class Span(Record):
   23:     start: float = Field(ge=0)
   24:     end: float = Field(gt=0)
   25: 
   26:     @model_validator(mode="after")
   27:     def ordered(self):
   28:         if self.end <= self.start:
   29:             raise ValueError("start must be before end")
   30:         return self
   31: 
   32: 
   33: class Word(Span):
   34:     text: str = Field(min_length=1, max_length=4000)
   35: 
   36: 
   37: class Segment(Span):
   38:     text: str = Field(max_length=16000)
   39:     words: list[Word] = Field(default_factory=list)
   40: 
   41: 
   42: class Transcript(Record):
   43:     language: str
   44:     segments: list[Segment]
   45: 
   46: 
   47: class SelectedClip(Span):
   48:     candidate_id: str
   49:     title: str
   50:     reason: str
   51:     confidence: float = Field(ge=0, le=1)
   52: 
```

## src/clipforge/landscape.py 65-77
```python
   65: def validate_selection(value, settings, duration):
   66:     clips = [SelectedClip.model_validate(row) for row in value["clips"]]
   67:     if not 5 <= len(clips) <= 10 or len(clips) != settings.clips:
   68:         raise PipelineError(
   69:             "landscape delivery needs exactly the requested 5 to 10 clips"
   70:         )
   71:     validate_picks(clips, duration, settings)
   72:     if any(not clip.title.strip() for clip in clips):
   73:         raise PipelineError("each landscape clip needs a topic title")
   74:     ordered = sorted(clips, key=lambda clip: clip.start)
   75:     if any(left.end > right.start for left, right in zip(ordered, ordered[1:])):
   76:         raise PipelineError("landscape selections must not overlap")
   77:     return clips
```

## src/clipforge/audio_first.py 334-352
```python
  334: def edited_transcript(transcript, clip_start, edits):
  335:     """Replace the caption text each edit names near its clip time (seconds).
  336: 
  337:     The nearest segment within ten seconds that holds the text is changed, and the new
  338:     text shares the time of the words it replaces. An edit naming no such text is refused,
  339:     before any video is fetched.
  340:     """
  341:     rows = [segment.model_dump() for segment in transcript.segments]
  342:     for edit in edits:
  343:         before, after, at = edit["from"], edit["to"], float(edit["at"])
  344:         lines = [
  345:             row
  346:             for row in rows
  347:             if before in row["text"] and abs(row["start"] - clip_start - at) <= 10
  348:         ]
  349:         if not lines:
  350:             raise PipelineError(f"no caption near {at:.1f} s holds {before!r}")
  351:         row = min(lines, key=lambda r: abs(r["start"] - clip_start - at))
  352:         words, offsets, position = row["words"], [], 0
```

## src/clipforge/audio_first.py 386-420
```python
  386: def reviewed_clips(value, analysis, preview, settings):
  387:     if (
  388:         value.get("analysis_key") != analysis["analysis_key"]
  389:         or preview.get("analysis_key") != analysis["analysis_key"]
  390:     ):
  391:         raise PipelineError("video review belongs to a different audio analysis")
  392:     crop_anchors(value)
  393:     caption_floors(value)
  394:     caption_edits(value)
  395:     cleaned = {
  396:         "clips": [
  397:             {
  398:                 k: v
  399:                 for k, v in row.items()
  400:                 if k
  401:                 not in (
  402:                     "video_reviewed",
  403:                     "crop_anchor",
  404:                     "caption_floor",
  405:                     "caption_edits",
  406:                 )
  407:             }
  408:             for row in value["clips"]
  409:         ]
  410:     }
  411:     clips = landscape.validate_selection(cleaned, settings, analysis["duration"])
  412:     available = {row["id"]: row for row in analysis["topics"]}
  413:     seen_previews = {row["topic_id"] for row in preview["previews"]}
  414:     for clip in clips:
  415:         topic = available.get(clip.candidate_id)
  416:         if (
  417:             topic is None
  418:             or (
  419:                 preview.get("mode") != "transcript"
  420:                 and clip.candidate_id not in seen_previews
```

## src/clipforge/audio_first.py 524-532
```python
  524:     local_transcripts = [
  525:         shifted_transcript(
  526:             edited_transcript(transcript, clip.start, edits) if edits else transcript,
  527:             clip.start,
  528:             clip.end,
  529:         )
  530:         for clip, edits in zip(clips, caption_edits(value))
  531:     ]
  532:     output = root / "delivery"
```

## src/clipforge/gemini_audio_editor.py 441-449
```python
  441:         selection = {"source_url": manifest["source_url"], "analysis_key": manifest["analysis_key"], "selection_basis": "saved_asr_audio_review", "clips": []}
  442:         words = [w for segment in transcript.segments for w in segment.words]
  443:         shifts = []
  444:         for i, clip in enumerate(sorted(choice["clips"], key=lambda row: row["start"]), 1):
  445:             clip = dict(clip, candidate_id=f"audio-review-{i:03}")
  446:             for name in ["start", "end"]:
  447:                 original = clip[name]
  448:                 while crossed := [w.end for w in words if w.start < clip[name] < w.end]:
  449:                     clip[name] = max(crossed)
```

## src/clipforge/gemini_audio_editor.py 555-569
```python
  555:                 rejected.append({"segment_id": sid, "reason": "unknown segment id"})
  556:                 continue
  557:             old = slots[sid]
  558:             if not audio_heard:
  559:                 rejected.append({"segment_id": sid, "reason": "audio not reported as heard"})
  560:                 continue
  561:             if sid in uncertain or old["text"] == row["text"]:
  562:                 continue
  563:             clip = selection["clips"][old["clip"] - 1]
  564:             edits = clip.setdefault("caption_edits", [])
  565:             edit = {"at": old["start"], "from": old["text"], "to": row["text"]}
  566:             try:
  567:                 changed = edited_transcript(transcript, clip["start"], [*edits, edit])
  568:                 shifted_transcript(changed, clip["start"], clip["end"])
  569:             except PipelineError:
```

## tools/colab_actions.py 188-194
```python
  188:     review = Path(os.environ.get("REVIEW_FILE", "")).read_text(encoding="utf-8")
  189:     # A completed private audio review can contain many timestamped subtitle edits.
  190:     editorial = os.environ.get("CLIPFORGE_AUDIO_REVIEW_RELEASE", "")
  191:     if editorial and not re.fullmatch(
  192:         r"clipforge-kick-editorial-(?:replay-)?[0-9]+", editorial
  193:     ):
  194:         raise ControlError("Invalid audio review release")
```

## tools/colab_actions.py 395-402
```python
  395:         if record["mode"] == "audio-stage":
  396:             payload["youtube_cookies_b64"] = os.environ.get("YOUTUBE_COOKIES_B64", "")
  397:             editorial = os.environ.get("CLIPFORGE_AUDIO_REVIEW_RELEASE", "")
  398:             if editorial:
  399:                 if not re.fullmatch(
  400:                     r"clipforge-kick-editorial-(?:replay-)?[0-9]+", editorial
  401:                 ):
  402:                     raise ControlError("Invalid audio review release")
```

## tools/colab_remote_job.py 368-374
```python
  368:         arguments += ["--config", stage["config_file"]]
  369:     stage_env = {**environment, "GH_TOKEN": payload["github_token"]}
  370:     if payload.get("audio_review_release"):
  371:         tag = payload["audio_review_release"]
  372:         if not re.fullmatch(r"clipforge-kick-editorial-(?:replay-)?[0-9]+", tag):
  373:             raise RuntimeError("Invalid audio review release")
  374:         bundles = ReleaseBundles(payload["github_token"])
```

