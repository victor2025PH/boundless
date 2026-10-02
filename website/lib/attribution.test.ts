/**
 * 来源码 / Telegram uid 归因冒烟：npx tsx lib/attribution.test.ts（假 window/localStorage）。
 * src 与 tg 各自读 URL → 落 30 天本地存储 → 无 URL 时回读；非法值一律归空，且互不串。
 */
import assert from "assert";

const store = new Map<string, string>();
const localStorage = {
  getItem: (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => void store.set(k, v),
};
const g = globalThis as unknown as Record<string, unknown>;
const win = { location: { search: "?src=ad_biz_a01&tg=5151" } };
g.window = win;
g.localStorage = localStorage;

async function main() {
  const A = await import("./attribution");

  assert.strictEqual(A.getSrc(), "ad_biz_a01");
  assert.strictEqual(A.getTgUid(), "5151");
  assert.ok(store.has("ml_src") && store.has("ml_tg"), "两者分开落盘");

  win.location.search = "";
  assert.strictEqual(A.getSrc(), "ad_biz_a01", "无 URL 参数时回读存储");
  assert.strictEqual(A.getTgUid(), "5151");

  win.location.search = "?tg=abc&src=<x>";
  assert.strictEqual(A.getTgUid(), "5151", "非法 tg 不覆盖已存的");
  assert.strictEqual(A.getSrc(), "ad_biz_a01", "非法 src 不覆盖已存的");

  win.location.search = "?tg=99";
  assert.strictEqual(A.getTgUid(), "99", "新 uid 覆盖");
  assert.strictEqual(A.getSrc(), "ad_biz_a01", "tg 变化不影响 src");

  store.clear();
  win.location.search = "";
  assert.strictEqual(A.getSrc(), "");
  assert.strictEqual(A.getTgUid(), "", "普通访客两者都为空，下载链不带参数");

  console.log("attribution smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
