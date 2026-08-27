"use strict";
// 壳级通知纯函数门禁（update-notify.js）：版本比较 / 公告规范化与定向 /
// 单横幅优先级（更新就绪 > 紧急公告 > 下载中 > 普通公告）/ 稍后与已读语义。
// Node 直跑，零 Electron 依赖。

const assert = require("assert");
const un = require("../update-notify.js");
const shI18n = require("../renderer/shell-i18n.js");

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
  console.log(`  ok - ${msg}`);
}

// i18n 边界（2026-08-19）：更新类横幅回 textKey+vars（取词在渲染层），公告类回 text
// （服务端下发内容不该被壳词典翻译）。断言据此走 render()——它复刻渲染层那一行消费
// 逻辑，于是「文案讲清了什么」这类产品不变量仍能在两种语言下被钉住。
function render(n, lang) {
  if (n && n.textKey) return shI18n.tIn(lang || "zh", n.textKey, n.vars || {});
  return (n && n.text) || "";
}

// ── 版本比较 ──────────────────────────────────────────────────────────────
ok(un.cmpVersions("1.0.26", "1.0.9") === 1, "1.0.26 > 1.0.9（数值段比较，非字符串）");
ok(un.cmpVersions("1.0.26", "1.0.26") === 0, "同版本相等");
ok(un.cmpVersions("1.0", "1.0.0") === 0, "缺段按 0 补齐");
ok(un.cmpVersions("1.0.x", "1.0.0") === 0, "非数字段容错按 0");

// ── target 版本定向 ────────────────────────────────────────────────────────
ok(un.versionSatisfies("1.0.26", { minVersion: "", maxVersion: "" }), "空区间=全员");
ok(un.versionSatisfies("1.0.26", null), "无 target=全员（fail-open）");
ok(!un.versionSatisfies("1.0.26", { minVersion: "", maxVersion: "1.0.25" }), "maxVersion 之上不命中（只提醒老版本）");
ok(un.versionSatisfies("1.0.25", { minVersion: "", maxVersion: "1.0.25" }), "maxVersion 含端点");
ok(!un.versionSatisfies("1.0.20", { minVersion: "1.0.21", maxVersion: "" }), "minVersion 之下不命中");

// ── 公告规范化 ────────────────────────────────────────────────────────────
const rawFeed = {
  items: [
    { id: "a1", type: "release", title: "v1.0.27 已发布", body: "修复若干问题", published_at: "2026-08-14T00:00:00Z" },
    { id: "a1", type: "release", title: "重复 id 应去重" },
    { id: "", title: "缺 id 丢弃" },
    { id: "a2", type: "weird-type", title: "未知类型归 notice" },
    { id: "a3", type: "urgent", title: "紧急维护公告", body_zh: "body_zh 兼容", link: "https://bd2026.cc/download/chatx", published_at: "2026-08-10T00:00:00Z" },
    "not-an-object",
    { id: "a4" },
  ],
};
const items = un.normalizeAnnouncements(rawFeed);
ok(items.length === 3, "规范化：去重/缺id/空内容/非对象全丢弃，余 3 条");
ok(items.find((x) => x.id === "a2").type === "notice", "未知 type 归 notice");
ok(items.find((x) => x.id === "a3").body === "body_zh 兼容", "body_zh 字段兼容");
ok(un.normalizeAnnouncements(null).length === 0, "空 feed 容错");
ok(un.normalizeAnnouncements({ items: "garbage" }).length === 0, "items 非数组容错");

// ── 未读排序：紧急 > 发版 > 通知，同级按时间降序 ────────────────────────────
const pend = un.pendingAnnouncements(items, "1.0.26", []);
ok(pend[0].id === "a3", "urgent 排最前（即使 published_at 更旧）");
ok(un.pendingAnnouncements(items, "1.0.26", ["a3", "a1", "a2"]).length === 0, "全已读=无待展示");

// ── pickNotice 优先级 ─────────────────────────────────────────────────────
const NOW = 1700000000000;
const base = { announcements: items, appVersion: "1.0.26", readIds: [], snoozedUntil: 0, now: NOW };

let n = un.pickNotice({ ...base, update: { phase: "downloaded", version: "1.0.27", percent: 100 } });
ok(n.kind === "update" && n.action === "restart", "更新就绪压过一切（含 urgent 公告）");
ok(render(n).includes("v1.0.27") && render(n).includes("重启"), "就绪文案带版本与重启动作");
ok(render(n, "en").includes("v1.0.27") && /restart/i.test(render(n, "en")),
  "英文就绪文案同样带版本与 restart 动作（i18n 后语义不缩水）");

