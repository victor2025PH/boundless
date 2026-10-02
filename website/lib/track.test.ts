/**
 * trackOnce 冒烟：npx tsx lib/track.test.ts（假 window/sessionStorage，捕获 sendBeacon）。
 * 验证同 key 会话内只上报一次（StrictMode 双执行 / 重挂 / 刷新），不同 src 各计一次，
 * 以及 sessionStorage 不可用时靠内存集去重。
 */
import assert from "assert";

const store = new Map<string, string>();
let storageBroken = false;
const sessionStorage = {
  getItem: (k: string) => {
    if (storageBroken) throw new Error("blocked");
    return store.get(k) ?? null;
  },
  setItem: (k: string, v: string) => {
    if (storageBroken) throw new Error("blocked");
    store.set(k, v);
  },
};
const beacons: string[] = [];
const g = globalThis as unknown as Record<string, unknown>;
g.window = { location: { pathname: "/download/chatx" }, sessionStorage };
g.sessionStorage = sessionStorage;
g.document = { referrer: "" };
Object.defineProperty(globalThis, "navigator", {
  value: { sendBeacon: (_u: string, b: Blob) => (beacons.push(String(b.size)), true) },
  configurable: true,
});

async function main() {
  const T = await import("./track");

  assert.strictEqual(T.trackOnce("chatx_landing_view:ad_a", "chatx_landing_view", { src: "ad_a" }), true);
  assert.strictEqual(T.trackOnce("chatx_landing_view:ad_a", "chatx_landing_view", { src: "ad_a" }), false, "同 key 第二次不发");
  assert.strictEqual(beacons.length, 1);

  // 模拟刷新：内存集清空（重新 import 不现实），直接验证 sessionStorage 已持久化标记
  assert.strictEqual(store.get("ml_once:chatx_landing_view:ad_a"), "1");

  assert.strictEqual(T.trackOnce("chatx_landing_view:ad_b", "chatx_landing_view", { src: "ad_b" }), true, "不同 src 各计一次");
  assert.strictEqual(beacons.length, 2);

  // sessionStorage 被 webview 禁用：仍靠内存集去重、且不抛
  storageBroken = true;
  assert.strictEqual(T.trackOnce("chatx_landing_view:ad_c", "chatx_landing_view", { src: "ad_c" }), true);
  assert.strictEqual(T.trackOnce("chatx_landing_view:ad_c", "chatx_landing_view", { src: "ad_c" }), false);
  assert.strictEqual(beacons.length, 3);

  console.log("track smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
