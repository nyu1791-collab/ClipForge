import assert from "node:assert/strict";
import test from "node:test";
import worker, { handleButton, verifyDiscord } from "../src/index.js";

const toHex = (buffer) => Buffer.from(buffer).toString("hex");

async function keys() {
  const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  const publicKey = toHex(await crypto.subtle.exportKey("raw", pair.publicKey));
  return { pair, publicKey };
}

async function signed(pair, body, timestamp = String(Math.floor(Date.now() / 1000))) {
  const signature = toHex(await crypto.subtle.sign("Ed25519", pair.privateKey, new TextEncoder().encode(timestamp + body)));
  return new Request("https://worker.test/", {
    method: "POST",
    body,
    headers: { "X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp },
  });
}

const env = { OWNER_DISCORD_ID: "owner", GITHUB_REPOSITORY: "o/r", GITHUB_TOKEN: "t" };
const button = (custom_id, user = "owner") => ({
  type: 3,
  data: { custom_id },
  member: { user: { id: user } },
  message: { content: "配信が終わりました" },
});

test("answers PING only with a valid signature", async () => {
  const { pair, publicKey } = await keys();
  const body = JSON.stringify({ type: 1 });
  const ok = await worker.fetch(await signed(pair, body), { ...env, DISCORD_PUBLIC_KEY: publicKey });
  assert.deepEqual(await ok.json(), { type: 1 });
  const other = await keys();
  const bad = await worker.fetch(await signed(other.pair, body), { ...env, DISCORD_PUBLIC_KEY: publicKey });
  assert.equal(bad.status, 401);
});

test("rejects replayed (old) requests", async () => {
  const { pair, publicKey } = await keys();
  const old = String(Math.floor(Date.now() / 1000) - 3600);
  const request = await signed(pair, "{}", old);
  assert.equal(await verifyDiscord(request, "{}", publicKey), false);
});

test("owner button dispatches to GitHub and removes the buttons", async () => {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response(null, { status: 204 });
  };
  const response = await handleButton(button("ap:approve:0123456789ab"), env, fetcher);
  const payload = await response.json();
  assert.equal(calls[0].url, "https://api.github.com/repos/o/r/dispatches");
  assert.deepEqual(calls[0].body, {
    event_type: "autopilot-decision",
    client_payload: { job: "0123456789ab", decision: "approve" },
  });
  assert.equal(payload.type, 7);
  assert.deepEqual(payload.data.components, []);
});

test("other users and malformed buttons never reach GitHub", async () => {
  const fetcher = async () => assert.fail("must not dispatch");
  for (const interaction of [
    button("ap:approve:0123456789ab", "someone-else"),
    button("ap:delete:0123456789ab"),
    button("ap:approve:../../etc"),
  ]) {
    const payload = await (await handleButton(interaction, env, fetcher)).json();
    assert.equal(payload.data.flags, 64);
  }
});

test("GitHub failure keeps the buttons for another try", async () => {
  const fetcher = async () => new Response("no", { status: 502 });
  const payload = await (await handleButton(button("ap:publish:0123456789ab"), env, fetcher)).json();
  assert.equal(payload.type, 4);
  assert.match(payload.data.content, /502/);
});
