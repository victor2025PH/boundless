/**
 * overlay-policy 纯函数冒烟：npx tsx lib/overlay-policy.test.ts
 */
import assert from "node:assert/strict";
import { overlayPolicy } from "./overlay-policy";

// 中文常规页：促销/游戏化照常
assert.deepEqual(overlayPolicy("/"), { promo: true, gamification: true });
assert.deepEqual(overlayPolicy("/pricing"), { promo: true, gamification: true });

// 收紧页：国际路由 / GEO / 教程 / 智聊下载落地页（zh 与 /en 成对）
for (const p of [
  "/en",
  "/ko/pricing",
  "/compare/x",
  "/chatx/tutorials",
  "/download/chatx",
  "/download/chatx/",
  "/en/download/chatx",
]) {
  assert.deepEqual(overlayPolicy(p), { promo: false, gamification: false }, p);
}

// 其它下载页不受影响
assert.deepEqual(overlayPolicy("/download"), { promo: true, gamification: true });
assert.deepEqual(overlayPolicy("/download/studio"), { promo: true, gamification: true });

console.log("overlay-policy smoke OK");
