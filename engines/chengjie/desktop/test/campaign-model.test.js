"use strict";

// campaign-model.js 纯函数单测（活动海报：feed 归一化 / 资格判定 / 频控 / 倒计时）。
// node test/campaign-model.test.js
const assert = require("assert");
const m = require("../renderer/campaign-model.js");

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
}

const NOW = Date.parse("2026-08-21T12:00:00Z");
const H = 3600e3;

function feedWith(over) {
  return {
    campaigns: [Object.assign({
      id: "newbie-6u",
      layout: "poster-countdown",
      audience: "new_user",
      surfaces: ["desktop_popup"],
      starts_at: "2026-08-01T00:00:00Z",
      ends_at: "2027-08-01T00:00:00Z",
      priority: 10,
      frequency: { max_shows: 3, min_interval_hours: 12 },
      window_hours_after_onboarding: 72,
      title: { zh: "新人首充大礼包", en: "Newcomer pack" },
      headline: { zh: "6U → 18,000 Token", en: "6U → 18,000 tokens" },
      sub: { zh: "双倍到账", en: "Double rate" },
      bullets: { zh: ["a", "b"], en: ["a", "b"] },
      cta: { label: { zh: "领取", en: "Claim" }, url: "https://bd2026.cc/order?plan=recharge-newbie-6" },
      dismiss_label: { zh: "稍后再说", en: "Maybe later" },
    }, over || {})],
  };
}

function ctx(over) {
  return Object.assign({
    now: NOW,
    onboardingMs: NOW - 1 * H, // 1 小时前完成首启
    managed: true,
    state: { shows: {}, never: {} },
  }, over || {});
}

// ── 归一化 ──────────────────────────────────────────────────────────────
{
  const f = m.cmNormalizeFeed(feedWith({}));
  ok(f.campaigns.length === 1, "合法条目应通过归一化");
  const c = f.campaigns[0];
  ok(c.maxShows === 3 && c.minIntervalMs === 12 * H, "频控字段应带出");
  ok(c.windowHoursAfterOnboarding === 72, "72h 窗应带出");
  ok(c.ctaUrl.indexOf("https://") === 0, "CTA 应为 https");
}
ok(m.cmNormalizeFeed(null).campaigns.length === 0, "空 feed 应回空数组");
ok(m.cmNormalizeFeed(feedWith({ layout: "poster-video" })).campaigns.length === 0,
  "未知版式应整条丢弃（老客户端对新版式优雅降级）");
ok(m.cmNormalizeFeed(feedWith({ surfaces: ["web_pricing"] })).campaigns.length === 0,
  "不含 desktop_popup surface 应丢弃");
ok(m.cmNormalizeFeed(feedWith({ cta: { label: { zh: "x" }, url: "http://evil" } })).campaigns.length === 0,
  "非 https CTA 应整条丢弃");
ok(m.cmNormalizeFeed(feedWith({ id: "  " })).campaigns.length === 0, "空 id 应丢弃");
{
  const many = m.cmNormalizeFeed({
    campaigns: [
      feedWith({}).campaigns[0],
      { id: "bad" }, // 缺字段的坏条目
      feedWith({ id: "second", priority: 1 }).campaigns[0],
    ],
  });
  ok(many.campaigns.length === 2, "坏条目静默丢弃，好条目保留");
}

// ── 资格判定 ────────────────────────────────────────────────────────────
function pick1(feedOver, ctxOver) {
  return m.cmPick(m.cmNormalizeFeed(feedWith(feedOver)).campaigns, ctx(ctxOver));
}
ok(pick1({}, {}) !== null, "新用户窗口内应可弹");
ok(pick1({}, { managed: false }) === null, "非托管版（开发/自建）绝不弹");
ok(pick1({}, { onboardingMs: 0 }) === null, "首启未完成（audience=new_user）不弹");
ok(pick1({}, { onboardingMs: NOW - 73 * H }) === null, "过了 72h 窗不弹");
ok(pick1({}, { onboardingMs: NOW - 71 * H }) !== null, "71h 仍在窗内");
ok(pick1({ audience: "all" }, { onboardingMs: 0 }) !== null, "audience=all 不要求 onboarding");
ok(pick1({ starts_at: "2026-09-01T00:00:00Z" }, {}) === null, "未开始不弹");
ok(pick1({ ends_at: "2026-08-01T00:00:00Z" }, {}) === null, "已结束不弹");
ok(pick1({}, { state: { shows: {}, never: { "newbie-6u": true } } }) === null, "不再提醒后永不弹");
ok(pick1({}, { state: { shows: { "newbie-6u": { count: 3, last_ts: NOW - 24 * H } }, never: {} } }) === null,
  "达到 max_shows 不弹");
