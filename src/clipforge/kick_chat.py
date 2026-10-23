"""Kick chat replay: fetch a VOD's chat and render it as an ASS side panel."""

import datetime as dt
import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from .storage import PipelineError

KICK_V1 = "https://kick.com/api/v1"
KICK_V2 = "https://kick.com/api/v2/channels"
REQUEST_TIMEOUT = 30
ATTEMPTS = 4
MAX_BACKOFF = 8.0
CHAT_STEP = dt.timedelta(seconds=5)
NEXT_STEP = dt.timedelta(seconds=1)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"
DEFAULT_COLOR = "#FFFFFF"

_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
_BREAKS = re.compile(r"[\t\n\r\v\f]+")
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

# Panel geometry, measured at 1080p with a 480 px panel and scaled from there.
REF_HEIGHT = 1080
REF_PANEL_W = 480
REF_FONT = 34
REF_LINE = 46
REF_WRAP = 13.6  # full-width characters per line
REF_HEAD = 30
REF_HEAD_Y = 30
REF_TOP = 90
REF_BOTTOM = 30  # bottom margin
REF_PAD = 22
GAP_RATIO = 0.25  # blank space between messages, as a share of a line
INDENT = 1.0  # continuation lines, in full-width characters
MIN_VISIBLE = 1.5  # seconds a message stays on the panel at least
MAX_MESSAGE_LINES = 6
NAME_CHARS = 14


