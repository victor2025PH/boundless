"use strict";
/**
 * 壳级通知（更新就绪 / 官方公告）的纯函数核心。
 *
 * 设计约束（P0 2026-08-14）：
 * - 零 Electron 依赖：main.js 负责 IO（updater 事件、公告 HTTP、状态落盘），
 *   本模块只做「状态 → 该显示哪条通知」的决策，Node 直跑可测。
 * - 单横幅原则：同一时刻最多展示一条（更新就绪 > 紧急公告 > 下载中 > 普通公告），
 *   多条排队靠已读推进，不做轮播——坐席端信息条越少越可信。
 * - 公告 target 版本区间判定 fail-open：公告是低风险信息，target 解析不了宁可多показ
 *   也不静默吞掉（更新动作本身不受公告影响）。
 */

/** 版本比较："1.0.26" vs "1.0.9" → 1；非数字段按 0 容错。 */
function cmpVersions(a, b) {
  const pa = String(a || "").split(".");
  const pb = String(b || "").split(".");
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = parseInt(pa[i], 10) || 0;
    const y = parseInt(pb[i], 10) || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/** 公告 target={minVersion,maxVersion}（含端点，空串=不限）是否命中当前版本。 */
function versionSatisfies(appVersion, target) {
  if (!target || typeof target !== "object") return true;
  const min = String(target.minVersion || "").trim();
  const max = String(target.maxVersion || "").trim();
  if (min && cmpVersions(appVersion, min) < 0) return false;
  if (max && cmpVersions(appVersion, max) > 0) return false;
  return true;
}

const ANN_TYPES = ["urgent", "release", "notice"];

/**
 * 服务端 announcements.json（或本地缓存）→ 规范化条目数组。
 * 容忍：非对象/缺 items/条目缺字段；缺 id 或标题正文全空的条目丢弃。
 */
function normalizeAnnouncements(raw) {
  const items = raw && Array.isArray(raw.items) ? raw.items : Array.isArray(raw) ? raw : [];
  const out = [];
  const seen = new Set();
  for (const it of items) {
    if (!it || typeof it !== "object") continue;
    const id = String(it.id || "").trim();
    const title = String(it.title || "").trim();
    const body = String(it.body || it.body_zh || "").trim();
    if (!id || (!title && !body)) continue;
    if (seen.has(id)) continue;
    seen.add(id);
    const type = ANN_TYPES.includes(it.type) ? it.type : "notice";
    out.push({
      id,
      type,
      title,
      body,
      link: String(it.link || "").trim(),
      published_at: String(it.published_at || "").trim(),
      target: it.target && typeof it.target === "object"
        ? { minVersion: String(it.target.minVersion || ""), maxVersion: String(it.target.maxVersion || "") }
        : { minVersion: "", maxVersion: "" },
    });
  }
  return out;
}

/**
 * 发版公告的隐式版本定向：id="release-vX" 天然只面向「还没升到 X」的客户端——
 * 已在 X（或更新）的用户刚升级完就看到「vX 已发布」横幅是纯噪音。发布脚本因此
 * 不需要知道「上一个版本号」来填 target（semver 无法安全地减一），feed 保持简单。
 * 显式 target 依然生效（两者取交集）；id 不合此形则不做隐式过滤。
 */
function releaseSuperseded(item, appVersion) {
  if (!item || item.type !== "release") return false;
  const m = /^release-v(\d+(?:\.\d+)*)$/.exec(String(item.id || ""));
  if (!m) return false;
  return cmpVersions(appVersion, m[1]) >= 0;
}

/**
 * 完整 feed 解析：条目 + 顶层 min_supported_version（强制升级线，空串=未启用）。
 * 放在 announcements.json 顶层而非 manifest.json 是刻意选择：公告轮询/缓存/广播
 * 链路已经存在，强制升级信号搭同一条链零新基建；且它与「发公告」同属运营动作。
 */
function normalizeFeed(raw) {
  const o = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    items: normalizeAnnouncements(raw),
    minSupportedVersion: String(o.min_supported_version || o.minSupportedVersion || "").trim(),
  };
}

/** 当前版本是否已低于强制升级线（min 为空=未启用，永不强制）。 */
function forcedUpgradeActive(appVersion, minSupportedVersion) {
  const min = String(minSupportedVersion || "").trim();
  return !!min && cmpVersions(appVersion, min) < 0;
}

/** 未读且版本命中的公告，按 紧急 > 发版 > 通知、同级按 published_at 降序。 */
function pendingAnnouncements(items, appVersion, readIds) {
  const read = new Set((readIds || []).map(String));
  const rank = { urgent: 0, release: 1, notice: 2 };
  return (items || [])
    .filter((it) => it && it.id && !read.has(it.id) && versionSatisfies(appVersion, it.target)
      && !releaseSuperseded(it, appVersion))
    .sort((a, b) => {
      const r = (rank[a.type] ?? 9) - (rank[b.type] ?? 9);
      if (r !== 0) return r;
      return String(b.published_at).localeCompare(String(a.published_at));
    });
}