ok(pick1({}, { state: { shows: { "newbie-6u": { count: 1, last_ts: NOW - 1 * H } }, never: {} } }) === null,
  "距上次展示 < 12h 不弹");
ok(pick1({}, { state: { shows: { "newbie-6u": { count: 1, last_ts: NOW - 13 * H } }, never: {} } }) !== null,
  "距上次展示 > 12h 可再弹");

// ── 多活动择优 ──────────────────────────────────────────────────────────
{
  const a = m.cmNormalizeFeed(feedWith({ id: "low", priority: 1 })).campaigns[0];
  const b = m.cmNormalizeFeed(feedWith({ id: "high", priority: 9 })).campaigns[0];
  const win = m.cmPick([a, b], ctx({}));
  ok(win && win.id === "high", "priority 高者胜出");
}

// ── 截止与倒计时 ────────────────────────────────────────────────────────
{
  const c = m.cmNormalizeFeed(feedWith({})).campaigns[0];
  const ob = NOW - 1 * H;
  ok(m.cmDeadline(c, ob) === ob + 72 * H, "截止 = onboarding + 72h");
  ok(m.cmDeadline(c, 0) === 0, "无 onboarding 无截止");
  const c2 = m.cmNormalizeFeed(feedWith({ window_hours_after_onboarding: 0 })).campaigns[0];
  ok(m.cmDeadline(c2, ob) === 0, "window=0 表示不限窗");
}
ok(m.cmCountdownText(71 * H + 59 * 60e3 + 59e3) === "71:59:59", "72h 窗倒计时按累计小时显示");
ok(m.cmCountdownText(61e3) === "00:01:01", "分秒补零");
ok(m.cmCountdownText(0) === "" && m.cmCountdownText(-5) === "", "到点/过点返回空串（渲染层收起）");

// ── 状态迁移（纯函数：不改入参） ─────────────────────────────────────────
{
  const s0 = { shows: {}, never: {} };
  const s1 = m.cmRecordShow(s0, "newbie-6u", NOW);
  ok(s1.shows["newbie-6u"].count === 1 && s1.shows["newbie-6u"].last_ts === NOW, "记一次展示");
  ok(!s0.shows["newbie-6u"], "入参 state 不被原地修改");
  const s2 = m.cmRecordShow(s1, "newbie-6u", NOW + H);
  ok(s2.shows["newbie-6u"].count === 2, "展示计数累加");
  const s3 = m.cmOptOut(s2, "newbie-6u");
  ok(s3.never["newbie-6u"] === true && s3.shows["newbie-6u"].count === 2, "opt-out 保留展示历史");
}

// ── 双语取文 ────────────────────────────────────────────────────────────
ok(m.cmLangText({ zh: "领取", en: "Claim" }, "en") === "Claim", "en 取英文");
ok(m.cmLangText({ zh: "领取", en: "Claim" }, "en-US") === "Claim", "en-US 取英文");
ok(m.cmLangText({ zh: "领取", en: "Claim" }, "") === "领取", "缺省取中文");
ok(m.cmLangText({ zh: "领取", en: "" }, "en") === "领取", "英文缺失回落中文");

// ── CTA 追加注册时间锚（官网真倒计时半件；2026-08-22） ─────────────────────
{
  const BASE = "https://bd2026.cc/order?plan=recharge-newbie-6";
  const OB = NOW - 1 * H;
  const out = m.cmCtaUrlWithReg(BASE, OB, ["bd2026.cc"]);
  ok(out.indexOf("reg_ts=" + Math.floor(OB / 1000)) > 0, "白名单域 + 有 onboarding → 追加 reg_ts（秒）");
  ok(out.indexOf("plan=recharge-newbie-6") > 0, "原有参数保留");
  ok(m.cmCtaUrlWithReg(BASE, 0, ["bd2026.cc"]) === BASE, "未完成首启（ms=0）不追加");
  ok(m.cmCtaUrlWithReg(BASE, OB, ["other.com"]) === BASE, "非白名单域原样返回（注册时间不带给第三方）");
  ok(m.cmCtaUrlWithReg("https://evil.bd2026.cc.attacker.io/x", OB, ["bd2026.cc"])
    === "https://evil.bd2026.cc.attacker.io/x", "伪装子域串（后缀非 .bd2026.cc）不追加");
  const sub = m.cmCtaUrlWithReg("https://www.bd2026.cc/order", OB, ["bd2026.cc"]);
  ok(sub.indexOf("reg_ts=") > 0, "真子域（www.）允许追加");
  ok(m.cmCtaUrlWithReg("http://bd2026.cc/order", OB, ["bd2026.cc"]) === "http://bd2026.cc/order",
    "非 https 原样返回");
  const already = BASE + "&reg_ts=123";
  ok(m.cmCtaUrlWithReg(already, OB, ["bd2026.cc"]) === already, "已带 reg_ts 不重复追加（幂等）");
  ok(m.cmCtaUrlWithReg("", OB, ["bd2026.cc"]) === "", "空串安全");
}

