import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldMarkScanned } from "../scan-signal.js";

test("isNewLogin 在 pending 时判定已扫描", () => {
  assert.equal(shouldMarkScanned({ status: "pending", qrImage: "" }, { isNewLogin: true }), true);
});

test("creds.me 首次出现且展示过二维码 → 已扫描（兜底信号）", () => {
  assert.equal(shouldMarkScanned({ status: "pending", qrImage: "data:image/png;base64,xxx" }, { meId: "639270135480" }), true);
});

test("恢复的老会话（无 QR）即使 me 存在也不误判扫码", () => {
  // 磁盘恢复重连：无 qrImage、me 早在凭据里 —— 绝不能被当成一次新扫码
  assert.equal(shouldMarkScanned({ status: "pending", qrImage: "" }, { meId: "639270135480" }), false);
});

test("非 pending 态（authorized/reconnecting）不被覆盖", () => {
  assert.equal(shouldMarkScanned({ status: "authorized", qrImage: "x" }, { isNewLogin: true }), false);
  assert.equal(shouldMarkScanned({ status: "reconnecting", qrImage: "x" }, { meId: "1" }), false);
});

test("无信号 / 空 entry 不判定", () => {
  assert.equal(shouldMarkScanned({ status: "pending", qrImage: "x" }, {}), false);
  assert.equal(shouldMarkScanned({ status: "pending", qrImage: "x" }), false);
  assert.equal(shouldMarkScanned(null, { isNewLogin: true }), false);
});