/** 更新横幅被「稍后」后的下次提醒时刻（默认 4 小时）。 */
function nextSnooze(now, hours) {
  const h = Number(hours) > 0 ? Number(hours) : 4;
  return (Number(now) || 0) + h * 3600 * 1000;
}

function _flatten(s, maxLen) {
  const t = String(s || "").replace(/\s+/g, " ").trim();
  return t.length > maxLen ? t.slice(0, maxLen - 1) + "…" : t;
}

const _EMPTY = { kind: "" };

/**
 * 决策入口：给定全部状态，返回当前该显示的一条通知（或 {kind:""}=不显示）。
 * state = {
 *   update: { phase: "idle"|"downloading"|"downloaded", version, percent },
 *   announcements: normalizeAnnouncements 产物,
 *   appVersion, minSupportedVersion?: "", readIds: [], snoozedUntil: ms, now: ms,
 * }
 * 返回 = { kind:"update"|"announcement", tone, badge, action:"restart"|"open"|"none",
 *          id?, link?, version?, forced?: true,
 *          textKey?, vars? | text? }
 *
 * i18n 边界（2026-08-19）：**更新类**文案属界面文字 → 只回 `textKey`(+`vars`)，
 * 取词在渲染层 SH()（此前回中文串，英文坐席看到「当前版本已停止支持…」）。
 * **公告类**文案是运营从服务端下发的内容数据（本就按运营语言撰写），不该被壳词典
 * 翻译，故继续回 `text`。渲染层按「有 textKey 优先取词，否则用 text」消费。
 */
function pickNotice(state) {
  const s = state || {};
  const upd = s.update || {};
  const now = Number(s.now) || Date.now();
  const snoozed = Number(s.snoozedUntil || 0) > now;
  const anns = pendingAnnouncements(s.announcements, s.appVersion, s.readIds);
  const urgent = anns.find((a) => a.type === "urgent");

  // 0. 强制升级线（min_supported_version）：低于线的客户端横幅不可关闭、无视稍后——
  //    这是「旧版继续跑会出事」（协议不兼容/安全缺陷）的最后手段，语义必须压过一切公告。
  //    但它只「催」不「锁」：应用本体保持可用（坐席正聊着单方面锁死是更大的事故），
  //    下载/安装仍走 electron-updater 既有链路，横幅文案随下载阶段推进。
  if (forcedUpgradeActive(s.appVersion, s.minSupportedVersion)) {
    if (upd.phase === "downloaded") {
      return {
        kind: "update", action: "restart", tone: "urgent", badge: "⛔", forced: true,
        version: upd.version || "",
        textKey: "notice.forced_ready",
      };
    }
    if (upd.phase === "downloading") {
      const pct = Math.max(0, Math.min(100, Math.round(Number(upd.percent) || 0)));
      return {
        kind: "update", action: "none", tone: "urgent", badge: "⛔", forced: true,
        version: upd.version || "",
        textKey: "notice.forced_downloading", vars: { pct },
      };
    }
    return {
      kind: "update", action: "none", tone: "urgent", badge: "⛔", forced: true,
      textKey: "notice.forced_fetching",
    };
  }

  // 1. 更新已就绪（未被稍后）——最高优先：一键重启即完成
  if (upd.phase === "downloaded" && !snoozed) {
    const v = upd.version ? `v${upd.version} ` : "";
    return {
      kind: "update", action: "restart", tone: "update", badge: "⬆️",
      version: upd.version || "",
      textKey: "notice.update_ready", vars: { v },
    };
  }
  // 2. 紧急公告
  if (urgent) return _annNotice(urgent);
  // 3. 下载进行中（未被稍后）——透明可见，完成后自动升级为「就绪」横幅
  if (upd.phase === "downloading" && !snoozed) {
    const v = upd.version ? `v${upd.version} ` : "";
    const pct = Math.max(0, Math.min(100, Math.round(Number(upd.percent) || 0)));
    return {
      kind: "update", action: "none", tone: "info", badge: "⬇️",
      version: upd.version || "",
      textKey: "notice.update_downloading", vars: { v, pct },
    };
  }
  // 4. 普通公告（发版说明 / 通知）
  if (anns.length) return _annNotice(anns[0]);
  return _EMPTY;
}

function _annNotice(a) {
  const badge = a.type === "urgent" ? "🚨" : a.type === "release" ? "🆕" : "📢";
  const tone = a.type === "urgent" ? "urgent" : "info";
  const body = _flatten(a.body, 120);
  const text = a.title && body ? `${a.title} · ${body}` : a.title || body;
  return {
    kind: "announcement", action: a.link ? "open" : "none", tone, badge,
    id: a.id, link: a.link || "", text,
  };
}

module.exports = {
  cmpVersions,
  versionSatisfies,
  releaseSuperseded,
  normalizeAnnouncements,
  normalizeFeed,
  forcedUpgradeActive,
  pendingAnnouncements,
  nextSnooze,
  pickNotice,
};
