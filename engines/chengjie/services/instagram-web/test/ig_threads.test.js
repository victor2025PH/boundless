import test from "node:test";
import assert from "node:assert/strict";

import {
  isRelativeTimeLabel,
  normRowText,
  parseIgThreadRow,
  splitSenderPrefix,
  stripTrailingRelTime,
  threadPreviewKey,
} from "../ig_threads.js";

test("normRowText 折叠空白并容忍非字符串", () => {
  assert.equal(normRowText("  旅行群\n\n明天几点  "), "旅行群 明天几点");
  assert.equal(normRowText(null), "");
  assert.equal(normRowText(undefined), "");
  assert.equal(normRowText(42), "42");
});

test("相对时间标签被识别（中英 + 刚刚/Active now）", () => {
  for (const s of ["2h", "15 m", "3d", "1w", "5分钟", "2小时", "刚刚", "now", "Just now", "Active now"]) {
    assert.equal(isRelativeTimeLabel(s), true, s);
  }
  for (const s of ["", "旅行群", "在吗", "2 个人", "hh"]) {
    assert.equal(isRelativeTimeLabel(s), false, s);
  }
});

test("splitSenderPrefix 支持半角/全角冒号，无前缀时原样返回", () => {
  assert.deepEqual(splitSenderPrefix("Alice: 明天几点"), { sender: "Alice", text: "明天几点" });
  assert.deepEqual(splitSenderPrefix("阿丽：在吗"), { sender: "阿丽", text: "在吗" });
  assert.deepEqual(splitSenderPrefix("Alice sent an attachment"), {
    sender: "",
    text: "Alice sent an attachment",
  });
  // 冒号在正文里（前缀过长）不切腰
  const long = `${"很".repeat(60)}：后半段`;
  assert.equal(splitSenderPrefix(long).sender, "");
  // 只有前缀没正文 → 不认（否则正文会被吞成空）
  assert.deepEqual(splitSenderPrefix("Alice:"), { sender: "", text: "Alice:" });
});

test("私聊行：标题=对方名、正文=末条预览、chat_type 空", () => {
  const r = parseIgThreadRow({
    tid: "t1",
    rowText: "alice_wang 在吗 2h",
    spans: ["alice_wang", "在吗", "2h"],
    imgCount: 1,
  });
  assert.deepEqual(r, {
    tid: "t1",
    title: "alice_wang",
    chatType: "",
    senderName: "",
    text: "在吗",
    parsed: true,
  });
});

test("私聊里自己发的那条不剥 'You:' 前缀（前缀不是群判据）", () => {
  const r = parseIgThreadRow({
    tid: "t2",
    rowText: "alice_wang You: ok 5m",
    spans: ["alice_wang", "You: ok", "5m"],
    imgCount: 1,
  });
  assert.equal(r.chatType, "");
  assert.equal(r.senderName, "");
  assert.equal(r.text, "You: ok");
});

test("群行：叠头像 ≥2 判群，发言人前缀剥进 sender_name", () => {
  const r = parseIgThreadRow({
    tid: "t3",
    rowText: "旅行群 Alice: 明天几点 1h",
    spans: ["旅行群", "Alice: 明天几点", "1h"],
    imgCount: 3,
  });
  assert.deepEqual(r, {
    tid: "t3",
    title: "旅行群",
    chatType: "group",
    senderName: "Alice",
    text: "明天几点",
    parsed: true,
  });
});

test("群行无冒号前缀：发言人留空、正文原样（不硬猜）", () => {
  const r = parseIgThreadRow({
    tid: "t4",
    rowText: "旅行群 Alice sent an attachment 1h",
    spans: ["旅行群", "Alice sent an attachment", "1h"],
    imgCount: 2,
  });
  assert.equal(r.chatType, "group");
  assert.equal(r.senderName, "");
  assert.equal(r.text, "Alice sent an attachment");
});

test("标题在 alt/aria 与可见 span 重复出现时保序去重", () => {
  const r = parseIgThreadRow({
    tid: "t5",
    rowText: "旅行群 旅行群 Bob：来了 3d",
    spans: ["旅行群", "旅行群", "Bob：来了", "3d"],
    imgCount: 2,
  });
  assert.equal(r.title, "旅行群");
  assert.equal(r.senderName, "Bob");
  assert.equal(r.text, "来了");
});

test("片段不足以区分标题/正文 → 回落首版行为（标题空、正文=整行）", () => {
  const r = parseIgThreadRow({
    tid: "t6",
    rowText: "alice_wang 发送了一个附件 2h",
    spans: ["alice_wang"],
    imgCount: 1,
  });
  assert.equal(r.parsed, false);
  assert.equal(r.title, "");
  assert.equal(r.text, "alice_wang 发送了一个附件 2h");
});

test("回落分支仍如实给出 chat_type（头像张数是独立信号）", () => {
  const r = parseIgThreadRow({
    tid: "t7",
    rowText: "旅行群 2h",
    spans: ["旅行群", "2h"],
    imgCount: 4,
  });
  // 去掉时间后只剩标题一个片段 → 正文回落整行，但仍判群
  assert.equal(r.parsed, false);
  assert.equal(r.chatType, "group");
  assert.equal(r.text, "旅行群 2h");
});

test("imgCount 缺失/脏值按 0 处理（宁可判私聊，误判成群后果更重）", () => {
  for (const bad of [undefined, null, "abc", NaN]) {
    const r = parseIgThreadRow({ tid: "t8", rowText: "x y", spans: ["x", "y"], imgCount: bad });
    assert.equal(r.chatType, "", String(bad));
  }
});

test("stripTrailingRelTime 只剥行尾时间，不动正文里的数字", () => {
  assert.equal(stripTrailingRelTime("alice 在吗 2h"), "alice 在吗");
  assert.equal(stripTrailingRelTime("alice 在吗 5 分钟"), "alice 在吗");
  assert.equal(stripTrailingRelTime("alice 在吗 Active now"), "alice 在吗");
  assert.equal(stripTrailingRelTime("alice 明天 3 点 1h"), "alice 明天 3 点");
  // 全是时间标签时不剥成空（宁可留着，也不要把键归零把所有行并成一条）
  assert.equal(stripTrailingRelTime("2h"), "2h");
  assert.equal(stripTrailingRelTime(""), "");
});

test("去重键与走时无关：同一条旧消息 2h→3h 不再被当新消息", () => {
  const mk = (t) =>
    parseIgThreadRow({ tid: "t9", rowText: `旅行群 Alice: 在吗 ${t}`, spans: ["旅行群", "Alice: 在吗", t], imgCount: 2 });
  assert.equal(threadPreviewKey(mk("2h")), threadPreviewKey(mk("3h")));
  // 回落分支（片段不足）同样免疫走时
  const fb = (t) => parseIgThreadRow({ tid: "t9", rowText: `旅行群 在吗 ${t}`, spans: ["旅行群"], imgCount: 2 });
  assert.equal(threadPreviewKey(fb("2h")), threadPreviewKey(fb("6d")));
  // 真的换了内容仍要变
  assert.notEqual(threadPreviewKey(mk("2h")), threadPreviewKey(fb("2h")));
});

test("空入参不抛异常", () => {
  const r = parseIgThreadRow(undefined);
  assert.deepEqual(r, { tid: "", title: "", chatType: "", senderName: "", text: "", parsed: false });
});
