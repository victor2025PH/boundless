/**
 * Messenger DOM 操作纯函数门禁（node --test）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  synthMsgId, parseReactionFromAria, normalizeReactionEmoji,
  isUnsentPreview, isUnsentTombstone, classifyInboxHint,
  adaptiveReqEvery, canOpenThread, normalizePin, autoPinGate,
} from "./msg_ops.js";

test("synthMsgId：同内容跨调用恒定，改字段即变", () => {
  const a = synthMsgId({ chatKey: "1", direction: "in", tsLabel: "14:44", text: "hi", mediaRef: "" });
  const b = synthMsgId({ chatKey: "1", direction: "in", tsLabel: "14:44", text: "hi", mediaRef: "" });
  assert.equal(a, b);
  assert.match(a, /^m_[0-9a-f]{16}$/);
  const c = synthMsgId({ chatKey: "1", direction: "in", tsLabel: "14:44", text: "hi!", mediaRef: "" });
  assert.notEqual(a, c);
});

test("parseReactionFromAria：中英本方/对端", () => {
  assert.deepEqual(parseReactionFromAria("你用👍回应了"), { emoji: "👍", sender: "me" });
  assert.deepEqual(parseReactionFromAria("你用大笑回应了"), { emoji: "😆", sender: "me" });
  assert.deepEqual(parseReactionFromAria("Alice用赞回应了"), { emoji: "👍", sender: "peer" });
  assert.deepEqual(parseReactionFromAria("You reacted with ❤️"), { emoji: "❤️", sender: "me" });
  assert.deepEqual(parseReactionFromAria("Bob reacted with a laugh"), { emoji: "😆", sender: "peer" });
  assert.deepEqual(parseReactionFromAria("You reacted with a like"), { emoji: "👍", sender: "me" });
  assert.equal(parseReactionFromAria("消息由你发送于14:44：hi"), null);
  assert.equal(parseReactionFromAria(""), null);
});

test("normalizeReactionEmoji：词表 + emoji 抽取", () => {
  assert.equal(normalizeReactionEmoji("赞"), "👍");
  assert.equal(normalizeReactionEmoji("laugh"), "😆");
  assert.equal(normalizeReactionEmoji("a love"), "❤️");
  assert.equal(normalizeReactionEmoji("👍👍"), "👍");
  assert.equal(normalizeReactionEmoji(""), "");
});

test("unsent 预览/墓碑", () => {
  assert.equal(isUnsentPreview("你撤回了一条消息"), true);
  assert.equal(isUnsentPreview("You unsent a message"), true);
  assert.equal(isUnsentPreview("你好呀"), false);
  assert.equal(isUnsentTombstone("此消息已撤回"), true);
  assert.equal(isUnsentTombstone("This message was unsent"), true);
  assert.equal(isUnsentTombstone("hello"), false);
});

test("classifyInboxHint：与 Python 半死态两支对齐", () => {
  assert.equal(classifyInboxHint({ unread: 0 }), "");
  assert.equal(classifyInboxHint({ unread: 2, readAttempts: 3, readFails: 3 }), "e2ee_relogin");
  assert.equal(classifyInboxHint({
    unread: 1, readAttempts: 0, e2eeRatio: 0.8, convCount: 10,
  }), "e2ee_relogin");
  assert.equal(classifyInboxHint({
    unread: 1, readAttempts: 0, e2eeRatio: 0.2, convCount: 10,
  }), "");
  assert.equal(classifyInboxHint({
    unread: 1, readAttempts: 1, readFails: 1, // 样本不足
  }), "");
});

test("classifyInboxHint 支三：稳态 unread=0 读取全败 + 大面积占位（198 实测形态）", () => {
  // 198 实测：读取 4/4 全败 + 74% 占位，但失败读取已把未读消费成 0 → 前两支都不命中
  assert.equal(classifyInboxHint({
    unread: 0, readAttempts: 4, readFails: 4, e2eeRatio: 0.74, convCount: 19,
  }), "e2ee_relogin");
  // 但读取全败若占位比例低（真是空会话/对端撤回）→ 不误报
  assert.equal(classifyInboxHint({
    unread: 0, readAttempts: 4, readFails: 4, e2eeRatio: 0.1, convCount: 19,
  }), "");
  // 占位高但读取有成功（解密其实可用）→ 不报
  assert.equal(classifyInboxHint({
    unread: 0, readAttempts: 4, readFails: 1, e2eeRatio: 0.8, convCount: 19,
  }), "");
});

test("normalizePin：只认 4-12 位纯数字，脏值归空", () => {
  assert.equal(normalizePin("123456"), "123456");
  assert.equal(normalizePin(" 12 34 "), "1234");
  assert.equal(normalizePin("abc123456"), "123456");
  assert.equal(normalizePin("123"), "");          // 太短
  assert.equal(normalizePin("1234567890123"), ""); // 太长
  assert.equal(normalizePin(""), "");
  assert.equal(normalizePin(null), "");
});

test("autoPinGate：无 PIN 不动手（误触键盘的安全底线）", () => {
  const g = autoPinGate({ pin: "", tries: 0, now: 1000 });
  assert.equal(g.ok, false);
  assert.equal(g.reason, "no_pin");
});

test("autoPinGate：预算内放行、间隔太近拦截", () => {
  assert.equal(autoPinGate({ pin: "123456", tries: 0, now: 100000 }).ok, true);
  // 距上次尝试 <minGapMs(5s) → 拦（同一浮层别连打）
  const g = autoPinGate({ pin: "123456", tries: 1, lastTryTs: 100000, now: 102000 });
  assert.equal(g.ok, false);
  assert.equal(g.reason, "too_soon");
});

test("autoPinGate：烧完预算进冷却，冷却后重开一轮", () => {
  const last = 100000;
  // 2 次已用尽，距上次 5min < cooldown(10min) → 冷却拦截
  const cold = autoPinGate({
    pin: "123456", tries: 2, lastTryTs: last, now: last + 5 * 60 * 1000,
  });
  assert.equal(cold.ok, false);
  assert.equal(cold.reason, "cooldown");
  // 距上次 >10min → 重开预算（reset 让调用方清零 tries）
  const warm = autoPinGate({
    pin: "123456", tries: 2, lastTryTs: last, now: last + 11 * 60 * 1000,
  });
  assert.equal(warm.ok, true);
  assert.equal(warm.reset, true);
});

test("adaptiveReqEvery：空转拉长、封顶 4×、封禁保持底数", () => {
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 0 }), 15);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 2 }), 30);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 6 }), 60);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 99 }), 60);
  assert.equal(adaptiveReqEvery({ baseEvery: 15, emptyStreak: 10, blocked: true }), 15);
});

test("canOpenThread：回填跳未读、探针禁用、入站放行", () => {
  assert.equal(canOpenThread({ purpose: "backfill", unread: true }).ok, false);
  assert.equal(canOpenThread({ purpose: "backfill", unread: false }).ok, true);
  assert.equal(canOpenThread({ purpose: "probe" }).ok, false);
  assert.equal(canOpenThread({ purpose: "inbound" }).ok, true);
  assert.equal(canOpenThread({ purpose: "history_pull" }).ok, true);
  assert.equal(canOpenThread({ purpose: "request_read", isRequest: true }).ok, true);
});