n = un.pickNotice({ ...base, update: { phase: "downloading", version: "1.0.27", percent: 42 } });
ok(n.kind === "announcement" && n.id === "a3", "下载中让位紧急公告");

n = un.pickNotice({ ...base, readIds: ["a3"], update: { phase: "downloading", version: "1.0.27", percent: 42 } });
ok(n.kind === "update" && n.action === "none" && render(n).includes("42%"), "紧急已读后显示下载进度（无动作按钮）");
ok(render(n, "en").includes("42%"), "英文横幅同样透传进度百分比");

n = un.pickNotice({ ...base, readIds: ["a3"], update: { phase: "idle" } });
ok(n.kind === "announcement" && n.id === "a1", "无更新时按序展示普通公告（release 先于 notice）");

n = un.pickNotice({ ...base, readIds: ["a1", "a2", "a3"], update: { phase: "idle" } });
ok(n.kind === "", "无更新且全已读=不显示");

// ── 稍后（snooze）语义 ────────────────────────────────────────────────────
const snoozeUntil = un.nextSnooze(NOW, 4);
ok(snoozeUntil === NOW + 4 * 3600 * 1000, "nextSnooze 默认 4 小时");
ok(un.nextSnooze(NOW, -1) === NOW + 4 * 3600 * 1000, "非法时长回落 4 小时");
n = un.pickNotice({ ...base, readIds: ["a1", "a2", "a3"], snoozedUntil: snoozeUntil, update: { phase: "downloaded", version: "1.0.27" } });
ok(n.kind === "", "更新就绪被稍后（4h 窗内）=不打扰");
n = un.pickNotice({ ...base, readIds: ["a1", "a2", "a3"], snoozedUntil: NOW - 1, update: { phase: "downloaded", version: "1.0.27" } });
ok(n.kind === "update", "稍后到期自动恢复提醒");
n = un.pickNotice({ ...base, snoozedUntil: snoozeUntil, update: { phase: "downloaded", version: "1.0.27" } });
ok(n.kind === "announcement", "更新被稍后时公告照常轮到（稍后只对更新横幅生效）");

// ── 公告文案与长正文截断 ──────────────────────────────────────────────────
const longBody = "很长的正文".repeat(60);
const longItems = un.normalizeAnnouncements({ items: [{ id: "L1", type: "notice", title: "标题", body: longBody }] });
n = un.pickNotice({ update: { phase: "idle" }, announcements: longItems, appVersion: "1.0.26", readIds: [], snoozedUntil: 0, now: NOW });
ok(!n.textKey && n.text.length < 140 && n.text.endsWith("…"),
  "公告正文回 text（服务端内容不进壳词典）且单行截断（横幅不是阅读器）");
ok(n.action === "none", "无 link 的公告没有主按钮");

// ── 版本定向端到端：老版本公告不打扰已升级用户 ─────────────────────────────
const targeted = un.normalizeAnnouncements({
  items: [{ id: "T1", type: "urgent", title: "请尽快升级", target: { minVersion: "", maxVersion: "1.0.25" } }],
});
n = un.pickNotice({ update: { phase: "idle" }, announcements: targeted, appVersion: "1.0.26", readIds: [], snoozedUntil: 0, now: NOW });
ok(n.kind === "", "「催升级」公告对 1.0.26 不显示（target maxVersion=1.0.25）");
n = un.pickNotice({ update: { phase: "idle" }, announcements: targeted, appVersion: "1.0.22", readIds: [], snoozedUntil: 0, now: NOW });
ok(n.kind === "announcement" && n.id === "T1", "同一公告对 1.0.22 显示");

