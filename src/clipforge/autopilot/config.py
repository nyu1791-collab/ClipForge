"""Autopilot settings: which channels to watch and how far to automate each one.

The real list is private. It is read from the ``AUTOPILOT_CONFIG`` environment
variable (a GitHub Secret holding YAML or JSON) or from ``autopilot.yaml``, which
git ignores. ``autopilot.example.yaml`` documents every field.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PLATFORMS = ("youtube", "twitch", "kick")
MODES = ("auto", "approve", "off")
DESTINATIONS = ("youtube", "x", "tiktok")
PRIVACY = ("private", "unlisted", "public")
PROFILES = ("preview", "standard", "full-hd")
_CHANNEL_ID = {
    "youtube": re.compile(r"^UC[A-Za-z0-9_-]{22}$"),
    "twitch": re.compile(r"^[A-Za-z0-9_]{3,25}$"),
    "kick": re.compile(r"^[A-Za-z0-9_-]{2,40}$"),
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Channel:
    platform: str
    id: str
    name: str
    # auto: start production as soon as a VOD appears. approve: ask on Discord.
    mode: str = "approve"
    # auto: publish right after production. approve: upload privately, then ask.
    publish_mode: str = "approve"
    publish: tuple = ("youtube",)
    privacy: str = "public"
    clips: int = 5
    render_profile: str = "preview"
    # VODs older than this are never picked up (first run, long outages).
    max_age_hours: float = 72.0
    # Skip short uploads (Shorts, trailers) when the duration is known.
    min_minutes: float = 10.0
    # Credit and permission note added to every description.
    credit: str = ""
    tags: tuple = ()

    @property
    def key(self):
        return f"{self.platform}:{self.id.lower()}"


@dataclass(frozen=True)
class Config:
    channels: tuple
    timezone_offset_hours: float = 9.0
    # Local hours for scheduled publishing; one clip per slot.
    publish_hours: tuple = (12, 18, 21)
    youtube_category: str = "20"
    description_footer: str = ""
    x_template: str = "{title}\n{url}"
    metadata_model: str = "gemini-3.5-flash"
    discord_mention_owner: bool = True
    extra: dict = field(default_factory=dict)


def _word(value):
    """YAML 1.1 reads bare off/on/no/yes as booleans; take them back as words."""
    if value is False:
        return "off"
    if value is True:
        return "on"
    return value


def _choice(value, allowed, name):
    value = _word(value)
    if value not in allowed:
        raise ConfigError(
            f"{name} は {', '.join(allowed)} のどれかにしてください: {value!r}"
        )
    return value


def _channel(row, index):
    if not isinstance(row, dict):
        raise ConfigError(f"channels[{index}] は項目の集まりにしてください")
    where = f"channels[{index}]"
    platform = _choice(
        str(row.get("platform", "")).lower(), PLATFORMS, where + ".platform"
    )
    ident = str(row.get("id", "")).strip()
    if not _CHANNEL_ID[platform].match(ident):
        hint = {
            "youtube": "UC で始まる24文字のチャンネルID",
            "twitch": "ログイン名",
            "kick": "チャンネルのスラッグ",
        }[platform]
        raise ConfigError(f"{where}.id は {hint} を指定してください: {ident!r}")
    publish = tuple(row.get("publish", ["youtube"]) or ())
    for target in publish:
        _choice(target, DESTINATIONS, where + ".publish")
    clips = int(row.get("clips", 5))
    if not 1 <= clips <= 10:
        raise ConfigError(f"{where}.clips は1〜10にしてください")
    return Channel(
        platform=platform,
        id=ident,
        name=str(row.get("name") or ident),
        mode=_choice(row.get("mode", "approve"), MODES, where + ".mode"),
        publish_mode=_choice(
            row.get("publish_mode", "approve"),
            ("auto", "approve"),
            where + ".publish_mode",
        ),
        publish=publish,
        privacy=_choice(row.get("privacy", "public"), PRIVACY, where + ".privacy"),
        clips=clips,
        render_profile=_choice(
            row.get("render_profile", "preview"), PROFILES, where + ".render_profile"
        ),
        max_age_hours=float(row.get("max_age_hours", 72)),
        min_minutes=float(row.get("min_minutes", 10)),
        credit=str(row.get("credit", "")),
        tags=tuple(str(t) for t in row.get("tags", ()) or ()),
    )


def parse(text):
    """YAML (if PyYAML is installed) or JSON text -> Config."""
    text = (text or "").strip()
    if not text:
        raise ConfigError("設定が空です")
    try:
        raw = json.loads(text)
    except ValueError:
        try:
            import yaml
        except ImportError:
            raise ConfigError(
                "YAML を読むには pyyaml が必要です（JSONでも書けます）"
            ) from None
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ConfigError(f"設定を読めません: {error}") from None
    if not isinstance(raw, dict):
        raise ConfigError("設定の最上位は項目の集まりにしてください")
    rows = raw.get("channels") or []
    channels = tuple(_channel(row, i) for i, row in enumerate(rows))
    keys = [c.key for c in channels]
    if len(keys) != len(set(keys)):
        raise ConfigError("同じチャンネルが2回登録されています")
    hours = tuple(int(h) for h in raw.get("publish_hours", (12, 18, 21)))
    if not hours or any(not 0 <= h <= 23 for h in hours):
        raise ConfigError("publish_hours は0〜23の時刻の一覧にしてください")
    known = {
        "channels",
        "publish_hours",
        "timezone_offset_hours",
        "youtube_category",
        "description_footer",
        "x_template",
        "metadata_model",
        "discord_mention_owner",
    }
    return Config(
        channels=channels,
        timezone_offset_hours=float(raw.get("timezone_offset_hours", 9)),
        publish_hours=tuple(sorted(set(hours))),
        youtube_category=str(raw.get("youtube_category", "20")),
        description_footer=str(raw.get("description_footer", "")),
        x_template=str(raw.get("x_template", "{title}\n{url}")),
        metadata_model=str(raw.get("metadata_model", "gemini-3.5-flash")),
        discord_mention_owner=bool(raw.get("discord_mention_owner", True)),
        extra={k: v for k, v in raw.items() if k not in known},
    )


def load(path=None):
    text = os.environ.get("AUTOPILOT_CONFIG", "")
    if text.strip():
        return parse(text)
    path = Path(path or "autopilot.yaml")
    if not path.is_file():
        raise ConfigError(
            "AUTOPILOT_CONFIG（GitHub Secret）か autopilot.yaml を用意してください。"
            "見本は autopilot.example.yaml です"
        )
    return parse(path.read_text(encoding="utf-8"))
