// Discord の承認ボタン → GitHub Actions（autopilot ワークフロー）への中継。
// ボタンの custom_id は "ap:<decision>:<job id>"（clipforge/autopilot/notify.py が作る）。
// 所有者以外の押下・署名のない要求・古い要求は受け付けない。

const DECISIONS = new Set(["approve", "reject", "publish", "skip", "retry", "restart"]);
const LABELS = {
  approve: "✅ 制作を受け付けました",
  reject: "🚫 制作しません",
  publish: "🚀 公開を受け付けました",
  skip: "🔒 非公開のままにします",
  retry: "🔁 再開を受け付けました",
  restart: "♻️ 作り直しを受け付けました",
};
const MAX_AGE_SECONDS = 300;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("autopilot approval worker");
    const body = await request.text();
    if (!(await verifyDiscord(request, body, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }
    const interaction = JSON.parse(body);
    if (interaction.type === 1) return reply({ type: 1 }); // PING
    if (interaction.type === 3) return handleButton(interaction, env);
    return ephemeral("この操作には対応していません。");
  },
};

export async function handleButton(interaction, env, fetcher = fetch) {
  const match = /^ap:([a-z]+):([0-9a-f]{12})$/.exec(String(interaction.data?.custom_id ?? ""));
  if (!match || !DECISIONS.has(match[1])) return ephemeral("不明なボタンです。");
  const [, decision, job] = match;
  const user = interaction.member?.user?.id ?? interaction.user?.id;
  if (!env.OWNER_DISCORD_ID || user !== env.OWNER_DISCORD_ID) {
    return ephemeral("このボタンは所有者だけが押せます。");
  }
  const response = await fetcher(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "autopilot-approval-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: "autopilot-decision", client_payload: { job, decision } }),
  });
  if (response.status !== 204) {
    // Buttons stay, so the owner can press again once GitHub answers.
    return ephemeral(`GitHub に送れませんでした（HTTP ${response.status}）。少し待って押し直してください。`);
  }
  const original = String(interaction.message?.content ?? "").slice(0, 1700);
  // Removing the buttons right away prevents a double decision.
  return reply({ type: 7, data: { content: `${original}\n\n${LABELS[decision]}`, components: [] } });
}

export async function verifyDiscord(request, body, publicKeyHex, now = Date.now()) {
  const signature = request.headers.get("X-Signature-Ed25519") ?? "";
  const timestamp = request.headers.get("X-Signature-Timestamp") ?? "";
  if (!publicKeyHex || !/^[0-9a-f]{128}$/i.test(signature) || !/^\d+$/.test(timestamp)) return false;
  if (Math.abs(now / 1000 - Number(timestamp)) > MAX_AGE_SECONDS) return false;
  try {
    const key = await crypto.subtle.importKey("raw", hex(publicKeyHex), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, hex(signature), new TextEncoder().encode(timestamp + body));
  } catch {
    return false;
  }
}

function hex(value) {
  const bytes = new Uint8Array(value.length / 2);
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = parseInt(value.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

function reply(payload) {
  return new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } });
}

function ephemeral(content) {
  return reply({ type: 4, data: { content, flags: 64 } });
}