// ── skip 原因汇总（P0 2026-08-22 可诊断化） ─────────────────────────────────
{
  ok(m.cmSkipSummary([], ctx({})).reason === "no_feed", "空 feed → no_feed");
  const list = m.cmNormalizeFeed(feedWith({})).campaigns;
  ok(m.cmSkipSummary(list, ctx({})) === null, "有合格活动 → null（不产生 skip）");
  const s1 = m.cmSkipSummary(list, ctx({ onboardingMs: NOW - 73 * H }));
  ok(s1 && s1.reason === "window_passed" && s1.id === "newbie-6u", "72h 窗已过 → window_passed 带活动 id");
  ok(m.cmSkipSummary(list, ctx({ managed: false })).reason === "not_managed", "非托管 → not_managed");
  ok(m.cmSkipSummary(list, ctx({ onboardingMs: 0 })).reason === "no_onboarding", "向导未完成 → no_onboarding");
  const s2 = m.cmSkipSummary(list,
    ctx({ state: { shows: { "newbie-6u": { count: 3, last_ts: NOW - 24 * H } }, never: {} } }));
  ok(s2.reason === "max_shows", "额度用尽 → max_shows");
  // 多活动：原因取最高 priority 那张（代表性原因，与 cmPick 的择优视角一致）
  const lo = m.cmNormalizeFeed(feedWith({ id: "lo", priority: 1, audience: "all" })).campaigns[0];
  const hi = m.cmNormalizeFeed(feedWith({ id: "hi", priority: 9 })).campaigns[0];
  const s3 = m.cmSkipSummary([lo, hi], ctx({ onboardingMs: NOW - 73 * H, managed: false }));
  ok(s3.id === "hi", "多活动 skip 原因取最高 priority 条目");
}

// ── headline 数字拆解（P1 CountUp） ────────────────────────────────────────
{
  const hp = m.cmHeadlineParts("6U → 18,000 Token");
  ok(hp && hp.num === "18,000" && hp.value === 18000, "取最大数字（含千分位原样保留）");
  ok(hp.before === "6U → " && hp.after === " Token", "前后段原样切分");
  ok(m.cmHeadlineParts("6U 大礼包") === null, "无 ≥1000 数字 → null（'6' 不值得滚）");
  ok(m.cmHeadlineParts("") === null && m.cmHeadlineParts(null) === null, "空/坏输入安全");
  const plain = m.cmHeadlineParts("送 18000 Token");
  ok(plain && plain.num === "18000" && plain.value === 18000, "无千分位写法同样识别");
}

// ── 倒计时紧迫档（P1 递进变色） ─────────────────────────────────────────────
ok(m.cmCountdownUrgent(23 * H) === true, "剩 23h → 紧迫");
ok(m.cmCountdownUrgent(25 * H) === false, "剩 25h → 常规");
ok(m.cmCountdownUrgent(0) === false && m.cmCountdownUrgent(-1) === false, "过期/零剩余不紧迫");

// ── poster-hero 版式 + ctaSub（P1 新增面） ──────────────────────────────────
{
  const hero = m.cmNormalizeFeed(feedWith({ layout: "poster-hero" }));
  ok(hero.campaigns.length === 1 && hero.campaigns[0].layout === "poster-hero",
    "poster-hero 进白名单（视觉资产随包、文本仍来自 feed）");
  const withSub = m.cmNormalizeFeed(feedWith({
    cta: {
      label: { zh: "领取", en: "Claim" },
      sub: { zh: "仅需 6U", en: "Only 6U" },
      url: "https://bd2026.cc/order?plan=recharge-newbie-6",
    },
  })).campaigns[0];
  ok(withSub.ctaSub.zh === "仅需 6U" && withSub.ctaSub.en === "Only 6U", "cta.sub 归一化带出");
  const noSub = m.cmNormalizeFeed(feedWith({})).campaigns[0];
  ok(noSub.ctaSub && noSub.ctaSub.zh === "" && noSub.ctaSub.en === "", "缺 cta.sub → 双语空串（渲染层跳过）");
}

console.log(`campaign-model.test.js: ${passed} assertions passed`);
