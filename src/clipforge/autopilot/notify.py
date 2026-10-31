"""Discord notifications with approval buttons.

With DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID the message carries buttons whose
custom_id is ``ap:<decision>:<job id>``; the approval Worker
(deploy/approval-worker) turns a click into a repository_dispatch.
With only DISCORD_WEBHOOK_URL the message is plain text and the decision is made
by running the "autopilot" workflow by hand (action=decide).
Without either, messages go to the job log.
"""

import os

from . import http

API = "https://discord.com/api/v10/"
DECISIONS = {
    "approve": ("制作する", 3),
    "reject": ("しない", 4),
    "publish": ("公開する", 3),
    "skip": ("非公開のまま", 2),
    "retry": ("もう一度", 1),
    "restart": ("最初から作り直す", 2),
}


def buttons(job_id, decisions):
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": DECISIONS[d][1],
                    "label": DECISIONS[d][0],
                    "custom_id": f"ap:{d}:{job_id}",
                }
                for d in decisions
            ],
        }
    ]


class Notifier:
    def __init__(self, env=os.environ, request=http.request, echo=print):
        self.token = env.get("DISCORD_BOT_TOKEN")
        self.channel = env.get("DISCORD_CHANNEL_ID")
        self.webhook = env.get("DISCORD_WEBHOOK_URL")
        self.owner = env.get("DISCORD_OWNER_ID")
        self.request = request
        self.echo = echo

    @property
    def interactive(self):
        return bool(self.token and self.channel)

    def _headers(self):
        return {"Authorization": f"Bot {self.token}"}

    def send(self, text, job_id=None, decisions=(), mention=False):
        """Returns {"channel": id, "message": id} when the message can be edited later."""
        if mention and self.owner:
            text = f"<@{self.owner}> {text}"
        text = text[:1900]
        mentions = {"users": [self.owner]} if mention and self.owner else {"parse": []}
        try:
            if self.interactive:
                body = {"content": text, "allowed_mentions": mentions}
                if job_id and decisions:
                    body["components"] = buttons(job_id, decisions)
                reply = self.request(
                    "POST",
                    API + f"channels/{self.channel}/messages",
                    headers=self._headers(),
                    json_body=body,
                ).json()
                return {"channel": self.channel, "message": reply.get("id")}
            if self.webhook:
                if job_id and decisions:
                    text += (
                        "\n（承認は Actions の autopilot を action=decide、job="
                        + job_id
                        + " で実行）"
                    )
                self.request(
                    "POST",
                    self.webhook,
                    json_body={"content": text[:1990], "allowed_mentions": mentions},
                )
                return None
        except http.HttpError as error:
            # A notification failure must never lose the job; it stays in the state.
            self.echo(f"[discord] 送信できませんでした: {error}")
        self.echo(f"[notify] {text}")
        return None

    def resolve(self, message, text):
        """Replace a decision message's buttons with the outcome."""
        if not (message and message.get("message") and self.interactive):
            return
        try:
            self.request(
                "PATCH",
                API + f"channels/{message['channel']}/messages/{message['message']}",
                headers=self._headers(),
                json_body={"content": text[:1900], "components": []},
            )
        except http.HttpError as error:
            self.echo(f"[discord] 更新できませんでした: {error}")