def fetch_messages(channel_slug, vod_uuid, start, end, *, lead=90.0, session=None):
    """Chat posted from start - lead to end (seconds from VOD start), in time order.

    Each item is {"t": seconds from stream start, "name", "color", "text"}.
    """
    if end <= start:
        raise ValueError("end must be later than start")
    if session is None:
        from curl_cffi import requests

        session = requests.Session(impersonate="chrome")
    channel = f"{KICK_V2}/{quote(channel_slug, safe='')}"
    channel_id = _dict(_get_json(session, channel)).get("id")
    if channel_id is None:
        raise PipelineError("Kick channel id not found")
    origin = _vod_origin(session, channel, vod_uuid)
    messages_url = f"{KICK_V2}/{quote(str(channel_id), safe='')}/messages"

    # Each request returns the chat just after start_time; the cursor only moves forward.
    budget = 3 * int((end - start + lead) // 5) + 20
    cursor = origin + dt.timedelta(seconds=max(0.0, start - lead))
    stop = origin + dt.timedelta(seconds=end)
    seen = {}
    requests_made = 0
    while cursor < stop:
        if requests_made >= budget:
            raise PipelineError("Kick chat paging exceeded its request budget")
        requests_made += 1
        payload = _get_json(
            session, messages_url, {"start_time": cursor.strftime(TIME_FORMAT)}
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise PipelineError("Kick chat response has no data")
        latest = cursor
        new_messages = 0
        rows = data.get("messages") or []
        for row in data.get("messages") or []:
            if not isinstance(row, dict):
                continue
            when = _parse_time(row.get("created_at"))
            if when is None:
                continue
            latest = max(latest, when)
            key = _message_key(row, when)
            if key not in seen:
                seen[key] = (when, row)
                new_messages += 1
        # Re-read the boundary second: pages can split a burst at that timestamp.
        # A repeated page makes no progress, so only then move to the next second.
        boundary = latest.replace(microsecond=0)
        if boundary > cursor or new_messages:
            cursor = max(cursor, boundary)
        else:
            cursor += NEXT_STEP if rows else CHAT_STEP

    lower = start - lead
    out = []
    for when, row in seen.values():
        t = (when - origin).total_seconds()
        if t < lower or t > end:
            continue
        sender = _dict(row.get("sender"))
        identity = _dict(sender.get("identity"))
        out.append(
            {
                "t": t,
                "name": _plain(sender.get("username")) or "-",
                "color": _hex_color(identity.get("color")),
                "text": _plain(_EMOTE.sub(r":\1:", _as_text(row.get("content")))),
            }
        )
    out.sort(key=lambda message: message["t"])
    return out


def panel_ass(
    messages,
    clip_start,
    clip_end,
    *,
    width=1920,
    height=1080,
    panel_x=1440,
    font="Noto Sans CJK JP",
    anonymize=False,
):
    """ASS subtitles for a chat panel at x >= panel_x, times relative to clip_start."""
    duration = clip_end - clip_start
    if duration <= 0:
        raise ValueError("clip_end must be later than clip_start")
    panel_w = width - panel_x
    if panel_w <= 0 or height <= 0:
        raise ValueError("the panel must fit inside the frame")

    k_h = height / REF_HEIGHT
    k_w = panel_w / REF_PANEL_W
    font_px = max(8, round(REF_FONT * k_h))
    line_h = max(font_px + 1, round(REF_LINE * k_h))
    gap = max(1, round(line_h * GAP_RATIO))
    head_px = max(8, round(REF_HEAD * k_h))
    pad = round(REF_PAD * k_w)
    left = panel_x + pad
    indent = font_px * INDENT
    top = round(REF_TOP * k_h)
    bottom = height - round(REF_BOTTOM * k_h)
    space = bottom - top
    # Stacked messages cost (lines * line_h + gap) each; the stack fits when the sum
    # is at most space + gap, i.e. the lines and the gaps between them fit in space.
    budget = space + gap
    wrap = min(REF_WRAP * k_w * REF_FONT / font_px, (panel_w - pad) / font_px)
    font_name = font.replace(",", " ").strip() or "Noto Sans CJK JP"

    entries = []
    for index, row in enumerate(messages):
        rel = float(row["t"]) - clip_start
        if rel >= duration:
            continue
        raw = _plain(row.get("name"))
        name = _anonymous(raw) if anonymize else (raw or "-")
        name_out, name_w = _clip_name(_ass_safe(name), 0.6 * wrap)
        color = _ass_color(_hex_color(row.get("color")))
        body = _ass_safe(_plain(row.get("text")))
        lines = _wrap(body, name_w + 0.6, wrap)
        if len(lines) > MAX_MESSAGE_LINES:
            lines = lines[:MAX_MESSAGE_LINES]
            lines[-1] = lines[-1][:-1] + "…"
        head = f"{{\\c{color}\\b1}}{name_out}{{\\c&HFFFFFF&\\b0}}  "
        entries.append(
            _Entry(
                rel=max(0.0, rel),
                index=index,
                head=head,
                lines=tuple(lines),
                cost=len(lines) * line_h + gap,
            )
        )

    # Thin out crowded moments: walking from the newest message back, keep a message
    # only if the messages posted within MIN_VISIBLE after it still fit. Older
    # messages in a burst are dropped first, and every kept one stays on screen for
    # at least MIN_VISIBLE seconds (or until the clip ends).
    entries.sort(key=lambda entry: (entry.rel, entry.index))
    kept = []  # newest first
    for entry in reversed(entries):
        used = entry.cost
        for other in reversed(kept):  # oldest kept first
            if other.rel >= entry.rel + MIN_VISIBLE:
                break
            used += other.cost
        if used <= budget:
            kept.append(entry)
    kept.reverse()

    # Panel state changes whenever a message arrives; the newest messages sit at the
    # bottom and older ones stack upward until they no longer fit.
    states = []
    arrived = []
    for i, entry in enumerate(kept):
        arrived.append(entry)
        if i + 1 < len(kept) and kept[i + 1].rel == entry.rel:
            continue
        states.append((entry.rel, _stack(arrived, budget)))

    events = [
        f"Dialogue: 0,{_stamp(0)},{_stamp(duration)},Head,,0,0,0,,"
        f"{{\\an7\\pos({left},{REF_HEAD_Y})}}チャット"
    ]
    ends = [when for when, _ in states[1:]] + [duration]
    for (begin, shown), finish in zip(states, ends):
        y = bottom
        for n, entry in enumerate(shown):
            if n:
                y -= gap
            for li in reversed(range(len(entry.lines))):
                if li == 0:
                    x, text = left, entry.head + entry.lines[0]
                else:
                    x, text = left + indent, entry.lines[li]
                events.append(
                    f"Dialogue: 0,{_stamp(begin)},{_stamp(finish)},Chat,,0,0,0,,"
                    f"{{\\an1\\pos({x:g},{y})}}{text}"
                )
                y -= line_h

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chat,{font_name},{font_px},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,1,0,0,0,1
Style: Head,{font_name},{head_px},&H00A0A0A0,&H00A0A0A0,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "\n".join(events) + "\n"


@dataclass(frozen=True)
class _Entry:
    rel: float
    index: int
    head: str
    lines: tuple
    cost: float


def _stack(arrived, budget):
    shown, used = [], 0.0
    for entry in reversed(arrived):  # newest first
        if used + entry.cost > budget:
            break
        used += entry.cost
        shown.append(entry)
    return shown


def _get_json(session, url, params=None):
    status = None
    for attempt in range(ATTEMPTS):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except Exception:  # transport errors may carry the URL; never surface them
            status = None
        else:
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise PipelineError("Kick chat response was not JSON") from None
            if status != 429 and status < 500:
                raise PipelineError(f"Kick chat request failed (HTTP {status})")
        if attempt + 1 < ATTEMPTS:
            _pause(min(MAX_BACKOFF, 2.0**attempt))
    detail = f"HTTP {status}" if status is not None else "connection error"
    raise PipelineError(f"Kick chat request failed after retries ({detail})")


def _pause(seconds):
    time.sleep(seconds)


def _vod_origin(session, channel, vod_uuid):
    listing = _get_json(session, f"{channel}/videos")
    stamp = None
    for row in listing if isinstance(listing, list) else []:
        video = _dict(row).get("video")
        if isinstance(video, dict) and video.get("uuid") == vod_uuid:
            stamp = _dict(row).get("start_time")
            break
    if stamp is None:
        detail = _get_json(session, f"{KICK_V1}/video/{quote(vod_uuid, safe='')}")
        stamp = _dict(_dict(detail).get("livestream")).get("start_time")
    origin = _parse_time(stamp)
    if origin is None:
        raise PipelineError("Kick VOD start time not found")
    return origin


def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1).replace("Z", "+00:00")
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)


