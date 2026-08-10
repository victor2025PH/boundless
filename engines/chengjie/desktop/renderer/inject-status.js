"use strict";

// 注入诊断纯函数：把 inject(tg-inject.js) 上报的原始状态映射为「状态条」展示模型。
// 双模式：浏览器经 <script> 加载后成为全局函数（renderer.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/inject-status.test.js 单测）。
//
// 输入 s（inject-status 上报）：{ supported:bool, composer:bool, bubbles:int, chatOpen:bool,
//   extract?: { decorated, unresolved, ingestTried, ingestKeyed } }
// 输出：{ cls: "ok"|"warn"|"bad"|"wait", code, text, detail }
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
  if (!s) return { cls: "wait", code: "wait", text: "等待注入…", detail: "注入脚本尚未上报状态" };
  if (!s.supported) {
    return { cls: "bad", code: "unsupported", text: "无注入档案", detail: "该平台无选择器档案，功能不可用" };
  }
  if (!s.chatOpen && !s.composer) {
    return { cls: "warn", code: "no_chat", text: "未登录/未进入会话", detail: "未检测到会话或输入框：请扫码登录并打开一个对话" };
  }
  if (!s.composer) {
    return { cls: "warn", code: "mismatch_composer", text: "选择器失配（输入框）", detail: "找不到输入框，注入可能因官方改版失效（需校准 PROFILES.composer）" };
  }
  if (s.chatOpen && !s.bubbles) {
    return { cls: "warn", code: "mismatch_bubble", text: "选择器失配（消息）", detail: "会话已打开但抓不到消息气泡（需校准 PROFILES.bubble/text）" };
  }
  const ex = s.extract;
  const decorated = _exNum(ex, "decorated", "decorated");
  const unresolved = _exNum(ex, "unresolved", "unresolved");
  const tried = _exNum(ex, "ingestTried", "ingest_tried");
  const keyed = _exNum(ex, "ingestKeyed", "ingest_keyed");
  // 气泡数得到了、却一条都没能装饰上（且确实逐条试过并全失败）＝正文提取器塌了：
  // 翻译按钮会一个都不出现，而只看元素存在性的旧判据会报「注入正常」。
  if ((s.bubbles || 0) > 0 && decorated === 0 && unresolved > 0) {
    return {
      cls: "warn", code: "mismatch_text", text: "选择器失配（正文提取）",
      detail: `抓到 ${s.bubbles} 条气泡但一条都提不出正文/媒体（需校准 PROFILES.bubbleText）`,
    };
  }
  // 本轮真要回流的每一条都拿不到 (mid, peerId)＝消息标识提取塌了：消息一条都进不了统一收件箱。
  if (tried > 0 && keyed === 0) {
    return {
      cls: "warn", code: "mismatch_ingest", text: "选择器失配（消息标识）",
      detail: `${tried} 条待回流消息取不到 msg_id/会话 id，同步已静默中断（需校准 mid/peerId）`,
    };
  }
  return { cls: "ok", code: "ok", text: "注入正常", detail: "输入框 ✓　消息气泡 ×" + (s.bubbles || 0) };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { deriveInjectState };
}
