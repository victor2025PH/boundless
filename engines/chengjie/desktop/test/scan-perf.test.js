"use strict";

// P0 注入层性能门禁：
//   ① analyzeMutations 纯函数语义（自体突变过滤 / 键击噪音过滤 / 脏气泡归集）——
//      这是「开会话上百轮全量重扫」的止血阀，语义漂移=要么扫不动（漏更新）要么白扫（复发）。
//   ② core.js 接线形状的静态钉：观察器回调必须走 analyzeMutations+scheduleScan（禁直呼
//      scanAll）、旧 2s/3s 轮询必须死、取文缓存必须挂在 bubbleVisibleText、陈旧比对必须
//      先看脏标记再碰「整子树克隆」的取文。跑法：node test/scan-perf.test.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const core = require("../../shared/inject/core.js");
const { analyzeMutations } = core;

let pass = 0;

// ── 假节点/适配器：纯对象即可（DOM 依赖全部经 adapters 注入）─────────────────────
const ours = (o) => Object.assign({ ours: true }, o);
const officialNode = (o) => Object.assign({ ours: false }, o);
const A = {
  isOurs: (n) => !!(n && n.ours),
  bubbleOf: (n) => (n && n.bubble) || null,
};
function rec(type, target, added, removed) {
  return { type, target, addedNodes: added || [], removedNodes: removed || [] };
}

// 1. 空批 → 不相关
{
  const v = analyzeMutations([], A);
  assert.strictEqual(v.relevant, false);
  assert.strictEqual(v.dirty.size, 0);
  pass++;
  const v2 = analyzeMutations(null, A);
  assert.strictEqual(v2.relevant, false);
  pass++;
}

// 2. target 在我们注入的节点内部（往译文盒里塞行）→ 整条跳过
{
  const v = analyzeMutations([rec("childList", ours(), [officialNode()], [])], A);
  assert.strictEqual(v.relevant, false, "our-box 内部变化不得触发扫描");
  pass++;
}

// 3. 我们往官方气泡里挂按钮/译文盒（target 官方、增删全是 .aitr-*）→ 跳过（掐断自触发循环）
{
  const bub = officialNode();
  const v = analyzeMutations(
    [rec("childList", officialNode({ bubble: bub }), [ours()], [])], A);
  assert.strictEqual(v.relevant, false, "append 注入控件不得自触发重扫");
  pass++;
  const v2 = analyzeMutations(
    [rec("childList", officialNode(), [], [ours()])], A);
  assert.strictEqual(v2.relevant, false, "移除注入控件同理");
  pass++;
}

// 4. 官方真插了消息节点 → 相关
{
  const v = analyzeMutations([rec("childList", officialNode(), [officialNode()], [])], A);
  assert.strictEqual(v.relevant, true);
  pass++;
}

// 5. 混合批（我们的 + 官方的同批到达）→ 相关（不得因掺了我们的就整批吞掉）
{
  const v = analyzeMutations(
    [rec("childList", officialNode(), [ours(), officialNode()], [])], A);
  assert.strictEqual(v.relevant, true, "混合 addedNodes 必须算相关");
  pass++;
}

// 6. 变化落在某气泡子树内 → 该气泡进 dirty（取文缓存失效判据）
{
  const bub = officialNode();
  const v = analyzeMutations(
    [rec("childList", officialNode({ bubble: bub }), [officialNode()], [])], A);
  assert.strictEqual(v.relevant, true);
  assert.ok(v.dirty.has(bub), "气泡内变化必须标脏该气泡");
  assert.strictEqual(v.dirty.size, 1);
  pass++;
}

// 7. characterData 在气泡外（composer 打字/计时器跳动）→ 不相关（键击级噪音源）
{
  const v = analyzeMutations([rec("characterData", officialNode(), [], [])], A);
  assert.strictEqual(v.relevant, false, "气泡外 characterData 不得排扫描");
  pass++;
}

// 8. characterData 在气泡内（消息被原地改写）→ 相关 + 标脏
{
  const bub = officialNode();
  const v = analyzeMutations(
    [rec("characterData", officialNode({ bubble: bub }), [], [])], A);
  assert.strictEqual(v.relevant, true);
  assert.ok(v.dirty.has(bub));
  pass++;
}

// 9. attributes 型（data-mid/data-peer-id 变化，已过 attributeFilter）→ 相关
{
  const v = analyzeMutations([rec("attributes", officialNode(), [], [])], A);
  assert.strictEqual(v.relevant, true);
  pass++;
}

