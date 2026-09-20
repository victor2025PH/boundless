"use strict";

// 注入诊断纯函数：把 inject(tg-inject.js) 上报的原始状态映射为「状态条」展示模型。
// 双模式：浏览器经 <script> 加载后成为全局函数（renderer.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/inject-status.test.js 单测）。
//
// 输入 s（inject-status 上报）：{ supported:bool, composer:bool, bubbles:int, chatOpen:bool,
//   extract?: { decorated, unresolved, ingestTried, ingestKeyed } }
// 输出：{ cls: "ok"|"warn"|"bad"|"wait", code, vars }
//
// ⚠ 本层**不出文案**（2026-08-20 i18n 收口）：中文写死在这里时，英文壳的状态条/栏点
// tooltip 永远是中文，而这是坐席每天盯的那一行。展示层（renderer.js::renderInjectStatus）
// 按 `SH("inject." + code)` / `SH("inject." + code + ".d", vars)` 取当前语言文案，
// `vars`（bubbles/tried）供 detail 插值——「抓到 N 条气泡」那个 N 是数据、不是文案。
//
// `code` 与后端 `classify_inject_health` 的状态字**同名**（unsupported / no_chat /
// mismatch_composer / mismatch_bubble / mismatch_text / mismatch_ingest / ok / wait）：
// 展示文案会随产品改写、cls 只有四档太粗，需要按具体症状分流的调用方（如受控出站
// 「无档案就别拉命令」）必须拿一个稳定字，否则只能去 match 中文文案。
//
// 判定顺序必须与后端 `desktop_inject_health.classify_inject_health` 逐档一致（两侧同口径，
// 否则壳层状态条与运营看板会各说一套）。extract 段的两档专治「元素在、内容抓不出」的静默失效。
function _exNum(ex, camel, snake) {
  const v = ex && (ex[camel] != null ? ex[camel] : ex[snake]);
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function deriveInjectState(s) {
  if (!s) return { cls: "wait", code: "wait", vars: {} };
  if (!s.supported) {
    return { cls: "bad", code: "unsupported", vars: {} };
  }
  if (!s.chatOpen && !s.composer) {
    return { cls: "warn", code: "no_chat", vars: {} };
  }
  if (!s.composer) {
    return { cls: "warn", code: "mismatch_composer", vars: {} };
  }
  if (s.chatOpen && !s.bubbles) {
    return { cls: "warn", code: "mismatch_bubble", vars: {} };
  }
  const ex = s.extract;
  const decorated = _exNum(ex, "decorated", "decorated");
  const unresolved = _exNum(ex, "unresolved", "unresolved");
  const tried = _exNum(ex, "ingestTried", "ingest_tried");
  const keyed = _exNum(ex, "ingestKeyed", "ingest_keyed");
  // 气泡数得到了、却一条都没能装饰上（且确实逐条试过并全失败）＝正文提取器塌了：
  // 翻译按钮会一个都不出现，而只看元素存在性的旧判据会报「注入正常」。
  if ((s.bubbles || 0) > 0 && decorated === 0 && unresolved > 0) {
    return { cls: "warn", code: "mismatch_text", vars: { bubbles: s.bubbles || 0 } };
  }
  // 本轮真要回流的每一条都拿不到 (mid, peerId)＝消息标识提取塌了：消息一条都进不了统一收件箱。
  if (tried > 0 && keyed === 0) {
    return { cls: "warn", code: "mismatch_ingest", vars: { tried } };
  }
  return { cls: "ok", code: "ok", vars: { bubbles: s.bubbles || 0 } };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { deriveInjectState };
}
