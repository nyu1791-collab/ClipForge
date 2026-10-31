"""The autopilot loop: detect → decide → produce → publish → learn.

Status flow (see state.TRANSITIONS):

  awaiting_approval ──approve──▶ approved ──▶ producing ──▶ produced
        │                                        │              │
        └─reject/expire                        failed ◀─────────┤
                                                 │ retry        ├─ publish_mode auto ─▶ published
                                                 ▼              └─ approve ─▶ awaiting_publish ─▶ published / skipped
                                              approved

Rules that keep it safe to run unattended:
- A VOD becomes at most one job (its key is remembered in ``seen``).
- A job is marked ``producing`` and saved *before* production starts, so a
  second trigger can never start a second paid production.
- ClipForge's production-state.json is checkpointed into the job; a retry
  reconnects to the existing production (``--resume``) instead of producing again.
- Uploads record each video ID as soon as it exists; a re-run skips them.
- X posts wait until the YouTube video is actually public.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import http, metadata, notify, sources, tiktok, x, youtube
from .state import Store, backend_from_env, transition

ROOT = Path(__file__).resolve().parents[3]
PRODUCE = ROOT / "tools" / "produce_with_gemini.py"
APPROVAL_TTL = 7 * 86400
CHECKPOINT_SECONDS = 60
MAX_PRODUCTION_STATE = 8000


def job_id(vod_key):
    return hashlib.sha256(vod_key.encode()).hexdigest()[:12]


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_slots(count, now, config, taken):
    """The next free publishing times (one clip per configured local hour)."""
    offset = timedelta(hours=config.timezone_offset_hours)
    local = datetime.fromtimestamp(now, timezone.utc) + offset
    taken = set(taken)
    found, day = [], 0
    while len(found) < count and day < 60:
        date = (local + timedelta(days=day)).date()
        for hour in config.publish_hours:
            moment = (
                datetime(date.year, date.month, date.day, hour, tzinfo=timezone.utc)
                - offset
            )
            epoch = moment.timestamp()
            if epoch >= now + 1800 and epoch not in taken:
                found.append(epoch)
                taken.add(epoch)
                if len(found) == count:
                    break
        day += 1
    return found


class Autopilot:
    def __init__(
        self,
        config,
        store=None,
        *,
        env=os.environ,
        request=http.request,
        notifier=None,
        finder=None,
        dispatch=None,
        clock=time.time,
        log=print,
        runner=subprocess.Popen,
    ):
        self.config = config
        self.channels = {c.key: c for c in config.channels}
        self.store = store or Store(backend_from_env())
        self.env = env
        self.request = request
        self.notifier = notifier or notify.Notifier(env, request, log)
        self._youtube = None
        self.finder = finder or sources.Finder(
            request, env, self._youtube_token_or_none
        )
        self.dispatch = dispatch or self._dispatch_workflow
        self.clock = clock
        self.log = log
        self.runner = runner

    # ---- credentials -------------------------------------------------
    def youtube_credentials(self):
        if self._youtube is None:
            self._youtube = youtube.Credentials.from_env(
                self.env, request=self.request, clock=self.clock
            )
        return self._youtube

    def _youtube_token_or_none(self):
        try:
            return self.youtube_credentials().token()
        except youtube.YouTubeError:
            return None

    def _dispatch_workflow(self, job, action="produce"):
        """Start the produce run in Actions (GITHUB_TOKEN with actions: write)."""
        repo = self.env.get("GITHUB_REPOSITORY")
        token = self.env.get("GITHUB_TOKEN") or self.env.get("AUTOPILOT_GITHUB_TOKEN")
        if not (repo and token):
            self.log(
                f"[dispatch] Actions 外のため手動で実行してください: autopilot {action} --job {job}"
            )
            return
        self.request(
            "POST",
            f"https://api.github.com/repos/{repo}/actions/workflows/autopilot.yml/dispatches",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json_body={
                "ref": self.env.get("GITHUB_REF_NAME", "main"),
                "inputs": {"action": action, "job": job},
            },
        )

    # ---- detect ------------------------------------------------------
    def watch(self):
        """Look for new VODs, create jobs, ask or start, expire old questions,
        and post X for videos that have gone public."""
        now = self.clock()
        to_produce, report = [], []
        for channel in self.config.channels:
            if channel.mode == "off":
                continue
            try:
                vods = self.finder.find(channel)
            except Exception as error:  # one channel must not stop the rest
                report.append(
                    f"{channel.name}: 確認できませんでした（{http.redact(error)[:160]}）"
                )
                continue

            def add(data, channel=channel, vods=vods):
                created = []
                first = channel.key not in data["bootstrapped"]
                for vod in sorted(vods, key=lambda v: v.published_at):
                    if vod.key in data["seen"]:
                        continue
                    data["seen"][vod.key] = now
                    too_old = now - vod.published_at > channel.max_age_hours * 3600
                    too_short = (
                        vod.duration_seconds is not None
                        and vod.duration_seconds < channel.min_minutes * 60
                    )
                    if first or too_old or too_short:
                        continue  # first sight of a channel only records its backlog
                    jid = job_id(vod.key)
                    data["jobs"][jid] = {
                        "id": jid,
                        "channel": channel.key,
                        "vod": vod.key,
                        "url": vod.url,
                        "title": vod.title,
                        "published_at": vod.published_at,
                        "status": "awaiting_approval",
                        "created_at": now,
                        "updated_at": now,
                        "history": [[round(now), "awaiting_approval", "detected"]],
                    }
                    if channel.mode == "auto":
                        transition(data["jobs"][jid], "approved", now, "auto")
                    created.append(jid)
                data["bootstrapped"][channel.key] = True
                return created

            for jid in self.store.update(add):
                job = self.store.load()["jobs"][jid]
                if channel.mode == "auto":
                    self.notifier.send(
                        f"🎬 {channel.name}「{job['title']}」の制作を自動で開始します\n{job['url']}"
                    )
                    to_produce.append(jid)
                else:
                    message = self.notifier.send(
                        f"📺 {channel.name} の配信が終わりました\n「{job['title']}」\n{job['url']}\n切り抜きを制作しますか？",
                        jid,
                        ("approve", "reject"),
                        mention=self.config.discord_mention_owner,
                    )
                    self._remember_message(jid, message)
        for jid in to_produce:
            self.dispatch(jid)
        self._expire(now)
        self.post_due()
        for line in report:
            self.log(line)
        return to_produce

    def _remember_message(self, jid, message):
        if message:
            self.store.update(lambda d: d["jobs"][jid].__setitem__("message", message))

    def _expire(self, now):
        def change(data):
            expired = []
            for job in data["jobs"].values():
                if (
                    job["status"] == "awaiting_approval"
                    and now - job["created_at"] > APPROVAL_TTL
                ):
                    transition(job, "expired", now, "no decision in 7 days")
                    expired.append(job)
            return expired

        for job in self.store.update(change):
            self.notifier.resolve(
                job.get("message"), f"⌛ 期限切れ（7日間決定なし）: {job['title']}"
            )

    # ---- decide ------------------------------------------------------
    def decide(self, jid, decision):
        now = self.clock()
        target = {
            "approve": "approved",
            "retry": "approved",
            "reject": "rejected",
            "skip": "skipped",
        }
        if decision == "publish":
            return self.publish_approved(jid)
        if decision not in target:
            raise ValueError(f"不明な決定です: {decision}")

        def change(data):
            job = data["jobs"].get(jid)
            if not job:
                raise KeyError(f"ジョブがありません: {jid}")
            if decision == "retry" and job["status"] != "failed":
                raise ValueError("retry は失敗したジョブだけに使えます")
            transition(job, target[decision], now, decision)
            return dict(job)

        job = self.store.update(change)
        labels = {
            "approve": "✅ 制作します",
            "retry": "🔁 再開します",
            "reject": "🚫 制作しません",
            "skip": "🔒 非公開のままにしました",
        }
        self.notifier.resolve(job.get("message"), f"{labels[decision]}: {job['title']}")
        if target[decision] == "approved":
            self.dispatch(jid)
        return job

    # ---- produce -----------------------------------------------------
    def produce(self, jid, workdir=None, publish=True):
        now = self.clock()

        def start(data):
            job = data["jobs"].get(jid)
            if not job:
                raise KeyError(f"ジョブがありません: {jid}")
            transition(job, "producing", now, "start")
            return dict(job), data.get("feedback", {}).get(job["channel"], "")

        job, feedback = self.store.update(start)
        channel = self.channels[job["channel"]]
        output = Path(workdir or tempfile.mkdtemp(prefix=f"autopilot-{jid}-"))
        output.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(PRODUCE)]
        if job.get("production_state"):
            # Reconnect to the production that already ran; never produce twice.
            (output / "production-state.json").write_text(
                job["production_state"], encoding="utf-8"
            )
            command += ["--resume", "--output", str(output)]
        else:
            command += [
                job["url"],
                "--output",
                str(output),
                "--clips",
                str(channel.clips),
                "--render-profile",
                channel.render_profile,
            ]
            if feedback:
                (output.parent / f"{jid}-feedback.txt").write_text(
                    feedback, encoding="utf-8"
                )
                command += [
                    "--audience-feedback",
                    str(output.parent / f"{jid}-feedback.txt"),
                ]
        self.notifier.send(f"⚙️ 制作中: {channel.name}「{job['title']}」")
        code = self._run_with_checkpoints(command, output, jid)
        clips = collect_clips(output)
        if code != 0 or not clips:
            reason = (
                f"制作が完了しませんでした（終了コード {code}、完成 {len(clips)} 本）"
            )
            self.store.update(
                lambda d: transition(d["jobs"][jid], "failed", self.clock(), reason)
            )
            message = self.notifier.send(
                f"⚠️ {channel.name}「{job['title']}」: {reason}。再開すると同じ制作に再接続します（再制作はしません）",
                jid,
                ("retry", "reject"),
                mention=True,
            )
            self._remember_message(jid, message)
            return False
        self.store.update(
            lambda d: (
                transition(
                    d["jobs"][jid], "produced", self.clock(), f"{len(clips)} clips"
                ),
                d["jobs"][jid].__setitem__(
                    "clips", [{"index": c["index"], "title": c["title"]} for c in clips]
                ),
            )
        )
        if publish:
            self.publish(jid, clips)
            if not workdir:
                shutil.rmtree(output, ignore_errors=True)
        return True

    def publish_from(self, jid, workdir):
        """Publish a job produced earlier in the same Actions run (separate step)."""
        job = self.store.load()["jobs"].get(jid)
        if not job or job["status"] != "produced":
            raise ValueError(f"投稿できる状態ではありません（{job and job['status']}）")
        clips = collect_clips(workdir)
        if not clips:
            raise ValueError("完成クリップが見つかりません")
        return self.publish(jid, clips)

    def _run_with_checkpoints(self, command, output, jid):
        statefile = output / "production-state.json"
        process = self.runner(command, cwd=str(ROOT))
        saved = None
        while True:
            try:
                code = process.wait(timeout=CHECKPOINT_SECONDS)
            except subprocess.TimeoutExpired:
                code = None
            if statefile.is_file():
                text = statefile.read_text(encoding="utf-8")
                if text != saved and len(text) <= MAX_PRODUCTION_STATE:
                    saved = text
                    self.store.update(
                        lambda d, t=text: d["jobs"][jid].__setitem__(
                            "production_state", t
                        )
                    )
            if code is not None:
                return code

    # ---- publish -----------------------------------------------------
    def publish(self, jid, clips):
        data = self.store.load()
        job = data["jobs"][jid]
        channel = self.channels[job["channel"]]
        auto = channel.publish_mode == "auto"
        scheduled = (
            auto and channel.privacy == "public" and bool(self.config.publish_hours)
        )
        slots = (
            next_slots(len(clips), self.clock(), self.config, data["slots"])
            if scheduled
            else []
        )
        results, errors = [], []
        for number, clip in enumerate(clips):
            record = next(
                (c for c in job.get("clips", []) if c["index"] == clip["index"]), {}
            )
            meta = metadata.build(
                clip, job, channel, self.config, self.env, self.request, self.log
            )
            entry = {"index": clip["index"], "title": meta["title"]}
            if "youtube" in channel.publish:
                if record.get("youtube"):
                    entry["youtube"] = record["youtube"]
                else:
                    publish_at = slots[number] if slots else None
                    privacy = channel.privacy if auto else "private"
                    try:
                        video = youtube.upload(
                            self.youtube_credentials(),
                            clip["video"],
                            title=meta["title"],
                            description=meta["description"],
                            tags=meta["tags"],
                            category=self.config.youtube_category,
                            privacy=privacy,
                            publish_at=iso(publish_at) if publish_at else None,
                            request=self.request,
                        )
                        entry["youtube"] = {
                            "id": video,
                            "privacy": "scheduled" if publish_at else privacy,
                            "at": publish_at
                            or (self.clock() if privacy == "public" else None),
                        }
                        self._checkpoint_clip(jid, entry, publish_at)
                    except (youtube.YouTubeError, http.HttpError, OSError) as error:
                        errors.append(
                            f"clip {clip['index']} YouTube: {http.redact(error)[:200]}"
                        )
            if "tiktok" in channel.publish and not record.get("tiktok"):
                try:
                    entry["tiktok"] = tiktok.send_to_inbox(
                        clip["video"],
                        tiktok.access_token(self.env, self.request),
                        self.request,
                    )
                except (tiktok.TikTokError, http.HttpError, OSError) as error:
                    errors.append(
                        f"clip {clip['index']} TikTok: {http.redact(error)[:200]}"
                    )
            # X waits for a public YouTube video; private/unlisted channels never post.
            if (
                "x" in channel.publish
                and entry.get("youtube")
                and channel.privacy == "public"
            ):
                entry["x"] = {
                    "status": "pending",
                    "text": metadata.x_text(meta, "", self.config),
                }
            results.append(entry)
        uploaded = [r for r in results if r.get("youtube") or r.get("tiktok")]
        now = self.clock()

        def finish(data):
            job = data["jobs"][jid]
            job["clips"] = results
            job["errors"] = errors[-10:]
            if not uploaded and channel.publish:
                transition(job, "failed", now, "nothing uploaded")
                return "failed"
            if auto:
                transition(job, "published", now, f"{len(uploaded)} uploaded")
                self._register_videos(data, job, channel)
                return "published"
            transition(job, "awaiting_publish", now, f"{len(uploaded)} private")
            return "awaiting_publish"

        outcome = self.store.update(finish)
        lines = []
        for r in results:
            link = (
                f"https://youtu.be/{r['youtube']['id']}"
                if r.get("youtube")
                else "（YouTube なし）"
            )
            when = r.get("youtube", {}).get("at")
            lines.append(
                f"・{r['title']} {link}"
                + (
                    f"（{iso(when)} 公開予定）"
                    if when and r["youtube"]["privacy"] == "scheduled"
                    else ""
                )
            )
        summary = "\n".join(lines)
        if errors:
            summary += "\n⚠️ " + "\n⚠️ ".join(errors[:5])
        if outcome == "awaiting_publish":
            message = self.notifier.send(
                f"✂️ {channel.name}「{job['title']}」の切り抜き {len(uploaded)} 本を非公開で用意しました\n{summary}\n公開しますか？",
                jid,
                ("publish", "skip"),
                mention=self.config.discord_mention_owner,
            )
            self._remember_message(jid, message)
        elif outcome == "published":
            self.notifier.send(
                f"🚀 {channel.name}「{job['title']}」を公開・予約しました\n{summary}"
            )
        else:
            message = self.notifier.send(
                f"⚠️ {channel.name}「{job['title']}」をアップロードできませんでした\n{summary}",
                jid,
                ("retry",),
                mention=True,
            )
            self._remember_message(jid, message)
        return outcome

    def _checkpoint_clip(self, jid, entry, publish_at):
        def change(data):
            clips = data["jobs"][jid].setdefault("clips", [])
            for clip in clips:
                if clip["index"] == entry["index"]:
                    clip.update(entry)
                    break
            else:
                clips.append(dict(entry))
            if publish_at:
                data["slots"].append(publish_at)

        self.store.update(change)

    def _register_videos(self, data, job, channel):
        for clip in job.get("clips", []):
            yt = clip.get("youtube")
            if yt:
                data["videos"][yt["id"]] = {
                    "job": job["id"],
                    "channel": channel.key,
                    "title": clip["title"],
                    "published_at": yt.get("at") or self.clock(),
                    "x": clip.get("x"),
                }

    def publish_approved(self, jid):
        """Owner tapped 公開する: make private uploads public (spread over slots)."""
        data = self.store.load()
        job = data["jobs"][jid]
        if job["status"] != "awaiting_publish":
            raise ValueError(f"公開待ちではありません（{job['status']}）")
        channel = self.channels[job["channel"]]
        clips = [c for c in job.get("clips", []) if c.get("youtube")]
        scheduled = channel.privacy == "public" and bool(self.config.publish_hours)
        slots = (
            next_slots(len(clips), self.clock(), self.config, data["slots"])
            if scheduled
            else []
        )
        errors = []
        for number, clip in enumerate(clips):
            at = slots[number] if slots else None
            try:
                youtube.set_privacy(
                    self.youtube_credentials(),
                    clip["youtube"]["id"],
                    channel.privacy,
                    iso(at) if at else None,
                    request=self.request,
                )
                clip["youtube"].update(
                    privacy="scheduled" if at else channel.privacy,
                    at=at or self.clock(),
                )
            except (youtube.YouTubeError, http.HttpError) as error:
                errors.append(f"{clip['title']}: {http.redact(error)[:160]}")
        now = self.clock()

        def change(data):
            stored = data["jobs"][jid]
            stored["clips"] = job["clips"]
            if errors and len(errors) == len(clips):
                transition(stored, "failed", now, "publish failed")
                return False
            transition(stored, "published", now, "owner approved")
            data["slots"].extend(
                c["youtube"]["at"]
                for c in clips
                if c["youtube"].get("privacy") == "scheduled"
            )
            self._register_videos(data, stored, channel)
            return True

        ok = self.store.update(change)
        text = "🚀 公開・予約しました" if ok else "⚠️ 公開に失敗しました"
        if errors:
            text += "\n" + "\n".join(errors[:5])
        self.notifier.resolve(job.get("message"), f"{text}: {job['title']}")
        return job

    def post_due(self):
        """Post to X once a video's publish time has passed and it is public."""
        data = self.store.load()
        now = self.clock()
        due = {
            vid: v
            for vid, v in data["videos"].items()
            if (v.get("x") or {}).get("status") == "pending"
            and v.get("published_at", now) <= now
        }
        if not due:
            return 0
        try:
            keys = x.keys_from_env(self.env)
            status = youtube.videos(self.youtube_credentials(), due, self.request)
        except (x.XError, youtube.YouTubeError, http.HttpError) as error:
            self.log(f"[x] 投稿を保留: {http.redact(error)[:200]}")
            return 0
        posted = 0
        for vid, video in due.items():
            if status.get(vid, {}).get("status", {}).get("privacyStatus") != "public":
                continue
            text = x.fit(video["x"]["text"], f"https://youtu.be/{vid}")
            try:
                post_id = x.post(text, keys, self.request)
            except (x.XError, http.HttpError) as error:
                self.store.update(
                    lambda d, vid=vid, e=error: d["videos"][vid]["x"].update(
                        status="failed", error=http.redact(e)[:200]
                    )
                )
                continue
            self.store.update(
                lambda d, vid=vid, p=post_id: d["videos"][vid]["x"].update(
                    status="posted", id=p
                )
            )
            posted += 1
        return posted

    # ---- learn -------------------------------------------------------
    def analytics(self, days=28):
        data = self.store.load()
        ids = list(data["videos"])
        if not ids:
            return {}
        today = datetime.fromtimestamp(self.clock(), timezone.utc).date()
        start = (today - timedelta(days=days)).isoformat()
        metrics = youtube.analytics(
            self.youtube_credentials(), ids, start, today.isoformat(), self.request
        )

        def change(data):
            for vid, values in metrics.items():
                if vid in data["videos"]:
                    data["videos"][vid]["m"] = {
                        k: round(float(v), 2) for k, v in values.items()
                    }
            data["feedback"] = feedback(data["videos"], self.channels)
            return data["feedback"]

        return self.store.update(change)

    def report(self):
        data = self.store.load()
        lines = ["📊 今週の振り返り"]
        for key, text in sorted(data.get("feedback", {}).items()):
            name = self.channels[key].name if key in self.channels else key
            lines.append(f"■ {name}\n{text}")
        counts = {}
        for job in data["jobs"].values():
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        lines.append(
            "ジョブ: " + "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
        )
        self.notifier.send("\n".join(lines))
        return lines


