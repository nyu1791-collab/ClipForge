"""Advisory selected-audio timing helpers; no ASR, grading, retry or rejection."""

from bisect import bisect_left, bisect_right
from difflib import SequenceMatcher
import re
import subprocess
import unicodedata


def compact(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def merge_boundaries(rows, chunk_seconds=60, context=1.5):
    """Merge only a shared utterance from different adjacent audio windows."""
    result, merged = [], 0
    for row in sorted(rows, key=lambda r: (r["start"], r["end"])):
        duplicate = None
        for index in range(len(result) - 1, -1, -1):
            old = result[index]
            if row["start"] - old["end"] > 2 * context:
                break
            if abs(row["_window"] - old["_window"]) != 1:
                continue
            boundary = max(row["_window"], old["_window"]) * chunk_seconds
            if not all(
                r["start"] <= boundary + context and r["end"] >= boundary - context
                for r in (old, row)
            ):
                continue
            overlap = min(row["end"], old["end"]) - max(row["start"], old["start"])
            if overlap < 0.3 or abs(row["start"] - old["start"]) > context:
                continue
            speakers = {r.get("speaker_id") for r in (old, row)} - {None, "unknown"}
            if len(speakers) > 1:
                continue
            a, b = compact(old["text"]), compact(row["text"])
            if min(len(a), len(b)) < 8:
                continue  # Never delete short responses or repeated comic reactions.
            if re.findall(r"\d+", a) != re.findall(r"\d+", b):
                continue
            if re.findall(r"ない|なく|ません|違う|いや|未確認", a) != re.findall(
                r"ない|なく|ません|違う|いや|未確認", b
            ):
                continue
            # Similarity alone must not erase a different statement. Require containment.
            if (a in b or b in a) and SequenceMatcher(None, a, b).ratio() >= 0.85:
                duplicate = index
                break
        if duplicate is None:
            result.append(dict(row))
        else:
            old = result[duplicate]
            chosen = (
                row if len(compact(row["text"])) > len(compact(old["text"])) else old
            )
            result[duplicate] = {
                **chosen,
                "start": min(old["start"], row["start"]),
                "end": max(old["end"], row["end"]),
                "_owned": old["_owned"] or row["_owned"],
            }
            merged += 1
    return [
        {k: v for k, v in row.items() if not k.startswith("_")}
        for row in result
        if row["_owned"]
    ], {"merged_boundary_rows": merged, "policy": "adjacent_window_containment_only"}


def snap_to_energy(rows, path, duration):
    """Use nearby energy boundaries only when distinct quiet/active levels exist."""
    report = {
        "method": "selected_audio_energy_heuristic",
        "max_shift_seconds": 0.5,
        "forced_alignment": False,
        "adjusted_boundaries": 0,
    }
    try:
        import numpy as np

        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(path),
                "-t",
                str(duration),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                "highpass=f=120,lowpass=f=3500",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            timeout=min(120, max(30, duration / 4)),
            check=False,
        )
        if result.returncode:
            return rows, {**report, "status": "not_applied_decode_unavailable"}
        pcm = np.frombuffer(result.stdout[: len(result.stdout) // 2 * 2], dtype="<i2")
        frame = 160  # 20 ms, selected audio only.
        if len(pcm) < frame * 10:
            return rows, {**report, "status": "not_applied_insufficient_audio"}
        samples = (
            pcm[: len(pcm) // frame * frame].astype(np.float32).reshape(-1, frame)
            / 32768
        )
        db = 20 * np.log10(
            np.maximum(np.sqrt(np.mean(samples * samples, axis=1)), 1e-6)
        )
        quiet, loud = (float(x) for x in np.percentile(db, [20, 90]))
        if loud - quiet < 10 or loud < -52:
            return rows, {**report, "status": "not_applied_ambiguous_energy"}
        threshold = max(-52, min(-28, quiet + 8, loud - 12))
        active = db > threshold
        starts, ends = [], []
        begin = None
        for i, on in enumerate(active):
            if on and begin is None:
                begin = i
            if begin is not None and (not on or i == len(active) - 1):
                stop = i if not on else i + 1
                if stop - begin >= 3:
                    if starts and begin * 0.02 - ends[-1] < 0.10:
                        ends[-1] = stop * 0.02
                    else:
                        starts.append(begin * 0.02)
                        ends.append(stop * 0.02)
                begin = None
        changed = []
        for index, row in enumerate(rows):
            start, end = row["start"], row["end"]
            near_start = starts[
                bisect_left(starts, start - 0.5) : bisect_right(starts, start + 0.5)
            ]
            near_end = ends[
                bisect_left(ends, end - 0.5) : bisect_right(ends, end + 0.5)
            ]
            a = min(near_start, key=lambda t: abs(t - start)) if near_start else start
            b = min(near_end, key=lambda t: abs(t - end)) if near_end else end
            if index and start >= rows[index - 1]["end"]:
                # The preceding end may also have moved. Preserve existing overlaps,
                # but do not introduce a new one between originally separate phrases.
                a = max(a, changed[-1]["end"])
            if (
                index + 1 < len(rows)
                and end <= rows[index + 1]["start"]
                and b > rows[index + 1]["start"]
            ):
                b = end
            if not 0 <= a < b <= duration or b - a < 0.08:
                a, b = start, end
            report["adjusted_boundaries"] += int(a != start) + int(b != end)
            changed.append({**row, "start": round(a, 3), "end": round(b, 3)})
        return changed, {
            **report,
            "status": "applied"
            if report["adjusted_boundaries"]
            else "unchanged_no_adjustment",
        }
    except (OSError, ValueError, ImportError, subprocess.SubprocessError):
        return rows, {**report, "status": "not_applied_helper_unavailable"}
