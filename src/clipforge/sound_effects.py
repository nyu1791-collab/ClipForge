"""Quiet original synthesized accents; no external samples, models or media rescan."""

import math

# Short tones with a smooth attack/release; no sampled copyrighted recordings.
# Peak amplitude is fixed at 0.025 (about -32 dBFS), after voice normalization.
RECIPES = {
    "soft_pop": (0.12, "sin(2*PI*(520*t-900*t*t))"),
    "light_chime": (0.32, "(sin(2*PI*880*t)+0.4*sin(2*PI*1320*t))/1.4"),
    "transition_tick": (0.10, "sin(2*PI*680*t)"),
}
RECIPE_VERSION = "clipforge-tones-v2-ducked"


def prepare_cues(rows, duration):
    """Keep renderable cues, with no quota or production failure for unused proposals."""
    candidates = []
    if not isinstance(rows, list):
        return []
    for row in rows[:10000]:
        if not isinstance(row, dict):
            continue
        at, kind = row.get("at"), row.get("kind")
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or not isinstance(kind, str)
            or kind not in RECIPES
            or not 0 <= at < duration
        ):
            continue
        at = round(at, 3)
        if duration - at < 0.01:
            continue
        candidates.append(
            {"at": at, "kind": kind, "reason": str(row.get("reason") or "")[:300]}
        )
    cues = []
    for cue in sorted(candidates, key=lambda c: c["at"]):
        if cues and cue["at"] - cues[-1]["at"] < 30:
            continue
        cues.append(cue)
    return cues


def mix_graph(cues, duration, rate):
    """Mix synthesized accents into [voice], producing [accented] on the clip clock."""
    graph, labels = ["[voice]asplit=2[voiceout][voicekey]"], []
    for index, cue in enumerate(cues):
        length, tone = RECIPES[cue["kind"]]
        length = min(length, duration - cue["at"])
        label = f"sfx{index}"
        graph.append(
            f"aevalsrc=exprs='0.025*sin(PI*t/{length:.6f})*({tone})':"
            f"s={rate}:d={length:.6f},"
            f"aformat=channel_layouts=stereo,adelay={round(cue['at'] * 1000)}:all=1[{label}]"
        )
        labels.append(f"[{label}]")
    graph.append(
        "".join(labels) + f"amix=inputs={len(cues)}:duration=longest:normalize=0[sebus]"
    )
    graph.append(
        "[sebus][voicekey]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=120[ducked]"
    )
    graph.append(
        "[voiceout][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[accented]"
    )
    return ";".join(graph)
