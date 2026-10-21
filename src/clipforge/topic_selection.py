"""Longer topic picks from local lexical shifts and utterance/pause boundaries."""

from .batch_models import BatchClip, ClipsManifest
from .storage import PipelineError


def plan_topics(url, duration, transcript, count=3, seconds=300):
    from .batch import clipped_subtitles, extract_title, grams, speech_end

    if not 3 <= count <= 5 or not 60 <= seconds <= 1200:
        raise PipelineError("topics require 3-5 picks, target 60-1200 seconds")
    segments = transcript.segments
    if not segments:
        raise PipelineError("no speech found; cannot invent topic picks")
    pool = []
    for i, s in enumerate(segments):
        start = s.start
        stop = speech_end(transcript, start, seconds, duration, 60, 1200)
        if stop - start < 60:
            continue
        before = "".join(t.text for t in segments if start - 60 < t.end <= start)
        after = "".join(t.text for t in segments if start <= t.start < start + 60)
        a, b = grams(before), grams(after)
        novelty = 1 - len(a & b) / max(1, len(a | b))
        gap = max(0, s.start - segments[i - 1].end) if i else 0
        subs = clipped_subtitles(transcript, start, stop)
        spoken = sum(t.end - t.start for t in subs)
        density = min(1, spoken / (stop - start))
        if density < 0.15:
            continue
        # A pause is supporting evidence, not a claim of semantic completeness.
        end_index = next(
            (k for k, t in enumerate(segments) if t.end == stop), len(segments) - 1
        )
        end_pause = (
            (segments[end_index + 1].start - stop)
            if end_index + 1 < len(segments)
            else 1
        )
        punctuation = (
            segments[end_index].text.rstrip().endswith(("。", "！", "？", "!", "?"))
        )
        boundary = (
            min(1, gap / 2) * 0.5
            + min(1, max(0, end_pause) / 2) * 0.3
            + int(punctuation) * 0.2
        )
        score = min(0.95, 0.25 + novelty * 0.25 + density * 0.2 + boundary * 0.25)
        text = "".join(t.text for t in subs).strip()
        pool.append((score, start, stop, subs, grams(text), text))
    chosen = []
    for row in sorted(pool, key=lambda r: r[0], reverse=True):
        score, start, stop, subs, tokens, text = row
        if any(min(stop, c[2]) - max(start, c[1]) > 0.1 for c in chosen):
            continue
        if any(len(tokens & c[4]) / max(1, len(tokens | c[4])) > 0.65 for c in chosen):
            continue
        chosen.append(row)
        if len(chosen) == count:
            break
    if len(chosen) != count:
        raise PipelineError(
            "insufficient distinct complete utterance ranges for topic videos"
        )
    clips = [
        BatchClip(
            clip_id=f"clip_{i:02}",
            type="topic",
            hook_title=extract_title(subs),
            start_time=start,
            end_time=stop,
            subtitles=subs,
            score=score,
            reason="前後60秒の語彙変化・発話密度・無音境界で抽出。発話末尾に合わせた通常動画候補。意味的完結・面白さは人による確認が必要。",
        )
        for i, (score, start, stop, subs, tokens, text) in enumerate(
            sorted(chosen, key=lambda r: r[1]), 1
        )
    ]
    return ClipsManifest(
        source_url=url,
        source_duration=duration,
        output_kind="topics",
        selection_method="local_transcript_audio",
        clips=clips,
    )