def feedback(videos, channels, minimum_views=30):
    """Short, factual notes for topic selection: what held viewers and what did not."""
    by_channel = {}
    for video in videos.values():
        m = video.get("m") or {}
        if m.get("views", 0) < minimum_views:
            continue
        by_channel.setdefault(video["channel"], []).append(
            (m.get("averageViewPercentage", 0), m.get("views", 0), video["title"])
        )
    notes = {}
    for key, rows in by_channel.items():
        if len(rows) < 3:
            continue
        rows.sort(reverse=True)
        good = "、".join(f"「{t}」(維持率{p:.0f}%・{v:.0f}回)" for p, v, t in rows[:3])
        weak = "、".join(f"「{t}」(維持率{p:.0f}%)" for p, v, t in rows[-2:])
        notes[key] = (
            f"視聴データ（直近28日・参考）: よく見られた話題 {good}。"
            f"見られにくかった話題 {weak}。似た傾向の話題を優先し、内容の誇張はしない。"
        )[:1000]
    return notes


def collect_clips(output):
    """Finished clips from a ClipForge output folder, in order."""
    clips = []
    for video in sorted(Path(output).glob("clips/*/video.mp4")):
        folder = video.parent
        info = {}
        receipt = folder / "render.json"
        if receipt.is_file():
            try:
                info = json.loads(receipt.read_text(encoding="utf-8")).get(
                    "selection", {}
                )
            except (OSError, ValueError):
                info = {}
        digits = "".join(ch for ch in folder.name if ch.isdigit())
        clips.append(
            {
                "index": int(digits) if digits else len(clips) + 1,
                "video": str(video),
                "title": info.get("title") or folder.name,
                "reason": info.get("reason", ""),
            }
        )
    return clips