def _message_key(row, when):
    if row.get("id") is not None:
        return str(row["id"])
    sender = _dict(row.get("sender"))
    return (
        when.isoformat(),
        str(sender.get("username", "")),
        _as_text(row.get("content")),
    )


def _dict(value):
    return value if isinstance(value, dict) else {}


def _as_text(value):
    return "" if value is None else str(value)


def _plain(value):
    """Text with line breaks turned into spaces and other control characters removed."""
    return _CONTROL.sub("", _BREAKS.sub(" ", _as_text(value))).strip()


def _hex_color(value):
    if isinstance(value, str) and _HEX.fullmatch(value.strip()):
        return value.strip().upper()
    return DEFAULT_COLOR


def _ass_color(hex_color):
    h = hex_color.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&"


def _ass_safe(text):
    return text.replace("\\", "＼").replace("{", "(").replace("}", ")")


def _anonymous(name):
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"視聴者{int(digest[:8], 16) % 1000:03d}"


def _char_width(ch):
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.55


def _clip_name(text, max_units):
    out, used = "", 0.0
    for ch in text:
        w = _char_width(ch)
        if len(out) >= NAME_CHARS or used + w > max_units:
            break
        out += ch
        used += w
    return out, used


def _wrap(text, first_used, wrap):
    lines, line, used, limit = [], "", first_used, wrap
    for ch in text:
        w = _char_width(ch)
        if line and used + w > limit:
            lines.append(line)
            line, used, limit = "", 0.0, wrap - INDENT
            if ch == " ":
                continue
        line += ch
        used += w
    lines.append(line)
    return lines


def _stamp(seconds):
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02}:{s:02}.{c:02}"
