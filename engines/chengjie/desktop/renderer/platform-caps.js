"use strict";
/**
 * 桌面壳「平台能力」单一事实源（P0 2026-08-13）。
 *
 * 背景：官方网页内嵌 Tab（Path2）与统一收件箱 + sidecar（Path1）是两套登录态。
 * Messenger / IG / X / Zalo 的 inject 档案默认 canIngest=false → 本页消息不进
 * 后端队列，右侧若仍显示「全自动运行中」= 对坐席撒谎。
 *
 * 约定：
 *  - assistOnlyEmbed：可内嵌官方页，但仅人工 + 翻译/标签/话术；全自动不在此页发生
 *  - preferInboxAuto：全自动产能只认统一收件箱（Messenger 另需 messenger-web 服务器登录）
 *
 * 浏览器全局 `PlatformCaps`；node 单测可 `require`。
 */
(function (root) {
  const ASSIST_ONLY_EMBED = Object.freeze({
    messenger: true,
    instagram: true,
    x: true,
    zalo: true,
  });

  const PREFER_INBOX_AUTO = Object.freeze({
    messenger: true,
    line: true,
  });

  function normalizePlatform(p) {
    return String(p || "").trim().toLowerCase();
  }

  function isAssistOnlyEmbed(platform) {
    return !!ASSIST_ONLY_EMBED[normalizePlatform(platform)];
  }

  function prefersInboxAuto(platform) {
    const p = normalizePlatform(platform);
    return !!PREFER_INBOX_AUTO[p] || isAssistOnlyEmbed(p);
  }

  // ── i18n 边界（2026-08-19）─────────────────────────────────────────────────
  // 本模块是**纯判定层**：只回稳定 key，不回人话。此前直接回中文串 → 英文坐席看到
  // 「人工」「此标签=官方网页…」。与 inject-status / webmulti 同一架构选择：取词留在
  // 展示层（renderer 的 SH()），单测因此断言 key（稳定）而非文案（会随本地化漂移）。
  // 空串＝不显示（调用方据此决定挂不挂元素），沿用旧语义不变。

  /** 标签条副标 i18n key（空串＝不挂 via-tag） */
  function assistOnlyTabTagKey(platform) {
    return isAssistOnlyEmbed(platform) ? "caps.tag_manual" : "";
  }

  /** 激活内嵌 Tab 时顶栏诚实条 key；非 assist-only 返回空串 */
  function assistOnlyBannerKey(platform) {
    if (!isAssistOnlyEmbed(platform)) return "";
    // messenger 多一句「全自动另需服务器完整登录」——这是坐席最常误解的一点
    // （本页登录了≠全自动能跑），故单列一条文案。
    return normalizePlatform(platform) === "messenger"
      ? "caps.banner_messenger"
      : "caps.banner_generic";
  }

  /** 新增账号菜单副标 key */
  function assistOnlyMenuHintKey(platform) {
    return isAssistOnlyEmbed(platform) ? "caps.menu_manual" : "";
  }

  const api = {
    ASSIST_ONLY_EMBED,
    PREFER_INBOX_AUTO,
    normalizePlatform,
    isAssistOnlyEmbed,
    prefersInboxAuto,
    assistOnlyTabTagKey,
    assistOnlyBannerKey,
    assistOnlyMenuHintKey,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.PlatformCaps = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