// ── 发版公告隐式版本定向（id=release-vX 对 >=X 客户端自动静默） ──────────────
ok(un.releaseSuperseded({ id: "release-v1.0.27", type: "release" }, "1.0.27"), "已在 1.0.27 → release-v1.0.27 视为过时");
ok(un.releaseSuperseded({ id: "release-v1.0.27", type: "release" }, "1.0.28"), "更新版本同样过时");
ok(!un.releaseSuperseded({ id: "release-v1.0.27", type: "release" }, "1.0.26"), "还在 1.0.26 → 应看到发版公告");
ok(!un.releaseSuperseded({ id: "release-v1.0.27", type: "urgent" }, "1.0.27"), "仅 release 类型做隐式过滤");
ok(!un.releaseSuperseded({ id: "custom-id", type: "release" }, "9.9.9"), "id 不合 release-vX 形不过滤");
const relFeed = un.normalizeAnnouncements({
  items: [{ id: "release-v1.0.27", type: "release", title: "ChatX v1.0.27 已发布", body: "更新说明" }],
});
n = un.pickNotice({ update: { phase: "idle" }, announcements: relFeed, appVersion: "1.0.26", readIds: [], snoozedUntil: 0, now: NOW });
ok(n.kind === "announcement" && n.id === "release-v1.0.27", "1.0.26 客户端看到发版公告");
n = un.pickNotice({ update: { phase: "idle" }, announcements: relFeed, appVersion: "1.0.27", readIds: [], snoozedUntil: 0, now: NOW });
ok(n.kind === "", "刚升到 1.0.27 的客户端不再被「v1.0.27 已发布」打扰（发布脚本无需算上一版本号）");

// ── normalizeFeed：条目 + 顶层 min_supported_version ──────────────────────
let feed = un.normalizeFeed({ items: [{ id: "f1", title: "t" }], min_supported_version: " 1.0.20 " });
ok(feed.items.length === 1 && feed.minSupportedVersion === "1.0.20", "normalizeFeed 同时取条目与强制线（trim）");
feed = un.normalizeFeed({ items: [], minSupportedVersion: "1.0.21" });
ok(feed.minSupportedVersion === "1.0.21", "驼峰写法兼容");
feed = un.normalizeFeed(null);
ok(feed.items.length === 0 && feed.minSupportedVersion === "", "空 feed 容错（离线首启）");

// ── forcedUpgradeActive：强制升级线判定 ───────────────────────────────────
ok(un.forcedUpgradeActive("1.0.19", "1.0.20"), "低于线 → 强制");
ok(!un.forcedUpgradeActive("1.0.20", "1.0.20"), "线上（等于）不强制");
ok(!un.forcedUpgradeActive("1.0.21", "1.0.20"), "高于线不强制");
ok(!un.forcedUpgradeActive("1.0.19", ""), "线为空=未启用，永不强制");
ok(!un.forcedUpgradeActive("1.0.19", "   "), "线为空白同未启用");

// ── pickNotice：强制升级压过一切、不可稍后、随下载阶段推进 ─────────────────
const forcedBase = { ...base, appVersion: "1.0.19", minSupportedVersion: "1.0.20" };
n = un.pickNotice({ ...forcedBase, update: { phase: "idle" } });
ok(n.forced === true && n.kind === "update" && n.tone === "urgent", "触线 idle：强制横幅（forced=true）");
ok(n.action === "none" && render(n).includes("停止支持"), "idle 阶段无按钮，文案讲清「已停止支持」");
ok(/no longer supported/i.test(render(n, "en")),
  "英文强制横幅同样讲清 no longer supported（这是不可弱化的产品红线）");
n = un.pickNotice({ ...forcedBase, update: { phase: "downloading", version: "1.0.27", percent: 66.4 } });
ok(n.forced === true && render(n).includes("66%"), "触线下载中：进度透传");
n = un.pickNotice({ ...forcedBase, update: { phase: "downloaded", version: "1.0.27" } });
ok(n.forced === true && n.action === "restart", "触线就绪：唯一出口=立即重启更新");
n = un.pickNotice({ ...forcedBase, snoozedUntil: NOW + 3600_000, update: { phase: "downloaded", version: "1.0.27" } });
ok(n.forced === true && n.action === "restart", "强制横幅无视「稍后」窗（snooze 只对普通更新生效）");
n = un.pickNotice({ ...forcedBase, update: { phase: "idle" }, announcements: items });
ok(n.forced === true && n.kind === "update", "强制横幅压过 urgent 公告");
n = un.pickNotice({ ...base, appVersion: "1.0.20", minSupportedVersion: "1.0.20", readIds: ["a1", "a2", "a3"], update: { phase: "idle" } });
ok(!n.forced && n.kind === "", "线上客户端一切如常（不误伤）");

console.log(`\nupdate-notify.test.js: ${passed} assertions passed`);