// 10. 多记录混批：我们的跳过、官方的算数，dirty 只收官方那条的气泡
{
  const bub = officialNode();
  const v = analyzeMutations([
    rec("childList", ours(), [officialNode()], []),
    rec("characterData", officialNode(), [], []),
    rec("childList", officialNode({ bubble: bub }), [officialNode()], []),
  ], A);
  assert.strictEqual(v.relevant, true);
  assert.strictEqual(v.dirty.size, 1);
  assert.ok(v.dirty.has(bub));
  pass++;
}

// 11. 缺 adapters（防御）：全部按官方对待，不抛
{
  const v = analyzeMutations([rec("childList", {}, [{}], [])], {});
  assert.strictEqual(v.relevant, true);
  pass++;
}

// ── core.js 接线形状静态钉 ───────────────────────────────────────────────────────
{
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "shared", "inject", "core.js"), "utf8");

  // 观察器回调必须经 analyzeMutations + scheduleScan；「回调直呼 scanAll」是载入慢根因，禁复活
  assert.ok(!/new MutationObserver\(\(\)\s*=>\s*scanAll\(\)\)/.test(src),
    "观察器回调不得直呼 scanAll（去抖被拆＝重扫风暴复发）");
  pass++;
  const obsBlock = src.slice(src.indexOf("new MutationObserver"));
  assert.ok(obsBlock.indexOf("analyzeMutations(records") > 0, "回调须走 analyzeMutations");
  assert.ok(obsBlock.indexOf("scheduleScan()") > 0, "回调须走 scheduleScan 去抖");
  pass += 2;

  // 旧轮询必须死：2s 全量扫 + 3s 挂钮轮（增量路径接管后它们只剩纯浪费）
  assert.ok(!/setInterval\(scanAll,\s*2000\)/.test(src), "2s 全量轮询不得复活");
  assert.ok(!/setInterval\(mountSmartReplyButton/.test(src), "3s 挂钮轮询不得复活");
  pass += 2;
  // 自愈全量轮仍在（观察器边角兜底），且带 full 语义
  assert.ok(src.indexOf("scanAll({ full: true }); }, 10000)") > 0, "10s 自愈全量轮缺失");
  pass++;

  // characterData 已订阅（取文缓存的失效窄口——文本原地改写不发 childList）
  assert.ok(src.indexOf("characterData: true") > 0, "观察器须订阅 characterData");
  pass++;

  // 取文缓存挂在 bubbleVisibleText（Telegram text() 整子树克隆的止血点）
  const bvt = src.slice(src.indexOf("function bubbleVisibleText"),
    src.indexOf("function targetLang") > 0
      ? src.length : undefined).slice(0, 800);
  assert.ok(bvt.indexOf("TEXT_CACHE.get(bubble)") > 0, "bubbleVisibleText 须先查缓存");
  assert.ok(bvt.indexOf("TEXT_CACHE.set(bubble") > 0, "bubbleVisibleText 须回填缓存");
  pass += 2;

  // 陈旧比对：脏标记闸门必须先于取文（否则每轮全体克隆取文复发）
  const rs = src.slice(src.indexOf("function refreshStale"),
    src.indexOf("function appendInjectControl"));
  const iGate = rs.indexOf("DIRTY.has(bubble)");
  const iText = rs.indexOf("bubbleVisibleText(bubble)");
  assert.ok(iGate > 0 && iText > 0 && iGate < iText,
    "refreshStale 须先看脏标记再取文");
  pass++;
  // 脏标记的**消费**必须在 busy 短路之后：「翻译在途时消息被编辑」的竞态里，
  // 提前消费＝陈旧检测退化到只能等 10s 自愈轮
  const iBusyRet = rs.indexOf('data-aitr-busy")) return;');
  const iConsume = rs.indexOf("DIRTY.delete(bubble)");
  assert.ok(iBusyRet > 0 && iConsume > iBusyRet && iConsume < iText,
    "脏标记须在 busy 短路后、取文前消费");
  pass++;

  // 挂钮折进 scanAll（幂等廉价），body 缺失有守卫
  const scan = src.slice(src.indexOf("function scanAll"),
    src.indexOf("function findComposer"));
  assert.ok(scan.indexOf("mountSmartReplyButton()") > 0, "挂钮须折进 scanAll");
  pass++;
  const msb = src.slice(src.indexOf("function mountSmartReplyButton"));
  assert.ok(msb.indexOf("!document.body") > 0, "mountSmartReplyButton 须守卫 body 未就绪");
  pass++;

  // 导出契约：纯函数可被 Node 单测（本文件自身就是消费者，防导出被顺手删掉）
  assert.strictEqual(typeof analyzeMutations, "function");
  pass++;
}

console.log(`scan-perf.test.js: ${pass} passed`);
