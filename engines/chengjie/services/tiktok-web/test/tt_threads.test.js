import test from "node:test";
import assert from "node:assert/strict";
import { parseTtThreadRow, threadPreviewKey, threadIdFromHref, isRelativeTimeLabel } from "../tt_threads.js";

test("href → thread id: u= 数字 / @uniqueId / 无", () => {
  assert.equal(threadIdFromHref("/messages?u=7123456789"), "7123456789");
  assert.equal(threadIdFromHref("https://www.tiktok.com/@shop.ph_1?lang=en"), "@shop.ph_1");
  assert.equal(threadIdFromHref("/messages"), "");
});

test("行快照 → 标题/正文/方向/会话键（@uniqueId 与真机桥同键）", () => {
  const row = parseTtThreadRow({ href: "/@buyer_x", spans: ["buyer_x", "how much is this?", "2h"], rowText: "buyer_x how much is this? 2h" });
  assert.equal(row.title, "buyer_x");
  assert.equal(row.text, "how much is this?");
  assert.equal(row.directionHint, "in");
  assert.equal(row.chatKey, "tiktok:user:buyer_x");
  const mine = parseTtThreadRow({ href: "/messages?u=99", spans: ["Buyer", "You: 350 pesos", "刚刚"] });
  assert.equal(mine.directionHint, "out");
  assert.equal(mine.text, "350 pesos");
  assert.equal(mine.chatKey, "tiktok:web:99");
});

test("判不出就回落：单段当正文、无键用标题 slug；预览键不含时间", () => {
  const one = parseTtThreadRow({ spans: ["hello there", "3d"], rowText: "hello there 3d" });
  assert.equal(one.title, "");
  assert.equal(one.text, "hello there");
  assert.equal(one.chatKey, "");
  const slug = parseTtThreadRow({ spans: ["Shop PH!", "ok", "1h"] });
  assert.equal(slug.chatKey, "tiktok:web:shop-ph");
  const a = parseTtThreadRow({ spans: ["A", "hi", "1h"] });
  const b = parseTtThreadRow({ spans: ["A", "hi", "2h"] });
  assert.equal(threadPreviewKey(a), threadPreviewKey(b));
  assert.ok(isRelativeTimeLabel("Yesterday") && isRelativeTimeLabel("昨天") && !isRelativeTimeLabel("hello"));
});
